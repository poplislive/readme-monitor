"""One synchronization cycle: reconcile, detect, generate if needed, propose, exit.

Every stage the cycle actually performs is recorded as an event (docs/api.md, "Event data by
stage"). Events describe what happened, never estimates of progress."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import replace
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import GENERATOR_VERSION, discovery
from .config import Config, RepoConfig
from .credentials import redact
from .evidence import Snapshot, build_payload, fingerprints, select_sources
from .github import GitHub, GitHubError, Incomplete, NotFound, RateLimited
from .llm import BudgetBlocked, Generator, InvalidOutput, LLMError
from .publish import Blocked, Conflict, Publisher, Stale
from .readme import (
    MarkerError, apply, migrate, parse, render_license, render_project_structure, render_recent_changes, unified_diff,
)

log = logging.getLogger(__name__)

# States in which an unchanged input set means there is nothing left to do.
SETTLED = {"no_changes", "proposal_created", "awaiting_merge", "published", "rejected", "conflict", "needs_init", "paused",
           "unsupported_readme", "publish_blocked"}
# GitHub's primary README lookup order: .github/, then the root, then docs/.
README_RE = re.compile(r"^(?:\.github/|docs/)?readme(?:\.[a-z]+)?$", re.I)

# Worker state -> result.outcome (docs/api.md). Anything not listed is "blocked".
OUTCOMES = {
    "published": "confirmed", "pushed": "pushed", "merged_unconfirmed": "pushed", "awaiting_merge": "awaiting_merge",
    "waiting_for_checks": "awaiting_checks", "proposal_created": "preview", "no_changes": "no_change", "unchanged": "unchanged",
    "generation_deferred": "deferred", "budget_paused": "deferred", "adoption_deferred": "deferred", "stale": "deferred",
    "paused": "paused", "conflict": "conflict", "cancelled": "cancelled",
    "error": "failed", "inaccessible": "failed", "rate_limited": "failed",
}
OUTCOME_TEXT = {
    "confirmed": "README verified on the target branch", "pushed": "written on GitHub; not yet verified on the target branch",
    "awaiting_merge": "pull request open, waiting for a person to merge it", "awaiting_checks": "waiting for running checks",
    "preview": "draft only; nothing was written to GitHub", "no_change": "checked; the README is already up to date",
    "unchanged": "checked; nothing relevant changed", "deferred": "checked; update deferred", "paused": "paused",
    "blocked": "blocked", "conflict": "not updated: a person edited a managed section", "failed": "failed",
    "cancelled": "stopped before finishing; pending work is kept",
}


class UnsupportedReadme(Exception):
    pass


class Cancelled(Exception):
    """A person asked the run to stop; raised only at a safe boundary."""


def detect_readme(paths: list[str]) -> str | None:
    """The primary README GitHub shows, with its actual filename case; None when there is none."""
    found = [p for p in paths if README_RE.match(p)]
    for prefix in (".github/", "", "docs/"):
        here = sorted((p for p in found if p.lower().startswith(prefix) and p.count("/") == prefix.count("/")),
                      key=lambda p: (not p.lower().endswith(".md"), p != prefix + "README.md", p))
        if here:
            return here[0]
    return None


class Locked(Exception):
    pass


@contextmanager
def file_lock(path: Path, wait: float = 0):
    """Process-wide lock so a manual run and a scheduled run never overlap. `wait` seconds of
    retries let a real run get past a momentary probe of the lock; a run in progress holds it far longer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    deadline = time.monotonic() + wait
    while True:
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                raise Locked(f"another ReadmeMonitor run holds {path}") from None
            time.sleep(0.1)
    try:
        yield
    finally:
        f.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Prepared:
    snap: Snapshot
    doc_fp: str
    llm_fp: str
    files: dict[str, tuple[str, str, str | None]] = field(default_factory=dict)  # path -> (old, new, blob)
    changes: dict[str, list[str]] = field(default_factory=dict)
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    llm_requested: dict[str, list[str]] = field(default_factory=dict)
    payloads: dict[str, dict] = field(default_factory=dict)
    generated: bool = False  # a paid Claude request was made
    estimate_usd: float | None = None  # offline character estimate of a model request not made (dry run)

    @property
    def changed(self) -> bool:
        return any(old != new for old, new, _ in self.files.values())


