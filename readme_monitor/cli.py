"""Command-line interface: readme-monitor <command>."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import logging.handlers
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import __version__, credentials, discovery, runtime, scheduler
from .config import Config, ConfigError, default_home, load
from .db import DB, check_integrity
from .github import GitHub, GitHubError, NotFound
from .llm import Generator, LLMError, pace_limit
from .publish import Conflict, Publisher, Stale
from .readme import parse, unified_diff
from .worker import Locked, Worker, adoption, file_lock

log = logging.getLogger("readme_monitor")
EXAMPLE_CONFIG = Path(__file__).resolve().parent / "templates" / "config.example.toml"


class Ctx:
    def __init__(self, args):
        self.args = args
        self.home: Path = Path(args.home).resolve() if args.home else default_home()
        self.config_path = Path(args.config).resolve() if args.config else self.home / "config.toml"
        self._cfg = self._db = None

    @property
    def cfg(self) -> Config:
        if self._cfg is None:
            self._cfg = load(self.config_path)
        return self._cfg

    @property
    def db(self) -> DB:
        if self._db is None:
            self._db = DB(self.home / "state.db")
        return self._db

    def host_lock(self) -> str | None:
        return (self.cfg.host_lock or None) if self.config_path.exists() else None

    def require_active(self):
        """Fails before any credential is read, money is reserved or anything is written."""
        runtime.require_active(self.home, self.host_lock())
        if os.path.normcase(str(self.config_path)) != os.path.normcase(str(self.home / "config.toml")):
            raise runtime.NotActivated(f"live work reads only {self.home / 'config.toml'}; --config points elsewhere")

    def github(self) -> GitHub:
        token, _ = credentials.get("github")
        if not token:
            raise SystemExit("GitHub credential not found for this Windows account. Run: readme-monitor set-credential github")
        return GitHub(token, db=self.db)

    def generator(self) -> Generator:
        key, _ = credentials.get("anthropic")
        return Generator(self.cfg, self.db, key)


def setup_logging(home: Path, verbose: bool, quiet: bool):
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    redactor = credentials.RedactingFilter()
    fh = logging.handlers.RotatingFileHandler(logs / "readme-monitor.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    fh.addFilter(redactor)
    root.addHandler(fh)
    if not quiet and sys.stderr is not None:  # pythonw has no console
        sh = logging.StreamHandler()
        sh.setLevel(logging.DEBUG if verbose else logging.WARNING)
        sh.addFilter(redactor)
        root.addHandler(sh)
    for noisy in ("urllib3", "httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def out(*a):
    if sys.stdout is not None:
        print(*a, flush=True)


def _repos(ctx: Ctx, name: str | None):
    return [discovery.find_repo(ctx.cfg, ctx.db, name)] if name else discovery.effective_repos(ctx.cfg, ctx.db)


def _snapshot(worker: Worker, repo):
    """(snapshot, repository settings with the README path resolved)."""
    meta, branch, head = worker._head(repo)
    releases = worker.gh.releases(repo.name) if "recent-changes" in repo.deterministic_sections else []
    return worker.snapshot(repo, meta, branch, head, releases)


def _worker(ctx: Ctx, trigger: str, **kw) -> Worker:
    gh = ctx.github()  # fails before a run record exists
    gen = Generator(ctx.cfg, ctx.db, None) if kw.get("dry_run") else ctx.generator()  # a dry run never reads the Anthropic key
    run_id = ctx.db.start_run(trigger, scheduler.current_user())
    return Worker(ctx.cfg, ctx.db, gh, gen, ctx.home, run_id, trigger=trigger, **kw)


def installation(ctx: Ctx) -> dict:
    lock = None
    try:
        lock = ctx.host_lock()
    except ConfigError:
        pass
    return {"home": str(ctx.home), "host": runtime.hostname(), "account": scheduler.current_user(), "platform": sys.platform,
            "supported": runtime.WINDOWS, "active": runtime.activation_problem(ctx.home, lock) is None,
            "problem": runtime.activation_problem(ctx.home, lock), "activation": runtime.activation(ctx.home), "host_lock": lock}


# ---- commands ---------------------------------------------------------------


def cmd_doctor(ctx: Ctx) -> int:
    results = doctor_checks(ctx, ctx.args.api)
    width = max(len(n) for _, n, _ in results)
    for status, name, detail in results:
        out(f"[{status}] {name.ljust(width)}  {detail}")
    return 1 if any(s == "FAIL" for s, _, _ in results) else 0


def doctor_checks(ctx: Ctx, api: bool = False) -> list[tuple[str, str, str]]:
    """(status, name, detail) rows; shared by `doctor` and the setup interface. Never contains a secret."""
    results: list[tuple[str, str, str]] = []

    def check(name, ok, detail, warn=False):
        results.append(("PASS" if ok else ("WARN" if warn else "FAIL"), name, detail))

    check("python", sys.version_info >= (3, 11), sys.version.split()[0])
    check("account", True, scheduler.current_user())
    inst = installation(ctx)
    check("installation", inst["active"], "active on this computer" if inst["active"] else inst["problem"], warn=True)
    check("home", os.access(ctx.home, os.W_OK) if ctx.home.exists() else False, str(ctx.home))
    cfg = None
    try:
        cfg = ctx.cfg
        check("config", True, f"{ctx.config_path} ({len(cfg.repos)} enrolled)")
    except ConfigError as e:
        check("config", False, str(e))
    db_path = ctx.home / "state.db"
    if db_path.exists():
        check("database", check_integrity(db_path) == "ok", str(db_path))
    else:
        check("database", True, f"{db_path} (created on first run)")
    try:
        with file_lock(ctx.home / "worker.lock"):
            check("worker lock", True, "free")
    except Locked:
        check("worker lock", True, "held by a running worker", warn=True)
    gh_token, gh_src = credentials.get("github")
    check("github credential", bool(gh_token), gh_src)
    key, key_src = credentials.get("anthropic")
    check("anthropic credential", bool(key), key_src, warn=True)
    if cfg:
        check("budget", cfg.budget.configured,
              f"per run ${cfg.budget.per_run_usd}, daily ${cfg.budget.daily_usd}, monthly ${cfg.budget.monthly_usd}"
              if cfg.budget.configured else "not configured: paid generation stays paused", warn=True)
        check("model pricing", cfg.price() is not None, cfg.model)
    if gh_token:
        gh = GitHub(gh_token)
        try:
            who = gh.whoami()
            check("github auth", True, f"{who['login']}; scopes: {gh.scopes or 'fine-grained or app token (no scope header)'}")
        except GitHubError as e:
            check("github auth", False, str(e))
            gh = None
        for repo in (cfg.repos if cfg and gh else ()):
            try:
                meta = gh.repo(repo.name)
                push = (meta.get("permissions") or {}).get("push")
                need_push = repo.publish_mode != "preview"
                check(f"repo {repo.name}", push or not need_push,
                      f"{meta['visibility']}, default {meta['default_branch']}, mode {repo.publish_mode}, push {'yes' if push else 'no'}"
                      + (f", auto-merge {'allowed' if meta.get('allow_auto_merge') else 'disabled in repo settings'}" if repo.publish_mode == "auto_pr" else ""))
            except GitHubError as e:
                check(f"repo {repo.name}", False, str(e))
    if api and key and cfg:
        try:
            import anthropic

            m = anthropic.Anthropic(api_key=key, timeout=30, max_retries=0).models.retrieve(cfg.model)
            check("model available", True, f"{m.id} (model lookup only; no tokens used)")
        except Exception as e:  # report, never print the key
            check("model available", False, f"{cfg.model}: {type(e).__name__}")
    task = scheduler.query()
    check("scheduled task", task is not None, json.dumps(task) if task else "not installed", warn=True)
    return results


def cmd_init(ctx: Ctx) -> int:
    home_cfg = ctx.home / "config.toml"
    if not ctx.config_path.exists():
        if ctx.args.dry_run:
            out(f"Would create {home_cfg} from the example configuration.")
            return 0
        ctx.home.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE_CONFIG, home_cfg)
        out(f"Created {home_cfg}. Edit it to enroll repositories, then run: readme-monitor init --dry-run")
        return 0
    if not ctx.args.dry_run:
        ctx.require_active()
        try:
            with file_lock(ctx.home / "worker.lock"):
                return _init(ctx)
        except Locked as e:
            out(f"Skipped: {e}")
            return 1
    return _init(ctx)


def _init(ctx: Ctx) -> int:
    worker = _worker(ctx, "init")
    status = 0
    for repo in _repos(ctx, ctx.args.repo):
        try:
            snap, repo = _snapshot(worker, repo)
        except GitHubError as e:
            out(f"{repo.name}: cannot read repository: {e}")
            status = 1
            continue
        plans = adoption(repo, snap)
        changed = {p: (o, n) for p, (o, n) in plans.items() if o != n}
        if not changed:
            out(f"{repo.name}: already adopted; nothing to migrate.")
            continue
        for path, (old, new) in changed.items():
            out(unified_diff(old, new, path) or f"(new file {path})")
        if ctx.args.dry_run:
            out(f"{repo.name}: dry run only. Nothing was written.")
            continue
        if repo.publish_mode == "preview":
            for path, (_, new) in changed.items():
                dest = worker.preview_dir(repo) / (path.replace("/", "__") + ".adoption.md")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(new, encoding="utf-8")
                out(f"{repo.name}: adoption written to {dest}")
            continue
        files = {p: (n, snap.readmes[p][1]) for p, (_, n) in changed.items()}
        hashes = {p: {"sections": {b.id: b.hash for b in parse(n)}} for p, (_, n) in changed.items()}
        pid = ctx.db.create_proposal(repo.name, "adoption", snap.head_sha, repo.publish_mode, "publishing", hashes, ["initial adoption"])
        try:
            pr = Publisher(ctx.cfg, ctx.db, worker.gh).publish(repo, snap.head_sha, snap.branch, files, pid, {p: [] for p in files}, {}, ["Initial adoption: review the markers and wrapped sections."])
            ctx.db.update_repo(repo.name, state="awaiting_merge", state_detail=pr["html_url"])
            out(f"{repo.name}: adoption pull request {pr['html_url']}")
        except (Stale, Conflict, GitHubError) as e:
            ctx.db.update_proposal(pid, state="failed", detail=str(e)[:300])
            out(f"{repo.name}: adoption not published: {e}")
            status = 1
    ctx.db.finish_run(worker.run_id, "ok" if status == 0 else "error", {"command": "init", "dry_run": ctx.args.dry_run})
    return status


def cmd_preview(ctx: Ctx) -> int:
    if not ctx.args.api:
        return _preview(ctx)
    ctx.require_active()  # paid: same activated folder, lock and ledger as the scheduled worker
    try:
        with file_lock(ctx.home / "worker.lock"):
            return _preview(ctx)
    except Locked as e:
        out(f"Skipped: {e}")
        return 1


def _preview(ctx: Ctx) -> int:
    repo = discovery.find_repo(ctx.cfg, ctx.db, ctx.args.repo)
    worker = _worker(ctx, "preview-api" if ctx.args.api else "preview")
    try:
        snap, repo, prep, adopted = worker.preview(repo, use_llm=ctx.args.api)
    except LLMError as e:
        out(f"Claude generation not performed: {e}")
        ctx.db.finish_run(worker.run_id, e.kind, {"command": "preview"})
        return 1
    for path in adopted:
        out(f"{path}: not adopted yet; previewing on top of the adoption migration.")
    dest = worker.write_preview(repo, prep, suffix=".preview")
    out(f"Repository: {repo.name}@{snap.branch} {snap.head_sha[:12]}")
    out(f"Mode: {'API-backed preview (Claude called if inputs changed)' if ctx.args.api else 'offline preview (no Claude request)'}")
    for path, sections in prep.llm_requested.items():
        est = worker.gen.estimate(prep.payloads[path]) if ctx.cfg.price() else None
        out(f"{path}: Claude sections {', '.join(sections)}; payload {len(json.dumps(prep.payloads[path])):,} chars"
            + (f"; maximum reserved cost ${est:.4f}" if est is not None else ""))
    for path, sections in prep.changes.items():
        out(f"{path}: sections that would change: {', '.join(sections) or 'none'}")
    for path, sections in prep.conflicts.items():
        out(f"{path}: edited by a person, left unchanged: {', '.join(sections)}")
    for f in prep.flags:
        out(f"review: {f}")
    for n in prep.notes:
        out(f"note: {n}")
    out(f"Preview files: {dest}")
    ctx.db.finish_run(worker.run_id, "ok", {"command": "preview", "api": ctx.args.api})
    return 0


def _only(ctx: Ctx) -> list[str] | None:
    return [discovery.find_repo(ctx.cfg, ctx.db, n).name for n in ctx.args.repo] if ctx.args.repo else None


def _dry_run(ctx: Ctx) -> int:
    """Reads GitHub only: no model or token-counting request, no GitHub write, no checkpoint
    change. Works on an installation that is not activated and takes no worker lock."""
    worker = _worker(ctx, "dry_run", dry_run=True, only=_only(ctx))
    summary = worker.run()
    ctx.db.finish_run(worker.run_id, "cancelled" if worker.cancelled else "ok", {**summary, "dry_run": True})
    out(json.dumps(summary))
    return 0


def cmd_run_once(ctx: Ctx) -> int:
    if ctx.args.dry_run:
        return _dry_run(ctx)
    ctx.require_active()
    try:
        # wait: the setup interface probes this lock for an instant to see whether a run is active
        with file_lock(ctx.home / "worker.lock", wait=0.5):
            try:
                worker = _worker(ctx, ctx.args.trigger, only=_only(ctx))
            except SystemExit as e:
                run_id = ctx.db.start_run(ctx.args.trigger, scheduler.current_user())
                ctx.db.finish_run(run_id, "credentials_missing", {"error": str(e)})
                ctx.db.add_event(run_id, ctx.args.trigger, None, "run", "failed", "Check did not start: a credential is missing",
                                 {"outcome": "credentials_missing"})  # add_event redacts
                raise
            started = time.monotonic()
            try:
                summary = worker.run()
                outcome = "cancelled" if worker.cancelled else ("error" if any(k in summary for k in ("error", "inaccessible")) else "ok")
            except Exception as e:
                ctx.db.record_error(None, "run", credentials.redact(f"{type(e).__name__}: {e}")[0])
                ctx.db.finish_run(worker.run_id, "crashed", {"error": type(e).__name__})
                ctx.db.add_event(worker.run_id, ctx.args.trigger, None, "run", "failed",
                                 f"Check stopped by an unexpected error ({type(e).__name__}); see the log in the runtime folder",
                                 {"outcome": "crashed", "error": type(e).__name__})
                raise
            finally:
                ctx.db.clear_cancel()
            summary["seconds"] = round(time.monotonic() - started, 1)
            ctx.db.finish_run(worker.run_id, outcome, summary)
            out(json.dumps(summary))
            return 0 if outcome in ("ok", "cancelled") else 1
    except Locked as e:
        out(f"Skipped: {e}")
        return 0


def _age(iso: str | None, now: datetime) -> str | None:
    if not iso:
        return None
    minutes = int((now - datetime.fromisoformat(iso)).total_seconds() // 60)
    return f"{minutes // 1440}d {minutes % 1440 // 60}h" if minutes >= 1440 else f"{minutes // 60}h {minutes % 60}m"


def _read_rows(ctx: Ctx) -> list[dict]:
    """Stored repository rows without creating, migrating or locking anything."""
    path = ctx.home / "state.db"
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{quote(path.resolve().as_posix())}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM repos")]
    finally:
        conn.close()


def cmd_discover(ctx: Ctx) -> int:
    cfg = ctx.cfg
    if not cfg.discovery.enabled:
        out('Discovery is off. Set [discovery] owner = "<GitHub account>" in config.toml to list every owned repository.')
        return 2
    if not ctx.args.dry_run:
        ctx.require_active()
    token, _ = credentials.get("github")
    if not token:
        raise SystemExit("GitHub credential not found for this Windows account. Run: readme-monitor set-credential github")
    gh = GitHub(token)
    try:
        owned, coverage = discovery.list_owned(gh, cfg.discovery.owner)
    except GitHubError as e:
        out(f"Discovery incomplete, nothing recorded: {e}")
        return 1
    if ctx.args.dry_run:
        rows = _read_rows(ctx)
    else:
        try:
            with file_lock(ctx.home / "worker.lock"):
                summary = discovery.reconcile(ctx.db, cfg, gh, owned, coverage)
        except Locked as e:
            out(f"Skipped: {e}")
            return 1
        out("Recorded: " + ", ".join(f"{k} {v}" for k, v in summary.items()))
        rows = [dict(r) for r in ctx.db.repos()]
    inventory = discover_inventory(cfg, rows, owned)
    report = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "dry_run": ctx.args.dry_run,
              "coverage": coverage, "repositories": inventory}
    dest = ctx.home / "inventory" / f"inventory-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if ctx.args.json:
        out(json.dumps(report, indent=2))
        return 0
    out(f"{'DRY RUN: nothing recorded. ' if ctx.args.dry_run else ''}{coverage['listed']} repositories owned by {coverage['owner']} "
        f"({coverage['public']} public, {coverage['private']} private; {coverage['skipped_not_owned']} not owned and ignored)")
    out("Coverage: " + ("complete" if coverage["complete"] else "NOT PROVEN - " + "; ".join(coverage["limitations"])))
    counts: dict[str, int] = {}
    for item in inventory:
        counts[item["disposition"]] = counts.get(item["disposition"], 0) + 1
    out("Dispositions: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    width = max((len(i["name"]) for i in inventory), default=10)
    for i in inventory:
        out(f"  {i['name'].ljust(width)}  {i['visibility'] or '?':<7} {i['disposition']:<24} {i['detail']}")
    out(f"Private inventory written to {dest}")
    return 0


def discover_inventory(cfg: Config, rows: list[dict], owned: list[dict]) -> list[dict]:
    """One entry per owned repository with its disposition; shared by `discover` and the setup interface."""
    by_id = {r["repo_id"]: r for r in rows if r.get("repo_id")}
    by_name = {r["name"].lower(): r for r in rows}
    explicit = {r.name.lower(): r for r in cfg.repos}
    inventory = []
    for r in sorted(owned, key=lambda r: r["full_name"].lower()):
        stored = by_id.get(r["id"]) or by_name.get(r["full_name"].lower()) or {}
        row = {**stored, "name": r["full_name"], "meta": json.dumps(discovery._meta(r)), "presence": "present",
               "restricted": int(cfg.is_restricted(r["full_name"]) or bool(stored.get("restricted")))}
        repo = explicit.get(r["full_name"].lower()) or cfg.discovery.repo(r["full_name"])
        if row["restricted"]:
            repo = replace(repo, api_export="none")
        code, why = discovery.disposition(cfg, row, repo)
        inventory.append({"id": r["id"], "name": r["full_name"], "visibility": r.get("visibility"), "archived": r.get("archived"),
                          "fork": r.get("fork"), "default_branch": r.get("default_branch"),
                          "configured": r["full_name"].lower() in explicit, "known": bool(stored),
                          "renamed_from": stored.get("name") if stored and stored.get("name") != r["full_name"] else None,
                          "disposition": code, "detail": why})
    return inventory


def cmd_activate(ctx: Ctx) -> int:
    data = runtime.activate(ctx.home, scheduler.current_user(), ctx.host_lock())
    out(f"Worker enabled on {data['host']} for {data['home']} (account {data['account']}, since {data['activated_at']}).")
    out("This is the active runtime folder for this Windows account; commands without --home use it.")
    return 0


def cmd_bind_host(ctx: Ctx) -> int:
    out("Note: bind-host is deprecated; it now runs `readme-monitor activate`.")
    return cmd_activate(ctx)


def cmd_deactivate(ctx: Ctx) -> int:
    r = runtime.deactivate(ctx.home)
    out(("Scheduled task removed. " if r["task_removed"] else "") +
        (f"Activation revoked ({r['revoked']})." if r["revoked"] else "This folder was not activated."))
    out(f"All data, configuration and credentials are kept in {ctx.home}.")
    return 0


def cmd_cancel(ctx: Ctx) -> int:
    ctx.db.request_cancel()
    out("Cancellation requested: a running cycle stops at its next safe point. Requests already sent to Claude or "
        "GitHub are completed and recorded.")
    return 0


def schedule_state(ctx: Ctx, read_back=None, task_query=None) -> dict:
    """read_back/task_query are replaced by the simulation's in-memory task."""
    sched = ctx.cfg.schedule
    effective = (read_back or scheduler.query_schedule)()
    task_query = task_query or scheduler.query
    now = datetime.now(timezone.utc)
    return {"configured": asdict(sched), "effective": asdict(effective) if effective else None,
            # compared by meaning: a task read back from Windows carries defaults for fields its mode does not use
            "in_sync": effective is not None and scheduler.summary(effective) == scheduler.summary(sched),
            "summary": scheduler.summary(sched),
            "next_checks": [t.isoformat(timespec="seconds") for t in scheduler.next_checks(sched, now)],
            "timezone_name": datetime.now().astimezone().tzname(), "task": task_query()}


