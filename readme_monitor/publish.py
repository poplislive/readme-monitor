"""Publication: documentation pull requests on one monitor-owned branch per repository, or, for a
repository that opted in with publish_mode = "direct", a non-force commit to the target branch."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from . import __version__, runtime
from .config import Config, RepoConfig
from .github import GitHub, GitHubError, RateLimited
from .readme import parse, MarkerError

log = logging.getLogger(__name__)
TRAILER = "Readme-Monitor-Proposal"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Stale(Exception):
    """The target branch moved after analysis; re-analyze instead of publishing."""


class Conflict(Exception):
    """Publishing would overwrite work the monitor does not own."""


class Blocked(Exception):
    """GitHub refused the write (branch protection, missing permission). Never retried around.
    `partial` is the last commit made in this call when earlier README paths were already written."""

    def __init__(self, message: str, partial: str | None = None):
        super().__init__(message)
        self.partial = partial


def _owned(gh: GitHub, db, repo: str, sha: str, bot_sha: str | None) -> bool:
    if sha == bot_sha:
        return True
    m = re.search(rf"^{TRAILER}: (\d+)$", gh.commit_message(repo, sha), re.M)
    if not m:
        return False
    p = db.proposal(int(m[1]))
    return p is not None and p["repo"] == repo


def pr_body(snap_sha: str, changes: dict[str, list[str]], conflicts: dict[str, list[str]], flags: list[str], mode: str) -> str:
    lines = [
        "ReadmeMonitor proposes this README update from the current state of the target branch.",
        "",
        f"- Analyzed revision: `{snap_sha}`",
        f"- Publishing mode: `{mode}`",
        f"- Generator: ReadmeMonitor {__version__}",
        "",
        "### Sections changed",
    ]
    for path, sections in changes.items():
        lines.append(f"- `{path}`: {', '.join(sections) if sections else 'adoption markers only'}")
    if conflicts:
        lines += ["", "### Not changed: edited by a person", ""]
        lines += [f"- `{p}`: {', '.join(s)} (edit outside the monitor; resolve by hand)" for p, s in conflicts.items() if s]
    lines += ["", "### Review notes", ""]
    lines += [f"- {f}" for f in flags] or ["- No review flags. Content cites supplied evidence only."]
    lines += ["", "Only text between `readme-monitor` markers is generated. Everything else in the README is left as it was."]
    return "\n".join(lines)


class Publisher:
    def __init__(self, cfg: Config, db, gh: GitHub, guard=None):
        self.cfg, self.db, self.gh = cfg, db, gh
        self.guard = guard  # the simulation passes a pass-through guard; production uses require_active

    def publish(self, repo: RepoConfig, base_sha: str, branch: str, files: dict[str, tuple[str, str | None]], pid: int,
                changes: dict, conflicts: dict, flags: list[str]) -> dict:
        """files: {path: (new_text, base_blob_sha)}. Returns the pull request, or for a direct commit
        {"direct": True, "commit_sha", "html_url", "confirmed"}."""
        home = self.db.path.parent  # every GitHub write path goes through here
        if self.guard is not None:
            self.guard(home)
        else:
            runtime.require_active(home, self.cfg.host_lock)
        name, bot = repo.name, self.cfg.bot_branch
        for path in files:
            if path not in repo.readme_paths:
                raise Conflict(f"refusing to write {path!r}: not a configured README path")
        # The README blobs were read from base_sha, so an unchanged head means unchanged content.
        if self.gh.head_sha(name, branch) != base_sha:
            raise Stale(f"{name}@{branch} moved after analysis")
        p = self.db.proposal(pid)
        adoption = p is not None and p["fingerprint"] == "adoption"  # adoption always goes through review
        if repo.publish_mode == "direct" and not flags and not adoption:
            return self._direct(repo, base_sha, branch, files, pid)
        row = self.db.repo(name)
        current = self.gh.ref_sha(name, bot)
        if current is None:
            self.gh.create_ref(name, bot, base_sha)
        elif current != base_sha:
            if not _owned(self.gh, self.db, name, current, row["bot_sha"]):
                raise Conflict(f"branch {bot} has commits the monitor did not write; it will not be overwritten")
            self.gh.move_ref(name, bot, base_sha)
        for path, (text, blob) in files.items():
            msg = f"docs: update {path} (ReadmeMonitor)\n\nAnalyzed revision {base_sha}.\n\n{TRAILER}: {pid}"
            sha = self.gh.put_file(name, path, bot, text, msg, blob)
            self.db.update_repo(name, bot_sha=sha)
        owner = name.split("/")[0]
        title = "docs: update README (ReadmeMonitor)"
        body = pr_body(base_sha, changes, conflicts, flags, repo.publish_mode)
        pr = self.gh.find_open_pr(name, owner, bot, branch)
        pr = self.gh.update_pr(name, pr["number"], title, body) if pr else self.gh.create_pr(name, title, bot, branch, body)
        detail = None
        if repo.publish_mode == "auto_pr":
            if flags:
                detail = "auto-merge withheld: content has review flags"
            else:
                try:
                    self.gh.enable_auto_merge(pr["node_id"])
                    detail = "auto-merge enabled; GitHub merges after required reviews and checks pass"
                except GitHubError as e:
                    detail = f"auto-merge unavailable ({str(e)[:160]}); human merge required"
        elif repo.publish_mode == "direct" and not adoption:
            detail = "direct commit withheld: content has review flags"
        self.db.update_proposal(pid, state="awaiting_merge", pr_number=pr["number"], pr_url=pr["html_url"], detail=detail)
        return pr

    def _verified(self, name: str, branch: str, pid: int, sha: str | None) -> dict:
        head = self.gh.head_sha(name, branch)
        self.db.update_proposal(pid, state="published", published_sha=head, detail="committed directly; verified on the target branch")
        sha = sha or head
        return {"direct": True, "commit_sha": sha, "html_url": f"https://github.com/{name}/commit/{sha}", "confirmed": True}

    def _direct(self, repo: RepoConfig, base_sha: str, branch: str, files: dict, pid: int) -> dict:
        """One non-force Contents API commit per README path, each pinned to the expected blob and
        made only while the branch head is still the analyzed revision (or our own previous commit).
        Never a ref move, never a force update."""
        name, expected, sha, written = repo.name, base_sha, None, []
        hashes = json.loads(self.db.proposal(pid)["files"])
        for path, (text, blob) in files.items():
            if self.gh.head_sha(name, branch) != expected:
                raise Stale(f"{name}@{branch} moved after analysis")
            msg = f"docs: update {path} (ReadmeMonitor)\n\nAnalyzed revision {base_sha}.\n\n{TRAILER}: {pid}"
            try:
                sha = self.gh.put_file(name, path, branch, text, msg, blob)
            except GitHubError as e:
                # The write may have landed (lost answer, or a retried request answered 409):
                # look before deciding, so nothing is ever committed twice.
                if self._confirmed(name, branch, hashes):
                    return self._verified(name, branch, pid, None)
                if e.status in (403, 409, 422) and not isinstance(e, RateLimited):
                    if self.gh.head_sha(name, branch) != expected:
                        raise Stale(f"{name}@{branch} moved after analysis") from None
                    if written:  # earlier paths are on the branch: say so and record the commit
                        why = (f"committed {', '.join(written)} to {branch} ({sha[:12]}), then GitHub refused {path} (HTTP {e.status}): "
                               "branch protection or a missing permission; switch this repository to review_pr or allow the commit on GitHub")
                        self.db.update_proposal(pid, state="failed", published_sha=sha, detail=why[:300])
                        raise Blocked(why, partial=sha) from None
                    why = (f"GitHub refused the direct commit to {branch} (HTTP {e.status}): branch protection or a missing "
                           "permission. Nothing was written; switch this repository to review_pr or allow the commit on GitHub")
                    self.db.update_proposal(pid, state="failed", detail=why[:300])
                    raise Blocked(why) from None
                self.db.update_proposal(pid, detail="direct commit outcome unknown; checked on the next run before any retry")
                raise
            expected = sha
            written.append(path)
        if self._confirmed(name, branch, hashes):
            return self._verified(name, branch, pid, sha)
        self.db.update_proposal(pid, detail="committed directly; not yet visible on the target branch")
        return {"direct": True, "commit_sha": sha, "html_url": f"https://github.com/{name}/commit/{sha}", "confirmed": False}

    def reconcile(self, repo: RepoConfig, branch: str) -> str | None:
        """Advance a live proposal from GitHub's actual state. Returns the new repo state or None."""
        p = self.db.live_proposal(repo.name)
        if p is None:
            return None
        if p["state"] == "publishing" and p["mode"] == "direct" and self._confirmed(repo.name, branch, json.loads(p["files"])):
            # A direct commit whose answer was lost is already there: record it, never make it again.
            self.db.update_proposal(p["id"], state="published", published_sha=self.gh.head_sha(repo.name, branch),
                                    detail="committed directly; verified on the target branch")
            return "published"
        if p["state"] == "publishing":
            # Interrupted between writes; the next analysis republishes idempotently
            # (same bot branch, existing PR found and updated; a direct commit is re-checked above).
            self.db.update_proposal(p["id"], state="failed", detail="interrupted while publishing; retried on the next run")
            return "retry"
        pr = self.gh.pr(repo.name, p["pr_number"])
        if pr.get("merged"):
            head = self.gh.head_sha(repo.name, branch)
            if self._confirmed(repo.name, head, json.loads(p["files"])):
                self.db.update_proposal(p["id"], state="published", published_sha=head)
                return "published"
            merged_at = datetime.fromisoformat((pr.get("merged_at") or _now_iso()).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - merged_at > timedelta(minutes=max(self.cfg.check_wait_minutes, 10)):
                # The merge never produced the proposed README (e.g. overwritten); start over.
                self.db.update_proposal(p["id"], state="failed", detail="merged, but the proposed content never reached the target branch")
                return "retry"
            self.db.update_proposal(p["id"], detail="merged; waiting to see the proposed README on the target branch")
            return "merged_unconfirmed"
        if pr.get("state") == "closed":
            self.db.update_proposal(p["id"], state="rejected", detail="pull request closed without merge")
            return "rejected"
        return "awaiting_merge"

    def _confirmed(self, name: str, ref: str, files: dict) -> bool:
        for path, info in files.items():
            text, _ = self.gh.file_at(name, path, ref)
            if text is None:
                return False
            try:
                blocks = {b.id: b for b in parse(text)}
            except MarkerError:
                return False
            for sid, h in info["sections"].items():
                b = blocks.get(sid)
                if b is None or b.hash != h or b.human_edited:
                    return False
        return True