class Worker:
    def __init__(self, cfg: Config, db, gh: GitHub, generator: Generator, home: Path, run_id: int, deadline: float | None = None,
                 trigger: str = "manual", dry_run: bool = False, only: list[str] | None = None, guard=None,
                 launch: str | None = None):
        self.cfg, self.db, self.gh, self.gen, self.home, self.run_id = cfg, db, gh, generator, home, run_id
        self.launch = launch  # the interface's token for the run it launched; see DB.request_cancel
        self.deadline = deadline or (time.monotonic() + cfg.run_timeout_seconds)
        self.publisher = Publisher(cfg, db, gh, guard=guard)
        self.active: set[str] | None = None  # repositories processed this cycle (set by run)
        self.trigger, self.dry_run = trigger, dry_run
        self.only = {n.lower() for n in only} if only else None
        self.cancelled = False
        self._started = False
        self._repo: str | None = None
        self._last_outcome: str | None = None
        self._final = False
        self._evented = False

    # ---- events -----------------------------------------------------------
    def emit(self, stage: str, status: str, message: str, repo: str | None = None, revision: str | None = None, **data) -> int:
        """Events are a report, never part of the work: a failure to store one is logged and the
        cycle (and its ledger and checkpoint writes) carries on."""
        name = repo or (None if stage in ("run", "discover") else self._repo)
        if name:
            self._evented = True
        try:
            if stage != "run" and not self._started:
                self._started = True
                self.db.add_event(self.run_id, self.trigger, None, "run", "started",
                                  f"{'Dry run' if self.dry_run else 'Check'} started ({self.trigger})", {"dry_run": self.dry_run})
            row = self.db.conn.execute("SELECT repo_id FROM repos WHERE name=?", (name,)).fetchone() if name else None
            return self.db.add_event(self.run_id, self.trigger, name, stage, status, message, data, revision, row[0] if row else None)
        except Exception as e:  # noqa: BLE001 - see docstring
            log.warning("activity event not stored (%s): %s", type(e).__name__, redact(message)[0][:120])
            return 0

    def _result(self, state: str, detail: str | None = None, outcome: str | None = None, final: bool = True, **extra):
        outcome = outcome or OUTCOMES.get(state, "blocked")
        if final:
            self._final = True
        if outcome == self._last_outcome:
            return
        self._last_outcome = outcome
        p = self.db.latest_proposal(self._repo) if outcome in ("confirmed", "pushed", "awaiting_merge") else None
        if p is not None:
            extra.setdefault("proposal_id", p["id"])
            extra.setdefault("pr_url", p["pr_url"])
            if p["published_sha"]:
                extra.setdefault("commit_sha", p["published_sha"])
        status = "failed" if outcome == "failed" else ("skipped" if outcome in ("deferred", "paused", "cancelled") else "done")
        msg = f"{self._repo}: {OUTCOME_TEXT.get(outcome, outcome)}" + (f": {detail}" if detail else "")
        self.emit("result", status, msg, outcome=outcome, state=state, detail=detail, **extra)

    def _check_cancel(self, where: str):
        if self.db.cancel_requested(self.run_id, self.launch):
            raise Cancelled(f"cancelled by request {where}")

    def _inspect(self, repo: RepoConfig, row, snap: Snapshot):
        """What changed since the last analyzed revision, from GitHub's own compare answer."""
        baseline = row["analyzed_head"] or row["head_sha"]
        head = snap.head_sha
        data = {"baseline": baseline, "head": head, "first_scan": baseline is None, "checks_pending": snap.pending_checks[:20]}
        commits, files, total, truncated = [], [], 0, False
        try:
            if baseline is None:
                commits, total = [self.gh.commit(repo.name, head)], 1
            elif baseline == head:
                data["compare_status"] = "identical"
            else:
                c = self.gh.compare(repo.name, baseline, head)
                data["compare_status"] = c["status"]
                commits, files, total, truncated = c["commits"], c["files"], c["total_commits"], c["truncated"]
        except GitHubError as e:  # never fails the cycle; content fingerprints still decide what changed
            log.info("%s: compare unavailable (%s)", repo.name, type(e).__name__)
            data["compare_status"] = "unavailable"
        data.update(commits=commits[-20:], commits_total=total, files_changed=files[:50], files_total=len(files),
                    truncated=truncated or len(commits) > 20 or len(files) > 50)
        if data["first_scan"]:
            what = "first scan"
        elif data["compare_status"] == "identical":
            what = "same revision as the last analysis"
        elif data["compare_status"] == "diverged":
            what = "history was rewritten since the last analysis"
        elif data["compare_status"] == "unavailable":
            what = "the change list is not available from GitHub"
        else:
            what = f"{total} new commit(s), {len(files)} file(s) changed since {baseline[:7]}"
        self.emit("inspect", "done", f"{repo.name}: {snap.branch} at {head[:7]}; {what}", revision=head, **data)

    def _record_draft(self, repo: RepoConfig, prep: Prepared, kind: str) -> int:
        files = {p: {"old": old, "new": new, "diff": unified_diff(old, new, p), "changed": prep.changes.get(p, []),
                     "conflicts": prep.conflicts.get(p, [])} for p, (old, new, _) in prep.files.items()}
        return self.db.add_draft(self.run_id, repo.name, kind, files, prep.flags, prep.notes, prep.estimate_usd)

    def _validated(self, repo: RepoConfig, prep: Prepared, draft_id: int | None):
        changed = {p: s for p, s in prep.changes.items() if s}
        what = ("; ".join(f"{p}: {', '.join(s)}" for p, s in changed.items()) or "no section changes") + \
               (f"; {sum(map(len, prep.conflicts.values()))} section(s) edited by a person left alone" if prep.conflicts else "")
        self.emit("validate", "done", f"{repo.name}: {what}", revision=prep.snap.head_sha, changed=changed, conflicts=prep.conflicts,
                  flags=prep.flags[:50], notes=prep.notes[:20], draft_id=draft_id)

    # ---- snapshot ---------------------------------------------------------
    def _head(self, repo: RepoConfig) -> tuple[dict, str, str]:
        meta = self.gh.repo(repo.name)
        branch = repo.branch or meta["default_branch"]
        return meta, branch, self.gh.head_sha(repo.name, branch)

    def snapshot(self, repo: RepoConfig, meta: dict, branch: str, head: str, releases: list[dict]) -> tuple[Snapshot, RepoConfig]:
        """The inspected revision, and the repository settings with the README path resolved."""
        tree = [{"path": e["path"], "sha": e["sha"], "size": e.get("size", 0)} for e in self.gh.tree(repo.name, head)]
        blobs = {e["path"]: e["sha"] for e in tree}
        if repo.readme_auto:
            found = detect_readme(list(blobs))
            if found and not found.lower().endswith((".md", ".markdown")):
                raise UnsupportedReadme(f"the primary README is {found}, not Markdown; it is left to people")
            repo = replace(repo, readme_paths=(found or "README.md",))
        readmes = {p: ((self.gh.blob_text(repo.name, blobs[p]), blobs[p]) if p in blobs else (None, None)) for p in repo.readme_paths}
        lic = (meta.get("license") or {}).get("spdx_id")
        snap = Snapshot(repo.name, branch, head, meta.get("description") or "", lic, tree, readmes, releases, self.gh.checks(repo.name, head))
        return snap, repo

    def load_sources(self, snap: Snapshot, repo: RepoConfig) -> list[str]:
        selected, skipped = select_sources(snap.tree, repo, self.cfg)
        for e in selected:
            text = self.gh.blob_text(repo.name, e["sha"])
            if text is None:
                skipped.append(f"{e['path']}: binary or not UTF-8")
            else:
                snap.sources[e["path"]] = text
        return skipped

    # ---- content ----------------------------------------------------------
    def deterministic(self, snap: Snapshot, repo: RepoConfig) -> dict[str, str]:
        out = {}
        for sid in repo.deterministic_sections:
            if sid == "project-structure":
                out[sid] = render_project_structure(snap.paths)
            elif sid == "recent-changes":
                out[sid] = render_recent_changes(repo.name, snap.releases, snap.branch)
            elif sid == "license":
                out[sid] = render_license(snap.license_spdx, snap.license_path)
        return out

    def prepare(self, repo: RepoConfig, snap: Snapshot, use_llm: bool) -> Prepared:
        """Compute the proposed README text(s). Calls Claude only if use_llm and the Claude
        inputs changed since the last stored generation."""
        row = self.db.repo(repo.name)
        doc_fp, llm_fp = fingerprints(snap, repo, self.cfg)
        prep = Prepared(snap, doc_fp, llm_fp)
        det = self.deterministic(snap, repo)
        stored = json.loads(row["generated_sections"] or "{}") if row["generated_llm_fp"] == llm_fp else None
        if stored is not None:
            prep.flags += json.loads(row["generated_flags"] or "[]")
        generated: dict[str, dict[str, str]] = {}
        new_flags: list[str] = []
        sources_loaded, skipped, mode, evidence = False, [], "deterministic", False
        for path, (text, blob) in snap.readmes.items():
            blocks = {b.id: b for b in parse(text)}
            requested = [s for s in repo.llm_sections if s in blocks]
            llm_out: dict[str, str] = {}
            if requested:
                if repo.api_export == "none":
                    prep.notes.append(f"{path}: api_export = none, so Claude-written sections are not updated")
                elif stored is not None:
                    llm_out = stored.get(path, {})
                    mode = "stored"
                else:
                    if not sources_loaded:
                        skipped = self.load_sources(snap, repo)
                        prep.flags += [f"skipped {s}" for s in skipped]
                        sources_loaded = True
                    current = {s: blocks[s].content.strip() for s in requested}
                    payload, pflags = build_payload(snap, repo, self.cfg, path, current, requested)
                    prep.payloads[path] = payload
                    prep.llm_requested[path] = requested
                    prep.flags += pflags
                    evidence = True
                    self.emit("evidence", "done", f"{repo.name}: read {len(snap.sources)} allowlisted file(s) as evidence for {path}",
                              revision=snap.head_sha, export=repo.api_export, files_read=sorted(snap.sources)[:60],
                              skipped=skipped[:20], facts=len(payload["facts"]), readmes=[path],
                              payload_chars=len(json.dumps(payload, ensure_ascii=False)), truncated=len(snap.sources) > 60)
                    if use_llm:
                        def sent(attempt, requested=requested):
                            self.emit("draft", "started", f"{repo.name}: request sent to {self.cfg.model} for {', '.join(requested)}"
                                      + (f" (attempt {attempt})" if attempt > 1 else ""), revision=snap.head_sha, mode="claude",
                                      sections_requested=requested, model=self.cfg.model)

                        result = self.gen.generate(repo, self.run_id, payload, snap.paths, on_request=sent)
                        prep.generated = True
                        mode = "claude"
                        llm_out = result.sections
                        new_flags += result.flags + pflags
                        generated[path] = llm_out
                        self.emit("draft", "done", f"{repo.name}: answer received for {path} ({len(llm_out)} section(s) returned)",
                                  revision=snap.head_sha, mode="claude", sections_requested=requested,
                                  sections_returned=sorted(llm_out), model=self.cfg.model, usage=result.usage)
                    elif self.cfg.price() is not None:
                        prep.estimate_usd = (prep.estimate_usd or 0) + self.gen.estimate(payload)  # offline: characters only
            res = apply(text, {**det, **llm_out})
            prep.files[path] = (text, res.text, blob)
            prep.changes[path] = res.changed
            if res.conflicts:
                prep.conflicts[path] = res.conflicts
        if not evidence:
            self.emit("evidence", "done", f"{repo.name}: read the README and the file tree" +
                      ("" if repo.api_export == "allowlist" else "; no source is sent to Claude (api_export = none)"),
                      revision=snap.head_sha, export=repo.api_export, files_read=[], readmes=list(snap.readmes))
        if mode != "claude":
            mode = "dry_run" if self.dry_run else mode
            extra = {"estimate_usd": round(prep.estimate_usd, 4)} if prep.estimate_usd is not None else {}
            self.emit("draft", "done", f"{repo.name}: draft computed ({mode.replace('_', ' ')}; no model request)",
                      revision=snap.head_sha, mode=mode, model=self.cfg.model, **extra)
        if use_llm and prep.payloads:
            # new_flags repeats the payload flags already added above (it is stored for reuse).
            prep.flags = list(dict.fromkeys(prep.flags + new_flags))
            self.db.update_repo(repo.name, generated_llm_fp=llm_fp, generated_sections=json.dumps(generated), generated_flags=json.dumps(new_flags))
        return prep

    def preview(self, repo: RepoConfig, use_llm: bool = False) -> tuple[Snapshot, RepoConfig, Prepared, list[str]]:
        """Read GitHub and compute the proposal without changing any checkpoint. A README not
        adopted yet is previewed on top of its adoption migration. Returns (snap, repo, prep, adopted)."""
        row = self.db.repo(repo.name)
        self._repo = repo.name
        meta, branch, head = self._head(repo)
        releases = self.gh.releases(repo.name) if "recent-changes" in repo.deterministic_sections else []
        snap, repo = self.snapshot(repo, meta, branch, head, releases)
        self._inspect(repo, row, snap)
        originals = {p: t or "" for p, (t, _) in snap.readmes.items()}
        adopted = []
        for path, (old, new) in adoption(repo, snap).items():
            if old != new:
                adopted.append(path)
                snap.readmes[path] = (new, snap.readmes[path][1])
        prep = self.prepare(repo, snap, use_llm=use_llm)
        for path in prep.files:
            _, new, blob = prep.files[path]
            prep.files[path] = (originals[path], new, blob)
        return snap, repo, prep, adopted

    def dry_process(self, repo: RepoConfig) -> str:
        """Dry run: reads GitHub, stores a draft and events, writes nothing else anywhere."""
        self._repo, self._last_outcome, self._final, self._evented = repo.name, None, False, False
        try:
            _, repo, prep, adopted = self.preview(repo)
        except (UnsupportedReadme, MarkerError, GitHubError, ValueError) as e:
            self._result("error", redact(f"{type(e).__name__}: {e}")[0][:300], outcome="failed" if isinstance(e, GitHubError) else "blocked")
            return "error"
        if adopted:
            prep.notes.append(f"not adopted yet ({', '.join(adopted)}); shown on top of the one-time adoption")
        did = self._record_draft(repo, prep, "dry_run")
        self._validated(repo, prep, did)
        est = f"; a model request would reserve at most ${prep.estimate_usd:.4f}" if prep.estimate_usd is not None else ""
        asked = sorted({s for sections in prep.llm_requested.values() for s in sections})
        if prep.changed:
            self._result("dry_run", f"{sum(map(len, prep.changes.values()))} section(s) would change{est}", outcome="preview", draft_id=did)
        elif asked:  # nothing changes without the model, but saying "nothing needed updating" would be wrong
            self._result("dry_run", f"a live run would ask the model to draft {', '.join(asked)}{est}; "
                         "no other section would change", outcome="preview", draft_id=did)
        else:
            self._result("dry_run", f"no section would change{est}", outcome="no_change", draft_id=did)
        return "previewed"

    # ---- cycle ------------------------------------------------------------
    def process(self, repo: RepoConfig) -> str:
        self._repo, self._last_outcome, self._final, self._evented = repo.name, None, False, False
        prev = self.db.repo(repo.name)["state"]
        state = self._process(repo)
        if state in ("unchanged", "backoff") or self._final or (state == prev and not self._evented):
            return state  # an unchanged repository produces no event of its own
        row = self.db.conn.execute("SELECT state_detail FROM repos WHERE name=?", (repo.name,)).fetchone()
        if row is not None:
            self._result(state, row["state_detail"])
        return state

    def _process(self, repo: RepoConfig) -> str:
        name = repo.name
        row = self.db.repo(name)
        now = _utc()
        now_s = now.isoformat(timespec="seconds")
        if repo.paused or row["paused"]:
            self.db.update_repo(name, state="paused", last_checked_at=now_s)
            return "paused"
        if row["presence"] == "transferred":
            self.db.update_repo(name, last_checked_at=now_s)
            return "transferred"
        if row["next_attempt_at"] and row["next_attempt_at"] > now_s:
            return "backoff"
        try:
            try:
                meta, branch, head = self._head(repo)
            except GitHubError as e:
                if e.status != 409:  # GitHub answers 409 "Git Repository is empty" for a repository without commits
                    raise
                self.db.update_repo(name, state="empty", state_detail="no commits yet; a README is proposed after the first push",
                                    last_checked_at=now_s, consecutive_errors=0,
                                    next_attempt_at=(now + timedelta(hours=6)).isoformat(timespec="seconds"))
                return "empty"
            moved = self._moved(repo, meta, now_s)
            if moved:
                return moved
            if meta.get("archived") or meta.get("disabled"):
                self.db.update_repo(name, state="archived", state_detail="archived or disabled on GitHub; nothing is written",
                                    last_checked_at=now_s, consecutive_errors=0)
                return "archived"
            if row["repo_id"] is None and meta.get("id"):
                self.db.update_repo(name, repo_id=meta["id"])
            outcome = self.publisher.reconcile(repo, branch)
            if outcome == "merged_unconfirmed":
                # Do not re-propose while GitHub finishes landing the merge.
                if row["state"] != outcome:
                    p = self.db.latest_proposal(name)
                    self.emit("await", "info", f"{name}: merged; waiting to see the README on {branch}", waiting_for="merge_confirmation",
                              pr_url=p["pr_url"] if p else None, since=now_s)
                self.db.update_repo(name, state=outcome, last_checked_at=now_s)
                return outcome
            if outcome in ("published", "rejected"):
                self.db.update_repo(name, state=outcome, **({"last_published_at": now_s} if outcome == "published" else {}))
                row = self.db.repo(name)
                self._result(outcome, f"read back from {branch}" if outcome == "published" else "pull request closed without merge", final=False)
            releases = self.gh.releases(name) if "recent-changes" in repo.deterministic_sections else []
            quick = hashlib.sha256(json.dumps([GENERATOR_VERSION, head, branch, repo.fingerprint_part(), repo.publish_mode,
                                               [(r["tag_name"], r.get("published_at")) for r in releases[:5]]]).encode()).hexdigest()
            if quick == row["quick_key"] and row["state"] in SETTLED and outcome != "retry":
                self.db.update_repo(name, last_checked_at=now_s, consecutive_errors=0)
                return "unchanged"
            if row["default_branch"] and row["default_branch"] != meta["default_branch"]:
                log.info("%s: default branch changed %s -> %s", name, row["default_branch"], meta["default_branch"])
            snap, repo = self.snapshot(repo, meta, branch, head, releases)
            self._inspect(repo, row, snap)
            base = dict(head_sha=head, target_branch=branch, default_branch=meta["default_branch"],
                        last_checked_at=now_s, consecutive_errors=0, next_attempt_at=None,
                        head_changed_at=now_s if row["head_sha"] != head else (row["head_changed_at"] or now_s))
            if snap.pending_checks:
                since = row["waiting_since"] or now_s
                if datetime.fromisoformat(since) + timedelta(minutes=self.cfg.check_wait_minutes) > now:
                    self.db.update_repo(name, state="waiting_for_checks", waiting_since=since,
                                        state_detail="pending: " + ", ".join(snap.pending_checks[:5]), **base)
                    self.emit("await", "info", f"{name}: waiting for {len(snap.pending_checks)} running check(s)", revision=head,
                              waiting_for="checks", since=since)
                    return "waiting_for_checks"
            base["waiting_since"] = None
            missing = [p for p, (t, _) in snap.readmes.items() if t is None]
            unmanaged = [p for p, (t, _) in snap.readmes.items() if t is not None and not parse(t)]
            if missing or unmanaged:
                return self.adopt_or_wait(repo, snap, meta, row, base, quick, missing, unmanaged, now)
            doc_fp, llm_fp = fingerprints(snap, repo, self.cfg)
            if doc_fp == row["analyzed_fp"] and row["state"] in SETTLED and outcome != "retry":
                self.db.update_repo(name, quick_key=quick, analyzed_head=head, **base)
                self._result(row["state"], "no input the README depends on changed", outcome="unchanged")
                return row["state"]
            if repo.publish_mode != "preview" and not (meta.get("permissions") or {}).get("push"):
                # Checked before generation so no paid request is made for an unpublishable result.
                self.db.update_repo(name, state="permission_missing", state_detail="the GitHub credential cannot push to this repository (needs contents and pull request write access)", **base)
                return "permission_missing"
            if self.needs_paid_generation(repo, snap, llm_fp, row):
                pending = row["pending_since"] or now_s
                wait = self.generation_wait(name, row, pending, base["head_changed_at"], now)
                if wait:
                    state, detail, retry_at = wait
                    self.db.update_repo(name, state=state, state_detail=detail, pending_since=pending, **base)
                    if retry_at:
                        self.db.update_repo(name, next_attempt_at=retry_at)
                    return state
                self.db.update_repo(name, pending_since=pending)
            self._check_cancel("before the draft step")
            prep = self.prepare(repo, snap, use_llm=True)
            done = dict(analyzed_fp=prep.doc_fp, analyzed_head=head, quick_key=quick, pending_since=None, **base)
            if prep.generated:
                done["last_generated_at"] = now_s
            draft_id = self._record_draft(repo, prep, "proposal") if prep.changed else None
            self._validated(repo, prep, draft_id)
            if not prep.changed:
                state = "conflict" if prep.conflicts else ("published" if row["state"] == "published" else "no_changes")
                detail = "; ".join(f"{p}: {', '.join(s)} edited by a person" for p, s in prep.conflicts.items()) or "; ".join(prep.notes) or None
                self.db.update_repo(name, state=state, state_detail=detail, **done)
                if state == "published":
                    self._result(state, "the README on the target branch is current", outcome="no_change")
                return state
            try:
                state = self.propose(repo, prep, draft_id)
            except Blocked as e:
                self.emit("publish", "failed", f"{name}: {e}", mode=repo.publish_mode, branch=snap.branch, error=str(e))
                self.db.update_repo(name, state="publish_blocked", state_detail=str(e)[:300], **done)
                if e.partial:  # part of the change is on GitHub: the result says what was written
                    self._result("publish_blocked", str(e)[:300], outcome="pushed", commit_sha=e.partial)
                return "publish_blocked"
            self.db.update_repo(name, **done)
            return state
        except Cancelled as e:
            # Pending work is preserved: nothing past this point was started, the checkpoint is not advanced.
            self.cancelled = True
            self.db.update_repo(name, state="cancelled", state_detail=f"{e}; retried on the next cycle", last_checked_at=now_s)
            return "cancelled"
        except UnsupportedReadme as e:
            self.db.update_repo(name, state="unsupported_readme", state_detail=str(e), last_checked_at=now_s, consecutive_errors=0)
            return "unsupported_readme"
        except BudgetBlocked as e:
            # Pending work is preserved: analyzed_fp is not advanced, so the next run retries.
            self.db.update_repo(name, state=e.kind, state_detail=str(e), last_checked_at=now_s,
                                pending_since=self.db.repo(name)["pending_since"] or now_s)
            return e.kind
        except MarkerError as e:
            self.db.update_repo(name, state="conflict", state_detail=f"malformed README markers: {e}", last_checked_at=now.isoformat(timespec="seconds"))
            return "conflict"
        except Conflict as e:
            self.db.update_repo(name, state="conflict", state_detail=str(e), last_checked_at=now.isoformat(timespec="seconds"))
            return "conflict"
        except Stale as e:
            self.db.update_repo(name, state="stale", state_detail=f"{e}; re-analyzing next run", last_checked_at=now.isoformat(timespec="seconds"))
            return "stale"
        except RateLimited as e:
            self.db.update_repo(name, state="rate_limited", state_detail=str(e))
            self._result("rate_limited", str(e))
            raise
        except (NotFound, Incomplete, GitHubError, LLMError, ValueError) as e:
            if isinstance(e, InvalidOutput):
                self.emit("validate", "failed", f"{name}: model answer rejected: {e}", error=str(e))
            elif isinstance(e, LLMError):
                self.emit("draft", "failed", f"{name}: no usable model answer: {e}", error=str(e), model=self.cfg.model)
            kind = "inaccessible" if isinstance(e, NotFound) else "error"
            return self._fail(name, row, kind, e)

    def _moved(self, repo: RepoConfig, meta: dict, now_s: str) -> str | None:
        """GitHub redirects renamed and transferred repositories; never follow a transfer."""
        full = meta.get("full_name")
        if not full or full.lower() == repo.name.lower():
            return None
        if full.split("/")[0].lower() != repo.name.split("/")[0].lower():
            self.db.update_repo(repo.name, presence="transferred", state="transferred", last_checked_at=now_s,
                                state_detail=f"transferred to {full}; no longer processed")
            return "transferred"
        if repo.readme_auto:  # discovered: follow the rename and keep history, spending and pending work
            self.db.rename_repo(repo.name, full)
            return "renamed"
        self.db.update_repo(repo.name, state="renamed", last_checked_at=now_s,
                            state_detail=f"renamed to {full}; update the [[repo]] name in config.toml")
        return "renamed"

    def adopt_or_wait(self, repo: RepoConfig, snap: Snapshot, meta: dict, row, base: dict, quick: str,
                      missing: list[str], unmanaged: list[str], now: datetime) -> str:
        """Open (or refresh) one adoption pull request, in bounded daily batches; otherwise wait.
        Adoption always goes through a pull request, also for repositories in direct mode."""
        name = repo.name
        detail = "README missing: " + ", ".join(missing) if missing else "no managed blocks yet: " + ", ".join(unmanaged)
        live = self.db.live_proposal(name)
        latest = self.db.latest_proposal(name)
        refresh = live is not None and live["fingerprint"] == "adoption"
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        used = self.db.conn.execute("SELECT COUNT(*) FROM repos WHERE adopted_at >= ?", (day.isoformat(timespec="seconds"),)).fetchone()[0]
        why = None
        if not (self.cfg.discovery.enabled and self.cfg.discovery.adoptions_per_day) or repo.publish_mode == "preview":
            why = "review with `init --dry-run`"
        elif latest is not None and latest["fingerprint"] == "adoption" and latest["state"] == "rejected":
            why = "the adoption pull request was closed; run `init` to propose it again"
        elif not (meta.get("permissions") or {}).get("push"):
            why = "the GitHub credential cannot push to this repository"
        elif not refresh and used >= self.cfg.discovery.adoptions_per_day:
            tomorrow = (day + timedelta(days=1)).isoformat(timespec="seconds")
            self.db.update_repo(name, state="adoption_deferred", quick_key=quick, **base)
            self.db.update_repo(name, next_attempt_at=tomorrow,
                                state_detail=f"{detail}; daily adoption batch ({self.cfg.discovery.adoptions_per_day}) used, next batch {tomorrow}")
            return "adoption_deferred"
        if why:
            self.db.update_repo(name, state="needs_init", state_detail=f"{detail}; {why}", quick_key=quick, **base)
            return "needs_init"
        det = self.deterministic(snap, repo)
        files, hashes, olds = {}, {}, {}
        for path, (old, new) in adoption(repo, snap).items():
            text = apply(new, det).text if det else new  # show real structure/release content, not placeholders
            if text != old:
                files[path] = (text, snap.readmes[path][1])
                hashes[path] = {"sections": {b.id: b.hash for b in parse(text)}}
                olds[path] = old
        if not files:
            self.db.update_repo(name, state="no_changes", quick_key=quick, **base,
                                state_detail="every standard section the monitor could maintain here is written by people; nothing to adopt")
            return "no_changes"
        self._check_cancel("before publishing the adoption")
        flags = ["Initial adoption: review the markers and wrapped sections. Text outside the markers is unchanged."]
        did = self.db.add_draft(self.run_id, name, "adoption",
                                {p: {"old": olds[p], "new": t, "diff": unified_diff(olds[p], t, p), "changed": [], "conflicts": []}
                                 for p, (t, _) in files.items()}, flags, [detail])
        self.emit("validate", "done", f"{name}: one-time adoption of {', '.join(files)}", revision=snap.head_sha,
                  changed={p: [] for p in files}, flags=flags, notes=[detail], draft_id=did)
        pid = self.db.create_proposal(name, "adoption", snap.head_sha, repo.publish_mode, "publishing", hashes, ["initial adoption"])
        self.db.link_draft(did, pid)
        self.emit("publish", "started", f"{name}: opening the adoption pull request", mode=repo.publish_mode, branch=snap.branch, proposal_id=pid)
        try:
            pr = self.publisher.publish(repo, snap.head_sha, snap.branch, files, pid, {p: [] for p in files}, {}, flags)
        except BaseException as e:
            self.db.update_proposal(pid, state="failed", detail=str(e)[:300])
            raise
        self.emit("publish", "done", f"{name}: adoption pull request #{pr['number']} accepted by GitHub", mode=repo.publish_mode,
                  branch=snap.branch, proposal_id=pid, pr_number=pr["number"], pr_url=pr["html_url"])
        self.emit("await", "info", f"{name}: waiting for a person to merge the adoption pull request", waiting_for="merge",
                  pr_url=pr["html_url"], since=now.isoformat(timespec="seconds"))
        self.db.update_repo(name, state="awaiting_merge", state_detail=pr["html_url"], quick_key=quick,
                            **({} if refresh else {"adopted_at": now.isoformat(timespec="seconds")}), **base)
        return "awaiting_merge"

    def needs_paid_generation(self, repo: RepoConfig, snap: Snapshot, llm_fp: str, row) -> bool:
        if repo.api_export != "allowlist" or not repo.llm_sections or row["generated_llm_fp"] == llm_fp:
            return False
        return any(s in {b.id for b in parse(t)} for t, _ in snap.readmes.values() if t for s in repo.llm_sections)

    def generation_wait(self, name: str, row, pending: str, changed_at: str, now: datetime) -> tuple[str, str, str | None] | None:
        """Why paid generation should wait, as (state, detail, retry_at), or None to go ahead.
        Coalesces bursts of commits, spaces paid updates per repository, and serves the oldest
        pending repository first when money is short. Pending work is never dropped."""
        cfg = self.cfg
        postponed = now - datetime.fromisoformat(pending) >= timedelta(hours=cfg.max_postpone_hours)
        quiet_until = datetime.fromisoformat(changed_at) + timedelta(minutes=cfg.quiet_minutes)
        if quiet_until > now and not postponed:
            return ("generation_deferred", f"waiting for {cfg.quiet_minutes} quiet minutes after the last change "
                    f"(pending since {pending})", quiet_until.isoformat(timespec="seconds"))
        if row["last_generated_at"]:
            allowed = datetime.fromisoformat(row["last_generated_at"]) + timedelta(hours=cfg.min_generation_interval_hours)
            if allowed > now:
                return ("generation_deferred", f"at most one paid update per {cfg.min_generation_interval_hours:g} h "
                        f"per repository; next {allowed.isoformat(timespec='seconds')} (pending since {pending})",
                        allowed.isoformat(timespec="seconds"))
        older = next((r for r in self.db.conn.execute(
            "SELECT name, pending_since, state_detail FROM repos WHERE state='budget_paused' AND pending_since < ? AND name <> ? "
            "ORDER BY pending_since", (pending, name))
            if (self.active is None or r["name"] in self.active) and not _own_cap(r)), None)
        if older:  # only repositories still being processed can hold the queue
            return ("budget_paused", f"queued behind {older['name']} (waiting for budget since {older['pending_since']})", None)
        return None

    def _fail(self, name: str, row, state: str, e: Exception) -> str:
        n = (row["consecutive_errors"] or 0) + 1
        delay = min(self.cfg.poll_interval_minutes * 2 ** n, 360)
        msg, _ = redact(f"{type(e).__name__}: {e}")
        self.db.record_error(name, state, msg)
        self.db.update_repo(name, state=state, state_detail=msg[:300], consecutive_errors=n,
                            next_attempt_at=(_utc() + timedelta(minutes=delay)).isoformat(timespec="seconds"),
                            last_checked_at=_utc().isoformat(timespec="seconds"))
        log.warning("%s: %s (retry in %d min)", name, msg, delay)
        return state

    def _section_hashes(self, prep: Prepared) -> dict:
        return {p: {"sections": {b.id: b.hash for b in parse(new)}} for p, (_, new, _) in prep.files.items()}

    def propose(self, repo: RepoConfig, prep: Prepared, draft_id: int | None = None) -> str:
        snap = prep.snap
        files = {p: (new, blob) for p, (old, new, blob) in prep.files.items() if old != new}
        if repo.publish_mode == "preview":
            pid = self.db.create_proposal(repo.name, prep.doc_fp, snap.head_sha, "preview", "preview", self._section_hashes(prep), prep.flags)
            if draft_id:
                self.db.link_draft(draft_id, pid)
            self.write_preview(repo, prep)
            self.db.update_repo(repo.name, state="proposal_created", state_detail=f"preview #{pid} written to {self.preview_dir(repo)}")
            return "proposal_created"
        self._check_cancel("before publishing")
        pid = self.db.create_proposal(repo.name, prep.doc_fp, snap.head_sha, repo.publish_mode, "publishing", self._section_hashes(prep), prep.flags)
        if draft_id:
            self.db.link_draft(draft_id, pid)
        direct = repo.publish_mode == "direct" and not prep.flags
        self.emit("publish", "started", f"{repo.name}: " + (f"committing to {snap.branch}" if direct else "opening or updating the pull request"),
                  revision=snap.head_sha, mode=repo.publish_mode, branch=snap.branch, proposal_id=pid)
        res = self.publisher.publish(repo, snap.head_sha, snap.branch, files, pid, {p: prep.changes[p] for p in files}, prep.conflicts, prep.flags)
        now_s = _utc().isoformat(timespec="seconds")
        if res.get("direct"):
            self.emit("publish", "done", f"{repo.name}: GitHub accepted the commit to {snap.branch}", revision=snap.head_sha,
                      mode="direct", branch=snap.branch, proposal_id=pid, commit_sha=res["commit_sha"], commit_url=res["html_url"])
            if res["confirmed"]:
                self.db.update_repo(repo.name, state="published", last_published_at=now_s,
                                    state_detail=f"committed directly to {snap.branch} and read back")
                return "published"
            self.db.update_repo(repo.name, state="pushed", state_detail=f"committed directly to {snap.branch}; verified on the next cycle")
            return "pushed"
        detail = self.db.proposal(pid)["detail"]
        self.emit("publish", "done", f"{repo.name}: pull request #{res['number']} accepted by GitHub", revision=snap.head_sha,
                  mode=repo.publish_mode, branch=snap.branch, proposal_id=pid, pr_number=res["number"], pr_url=res["html_url"],
                  auto_merge=detail)
        self.emit("await", "info", f"{repo.name}: waiting for a person to merge the pull request" + (f" ({detail})" if detail else ""),
                  revision=snap.head_sha, waiting_for="merge", pr_url=res["html_url"], since=now_s)
        self.db.update_repo(repo.name, state="awaiting_merge", state_detail=res["html_url"])
        return "awaiting_merge"

    def preview_dir(self, repo: RepoConfig) -> Path:
        return self.home / "previews" / repo.name.replace("/", "__")

    def write_preview(self, repo: RepoConfig, prep: Prepared, suffix: str = "") -> Path:
        out = self.preview_dir(repo)
        out.mkdir(parents=True, exist_ok=True)
        for path, (old, new, _) in prep.files.items():
            safe = path.replace("/", "__")
            (out / f"{safe}{suffix}").write_text(new, encoding="utf-8", newline="")
            (out / f"{safe}{suffix}.diff").write_text(unified_diff(old, new, path), encoding="utf-8")
        for path, payload in prep.payloads.items():
            (out / f"{path.replace('/', '__')}.payload.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return out

    def run(self) -> dict:
        started = time.monotonic()
        summary = self._cycle()
        what = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(summary.items())) or "nothing to check"
        label = "Dry run" if self.dry_run else "Check"
        self.emit("run", "done", f"{label} {'cancelled' if self.cancelled else 'finished'}: {what}", summary=summary,
                  seconds=round(time.monotonic() - started, 1), repos=sum(summary.values()), dry_run=self.dry_run)
        return summary

    def _cycle(self) -> dict:
        summary: dict[str, int] = {}
        if not self.dry_run:
            recovered = self.db.recover_reservations(self.run_id)
            if recovered:
                log.warning("%d reservation(s) from an interrupted run kept as ambiguous spend", recovered)
            if self.db.globally_paused:
                return {"paused": len(discovery.effective_repos(self.cfg, self.db))}
            if discovery.discovery_due(self.db, self.cfg):
                try:
                    found = discovery.refresh(self.db, self.cfg, self.gh)
                    summary.update({f"discovered_{k}": v for k, v in found.items() if v})
                    cov = json.loads(self.db.get_setting("discovery", "{}") or "{}")
                    self.emit("discover", "done", f"Repository list refreshed: {found.get('present', 0)} owned, {found.get('new', 0)} new",
                              listed=found.get("present"), new=found.get("new"), renamed=found.get("renamed"),
                              missing=found.get("missing"), transferred=found.get("transferred"), complete=cov.get("complete"))
                except GitHubError as e:  # keep the previous inventory; nothing is marked missing
                    log.warning("discovery skipped: %s", redact(str(e))[0])
                    summary["discovery_failed"] = 1
                    self.emit("discover", "failed", f"Repository list not refreshed; the previous one is kept: {e}")
        repos = discovery.effective_repos(self.cfg, self.db)
        if self.only is not None:
            repos = [r for r in repos if r.name.lower() in self.only]
        self.active = {r.name for r in repos}
        rows = {r["name"]: r for r in self.db.repos()}
        now_s = _utc().isoformat(timespec="seconds")
        # Repositories waiting for a retry time do not take a slot, so failures cannot crowd out
        # the rest; everything else rotates least-recently-checked first (stored, so it survives restarts).
        waiting = [r for r in repos if not self.dry_run and (rows.get(r.name) or {"next_attempt_at": None})["next_attempt_at"]
                   and rows[r.name]["next_attempt_at"] > now_s]
        if waiting:
            summary["backoff"] = len(waiting)
        due = [r for r in repos if r not in waiting]
        order = sorted(due, key=lambda r: (rows.get(r.name) or {"last_checked_at": None})["last_checked_at"] or "")
        batch = order[: self.cfg.max_repos_per_run]
        for i, repo in enumerate(batch):
            if self.db.cancel_requested(self.run_id, self.launch):
                self.cancelled = True
                summary["cancelled"] = summary.get("cancelled", 0) + len(batch) - i
                break
            if time.monotonic() > self.deadline:
                summary["deferred_timeout"] = summary.get("deferred_timeout", 0) + 1
                continue
            if self.gh.quota_low:
                summary["deferred_rate_limit"] = summary.get("deferred_rate_limit", 0) + 1
                continue
            try:
                state = self.dry_process(repo) if self.dry_run else self.process(repo)
            except RateLimited:
                state = "rate_limited"
            summary[state] = summary.get(state, 0) + 1
        if len(order) > self.cfg.max_repos_per_run:
            summary["queued_next_cycle"] = len(order) - self.cfg.max_repos_per_run
            for pos, repo in enumerate(order[self.cfg.max_repos_per_run:], 1):
                self.emit("queue", "info", f"{repo.name}: queued for the next cycle (position {pos})", repo=repo.name,
                          position=pos, reason="queued_next_cycle")
        return summary


def _own_cap(row) -> bool:
    """A repository stopped by its own per-repository limit cannot be funded from the shared budget,
    so it must not hold the shared queue. Matches the label llm.Generator.limits gives those limits."""
    detail, name = row["state_detail"] or "", row["name"]
    return detail.startswith((f"{name} daily limit ", f"{name} monthly limit "))


def adoption(repo: RepoConfig, snap: Snapshot) -> dict[str, tuple[str, str]]:
    """Migration text per README path: {path: (old, new)}."""
    out = {}
    for path, (text, _) in snap.readmes.items():
        # Discovered repositories only gain missing sections; existing hand-written ones stay with people.
        new = migrate(text, repo.name, snap.description, repo.managed_sections, repo.disabled_sections,
                      minimal=repo.readme_auto, take_over=not repo.readme_auto)
        out[path] = (text or "", new)
    return out