def cmd_schedule(ctx: Ctx) -> int:
    data = schedule_state(ctx)
    if ctx.args.json:
        out(json.dumps(data, indent=2, default=str))
        return 0
    out(f"Configured: {data['summary']}")
    out("Next checks: " + (", ".join(data["next_checks"]) or "none (manual)") + " (UTC)")
    if runtime.WINDOWS:
        eff = data["effective"]
        out("Task Scheduler: " + ("no task registered" if eff is None else
                                  f"{scheduler.summary(scheduler.Schedule(**{**eff, 'weekdays': tuple(eff['weekdays'])}))}"
                                  + ("" if data["in_sync"] else "; differs from config.toml, run install-task")))
    return 0


def cmd_status(ctx: Ctx) -> int:
    data = status_data(ctx)
    cfg, inst, scheduled = ctx.cfg, installation(ctx), data["last_scheduled_run"]
    if ctx.args.json:
        out(json.dumps(data, indent=2, default=str))
        return 0
    return _print_status(ctx, cfg, data, inst, scheduled)


def status_data(ctx: Ctx, task_query=None) -> dict:
    """Everything `status --json` reports; the setup interface's State is built from it.
    task_query replaces the Task Scheduler query (the simulation's in-memory task)."""
    cfg, db = ctx.cfg, ctx.db
    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    rows = {r["name"].lower(): dict(r) for r in db.repos()}
    effective = {r.name.lower(): r for r in discovery.effective_repos(cfg, db)}
    inst = installation(ctx)
    scheduled = db.conn.execute("SELECT * FROM runs WHERE trigger='scheduled' ORDER BY id DESC LIMIT 1").fetchone()
    reservations = [dict(r) for r in db.open_reservations()]
    spent_month = db.spent_since(month)
    pace = pace_limit(cfg, now)
    data = {
        "runtime": {"host": runtime.hostname(), "home": str(ctx.home), "database": str(db.path),
                    "account": scheduler.current_user()},
        "installation": {k: inst[k] for k in ("active", "problem", "activation", "host_lock")},
        "paused": db.globally_paused,
        "model": cfg.model,
        "poll_interval_minutes": cfg.poll_interval_minutes,
        "schedule": {**asdict(cfg.schedule), "summary": scheduler.summary(cfg.schedule)},
        "generation_policy": {"quiet_minutes": cfg.quiet_minutes, "min_generation_interval_hours": cfg.min_generation_interval_hours,
                              "max_postpone_hours": cfg.max_postpone_hours, "pacing": cfg.pacing},
        "budget": {"configured": cfg.budget.configured, "per_run_usd": cfg.budget.per_run_usd,
                   "daily_usd": cfg.budget.daily_usd, "monthly_usd": cfg.budget.monthly_usd,
                   "spent_today_usd": round(db.spent_since(day), 4), "spent_month_usd": round(spent_month, 4),
                   "remaining_month_usd": round(cfg.budget.monthly_usd - spent_month, 4) if cfg.budget.monthly_usd is not None else None,
                   "pacing_limit_today_usd": round(pace, 4) if pace is not None else None,
                   "periods": "UTC calendar day and month; spend counts actual cost when known, else the full reservation",
                   "open_reservations": [{"repo": r["repo"], "state": r["state"], "reserved_usd": r["reserved_usd"], "at": r["created_at"]}
                                         for r in reservations],
                   "scope": "calls made by this monitor through this database only; other Claude usage is not tracked"},
        "discovery": json.loads(db.get_setting("discovery", "null") or "null") if cfg.discovery.enabled else None,
        "counts": {},
        "repos": [],
        "deferred": [],
        "runs": [dict(r) for r in db.last_runs(5)],
        "last_scheduled_run": dict(scheduled) if scheduled else None,
        "errors": [dict(r) for r in db.last_errors(5)],
        "task": (task_query or scheduler.query)(),
    }
    for key in sorted(set(rows) | set(effective)):
        r, repo = rows.get(key), effective.get(key)
        code, why = discovery.disposition(cfg, r, repo)
        data["counts"][code] = data["counts"].get(code, 0) + 1
        r = r or {}
        name = r.get("name") or repo.name
        p = db.latest_proposal(name)
        enrolled = repo is not None and not repo.readme_auto
        meta = json.loads(r.get("meta") or "{}")
        entry = {
            "name": name, "disposition": code, "detail": why,
            "source": r.get("source") or ("discovered" if repo and repo.readme_auto else "config"),
            "mode": repo.publish_mode if repo else None, "api_export": repo.api_export if repo else None,
            "state": r.get("state") or "new", "state_detail": r.get("state_detail"),
            "branch": r.get("target_branch") or (repo.branch if repo and repo.branch else None), "head": (r.get("head_sha") or "")[:12] or None,
            "last_checked": r.get("last_checked_at"), "pending_since": r.get("pending_since"),
            "pending_age": _age(r.get("pending_since"), now), "next_eligible": r.get("next_attempt_at"),
            "last_generated": r.get("last_generated_at"), "last_published": r.get("last_published_at"),
            "proposal": {"id": p["id"], "state": p["state"], "pr": p["pr_url"], "detail": p["detail"], "created": p["created_at"],
                         "mode": p["mode"], "pr_number": p["pr_number"], "pr_url": p["pr_url"], "published_sha": p["published_sha"]} if p else None,
            "spent_month_usd": round(db.spent_since(month, repo=name), 4),
            # additive fields for the setup interface (RepoView)
            "repo_id": r.get("repo_id"), "visibility": meta.get("visibility"), "enrolled": enrolled,
            "restricted": bool(r.get("restricted")) or cfg.is_restricted(name), "paused": bool(r.get("paused")) or bool(repo and repo.paused),
            "readme_paths": list(repo.readme_paths) if repo else [], "managed_sections": list(repo.managed_sections) if repo else [],
            "disabled_sections": list(repo.disabled_sections) if repo else [], "source_include": list(repo.source_include) if repo else [],
            "source_exclude": list(repo.source_exclude) if repo else [], "latest_draft_id": db.latest_draft_id(name),
        }
        data["repos"].append(entry)
        if entry["pending_since"]:
            data["deferred"].append({"name": name, "pending_since": entry["pending_since"], "age": entry["pending_age"],
                                     "state": entry["state"], "reason": entry["state_detail"]})
    data["deferred"].sort(key=lambda d: d["pending_since"])
    return data


def _print_status(ctx: Ctx, cfg, data: dict, inst: dict, scheduled) -> int:
    rt, b = data["runtime"], data["budget"]
    out(f"ReadmeMonitor {__version__}  host {rt['host']}  home {rt['home']}")
    out("Installation: " + ("ACTIVE on this computer" if inst["active"] else f"not active ({inst['problem']})"))
    out(f"Monitor: {'PAUSED' if data['paused'] else 'active'}; model {cfg.model}; {scheduler.summary(cfg.schedule).lower()}; "
        f"quiet {cfg.quiet_minutes} min; at most one paid update per repository per {cfg.min_generation_interval_hours:g} h")
    if b["configured"]:
        out(f"Budget: today ${b['spent_today_usd']:.4f} of ${b['daily_usd']:.2f}; month ${b['spent_month_usd']:.4f} of ${b['monthly_usd']:.2f}"
            + (f" (pacing allows ${b['pacing_limit_today_usd']:.2f} month-to-date today)" if b["pacing_limit_today_usd"] is not None else ""))
    else:
        out(f"Budget: NOT CONFIGURED (paid generation paused); recorded spend this month ${b['spent_month_usd']:.4f}")
    if b["open_reservations"]:
        out(f"Open reservations: {len(b['open_reservations'])} (${sum(r['reserved_usd'] for r in b['open_reservations']):.4f} counted as spent)")
    d = data["discovery"]
    if cfg.discovery.enabled:
        if not d or not d.get("at"):
            line = "never completed"
        else:
            line = (f"{d['listed']} owned repositories at {d['at']}; coverage "
                    + ("complete" if d["complete"] else "NOT PROVEN: " + "; ".join(d["limitations"])))
        if d and d.get("last_error"):
            line += f"; last failure {d['last_error_at']}: {d['last_error']}"
        out("Discovery: " + line)
    out("Repositories: " + (", ".join(f"{k} {v}" for k, v in sorted(data["counts"].items())) or "none"))
    task = data["task"]
    out("Task: " + (f"{task.get('Status')}; last run {task.get('Last Run Time')} result {task.get('Last Result')}; next {task.get('Next Run Time')}"
                    if task else "not installed")
        + (f"; last scheduled cycle recorded {scheduled['started_at']} ({scheduled['outcome'] or 'running'})" if scheduled else "; no scheduled cycle recorded"))
    if data["deferred"]:
        out("Deferred work (oldest first):")
        for item in data["deferred"]:
            out(f"  {item['name']}: {item['age']} - {item['state']}: {item['reason'] or ''}")
    for r in data["repos"]:
        out(f"\n{r['name']}  [{r['disposition']}; {r['mode'] or '-'}]  {r['detail']}")
        out(f"  checked: {r['last_checked'] or 'never'}  head: {r['head'] or '-'}  month spend: ${r['spent_month_usd']:.4f}"
            + (f"  next eligible {r['next_eligible']}" if r["next_eligible"] else ""))
        if r["state_detail"] and r["state_detail"] not in r["detail"]:
            out(f"  {r['state']}: {r['state_detail']}")
        if r["proposal"]:
            pr = r["proposal"]
            out(f"  latest proposal #{pr['id']}: {pr['state']}" + (f" {pr['pr']}" if pr["pr"] else "") + (f" ({pr['detail']})" if pr["detail"] else ""))
        if r["last_published"]:
            out(f"  last confirmed on the target branch: {r['last_published']}")
    if data["runs"]:
        out("\nRecent runs:")
        for run in data["runs"]:
            out(f"  {run['started_at']} {run['trigger']:<10} {run['outcome'] or 'running'} {run['summary'] or ''}")
    if data["errors"]:
        out("\nRecent errors:")
        for e in data["errors"]:
            out(f"  {e['at']} {e['repo'] or '-'} {e['kind']}: {e['message']}")
    return 0


def cmd_pause(ctx: Ctx, paused: bool) -> int:
    if ctx.args.repo:
        repo = ctx.cfg.repo(ctx.args.repo)
        ctx.db.update_repo(repo.name, paused=int(paused))
        out(f"{repo.name}: {'paused' if paused else 'resumed'}" + (" (config file still sets paused = true)" if repo.paused and not paused else ""))
    else:
        ctx.db.set_setting("paused", "1" if paused else "0")
        out("Monitor " + ("paused: scheduled runs exit without contacting GitHub or Claude." if paused else "resumed."))
    return 0


def cmd_install_task(ctx: Ctx) -> int:
    if not ctx.args.dry_run:
        ctx.require_active()
    cfg = ctx.cfg  # validates before registering anything
    sched = scheduler.Schedule(mode="interval", interval_minutes=ctx.args.interval) if ctx.args.interval else cfg.schedule
    if ctx.args.dry_run:
        out(task_definition(ctx, sched))
        return 0
    out(register_task(ctx, sched))
    out(f"Task {scheduler.TASK_NAME}: {scheduler.summary(sched)}, as {scheduler.current_user()} (only while signed in).")
    out("Verify a real scheduled run with: readme-monitor run-task-now, then readme-monitor status")
    return 0


def task_definition(ctx: Ctx, sched) -> str:
    workdir = Path(__file__).resolve().parent.parent
    return scheduler.task_xml(scheduler.interpreter(), workdir, ctx.home, ctx.config_path, sched,
                              ctx.cfg.run_timeout_seconds // 60 + 5, scheduler.current_user())


def register_task(ctx: Ctx, sched) -> str:
    """Callers check activation first (install-task, the setup interface's schedule save)."""
    return scheduler.install(task_definition(ctx, sched), ctx.home / "task" / "ReadmeMonitor-Sync.xml")


def cmd_ui(ctx: Ctx) -> int:
    from . import server

    return server.launch(ctx.home, port=ctx.args.port, browser=not ctx.args.no_browser, demo=ctx.args.demo)


def cmd_uninstall_task(ctx: Ctx) -> int:
    out(scheduler.uninstall())
    out(f"Task removed. Data, configuration and credentials are kept in {ctx.home}.")
    return 0


def cmd_run_task_now(ctx: Ctx) -> int:
    ctx.require_active()
    out(scheduler.run_now())
    return 0


def cmd_set_credential(ctx: Ctx) -> int:
    kind = ctx.args.kind
    value = sys.stdin.readline().strip() if ctx.args.stdin else getpass.getpass(f"Paste the {kind} credential (input hidden): ").strip()
    if not value:
        out("Nothing entered; credential unchanged.")
        return 1
    credentials.store(kind, value)
    out(f"Stored {kind} credential in Windows Credential Manager for {scheduler.current_user()} (target {credentials.SERVICE}).")
    return 0


def cmd_remove_credential(ctx: Ctx) -> int:
    removed = credentials.remove(ctx.args.kind)
    out(f"{ctx.args.kind} credential {'removed from' if removed else 'was not in'} Windows Credential Manager for {scheduler.current_user()}.")
    return 0


def cmd_backup(ctx: Ctx) -> int:
    dest = ctx.home / "backups" / f"state-{datetime.now().strftime('%Y%m%dT%H%M%S')}.db"
    ctx.db.backup_to(dest)
    backups = sorted((ctx.home / "backups").glob("state-*.db"))
    for old in backups[: -ctx.args.keep]:
        old.unlink()
    out(f"Backup written: {dest} (integrity {check_integrity(dest)})")
    return 0


def cmd_restore(ctx: Ctx) -> int:
    src = Path(ctx.args.path).resolve()
    if check_integrity(src) != "ok":
        out(f"{src} failed the SQLite integrity check; nothing restored.")
        return 1
    try:
        with file_lock(ctx.home / "worker.lock"):
            ctx.db.backup_to(ctx.home / "backups" / f"pre-restore-{datetime.now().strftime('%Y%m%dT%H%M%S')}.db")
            carried = ctx.db.restore_from(src)
    except Locked as e:
        out(f"{e}; pause the monitor and retry.")
        return 1
    out(f"Restored {src}. The previous database was saved under {ctx.home / 'backups'}.")
    if carried:
        out(f"Kept {carried} spending record(s) the backup did not have, so recorded spend never goes down.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", help="runtime folder (default: READMEMONITOR_HOME, else the activated folder, else %%USERPROFILE%%\\ReadmeMonitor)")
    common.add_argument("--config", help="config file (default: <home>\\config.toml)")
    common.add_argument("-v", "--verbose", action="store_true")
    p = argparse.ArgumentParser(prog="readme-monitor", description="Evidence-based README maintenance for enrolled GitHub repositories.")
    p.add_argument("--version", action="version", version=f"ReadmeMonitor {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("doctor", parents=[common], help="check configuration, credentials, access and the scheduled task")
    s.add_argument("--api", action="store_true", help="also look up the configured model with the Anthropic API (no tokens used)")
    s = sub.add_parser("init", parents=[common], help="create a config file, or adopt enrolled READMEs into the standard")
    s.add_argument("--repo")
    s.add_argument("--dry-run", action="store_true", help="show the adoption diff without writing anything")
    s = sub.add_parser("preview", parents=[common], help="show the proposed README locally without publishing")
    s.add_argument("--repo", required=True)
    s.add_argument("--api", action="store_true", help="call Claude if inputs changed (uses budget); default is offline")
    s = sub.add_parser("run-once", parents=[common], help="run one synchronization cycle and exit")
    s.add_argument("--trigger", default="manual", choices=["manual", "scheduled"])
    s.add_argument("--dry-run", action="store_true", help="read GitHub and store drafts only: no model request, no GitHub write")
    s.add_argument("--repo", action="append", help="limit the cycle to this repository (repeatable)")
    s = sub.add_parser("discover", parents=[common], help="list every repository the configured account owns and record dispositions")
    s.add_argument("--dry-run", action="store_true", help="preview the inventory and dispositions without recording anything")
    s.add_argument("--json", action="store_true")
    sub.add_parser("activate", parents=[common], help="enable the worker on this computer for this runtime folder")
    sub.add_parser("bind-host", parents=[common], help="deprecated alias of activate")
    sub.add_parser("deactivate", parents=[common], help="remove the scheduled task and revoke the activation (keeps data)")
    sub.add_parser("cancel", parents=[common], help="ask a running cycle to stop at its next safe point")
    s = sub.add_parser("schedule", parents=[common], help="show the configured schedule, the next checks and the registered task")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("status", parents=[common], help="show monitor, repository, budget and task state")
    s.add_argument("--json", action="store_true")
    for name in ("pause", "resume"):
        s = sub.add_parser(name, parents=[common], help=f"{name} the monitor or one repository")
        s.add_argument("--repo")
    s = sub.add_parser("install-task", parents=[common], help="register the Windows scheduled task")
    s.add_argument("--interval", type=int, help="minutes between runs (default: config poll_interval_minutes)")
    s.add_argument("--dry-run", action="store_true", help="print the task definition instead of registering it")
    sub.add_parser("uninstall-task", parents=[common], help="remove the scheduled task (keeps data)")
    sub.add_parser("run-task-now", parents=[common], help="ask Task Scheduler to start the task now")
    s = sub.add_parser("set-credential", parents=[common], help="store a credential in Windows Credential Manager")
    s.add_argument("kind", choices=sorted(credentials.KINDS))
    s.add_argument("--stdin", action="store_true", help="read the value from standard input")
    s = sub.add_parser("remove-credential", parents=[common], help="delete a stored credential")
    s.add_argument("kind", choices=sorted(credentials.KINDS))
    s = sub.add_parser("backup-db", parents=[common], help="write a consistent copy of the state database")
    s.add_argument("--keep", type=int, default=10)
    s = sub.add_parser("restore-db", parents=[common], help="restore the state database from a backup")
    s.add_argument("path")
    s = sub.add_parser("ui", parents=[common], help="open the local setup interface in the browser (127.0.0.1 only)")
    s.add_argument("--port", type=int, default=8765, help="first port to try (default 8765; the next free one is used)")
    s.add_argument("--no-browser", action="store_true", help="print the sign-in address instead of opening the browser")
    s.add_argument("--demo", action="store_true", help="simulation with an in-memory GitHub and a scripted model in a temporary folder")
    return p


COMMANDS = {
    "doctor": cmd_doctor, "init": cmd_init, "preview": cmd_preview, "run-once": cmd_run_once, "status": cmd_status,
    "pause": lambda c: cmd_pause(c, True), "resume": lambda c: cmd_pause(c, False), "install-task": cmd_install_task,
    "uninstall-task": cmd_uninstall_task, "run-task-now": cmd_run_task_now, "set-credential": cmd_set_credential, "remove-credential": cmd_remove_credential,
    "backup-db": cmd_backup, "restore-db": cmd_restore, "discover": cmd_discover, "bind-host": cmd_bind_host,
    "activate": cmd_activate, "deactivate": cmd_deactivate, "cancel": cmd_cancel, "schedule": cmd_schedule, "ui": cmd_ui,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    ctx = Ctx(args)
    if not (args.command == "ui" and args.demo):  # the simulation uses its own temporary folder and touches no real one
        ctx.home.mkdir(parents=True, exist_ok=True)
        try:
            runtime.migrate_legacy(ctx.home)  # idempotent; a mismatch only leaves the folder inactive
        except OSError as e:
            out(f"Note: the earlier activation record could not be carried forward ({type(e).__name__}); the folder stays inactive.")
        setup_logging(ctx.home, args.verbose, quiet=args.command == "run-once" and args.trigger == "scheduled")
    try:
        return COMMANDS[args.command](ctx)
    except ConfigError as e:
        out(f"Configuration error: {e}")
        return 2
    except runtime.HostNotAuthorized as e:
        out(f"Refused: {e}")
        return 3
    except NotFound as e:
        out(f"Not found: {e}")
        return 1
    except RuntimeError as e:
        out(f"Error: {credentials.redact(str(e))[0]}")
        return 1
    finally:
        if ctx._db is not None:
            ctx._db.close()
