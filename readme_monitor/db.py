"""SQLite state: checkpoints, proposals, usage reservations, runs, pause flags and errors.

Write transactions are short and never span a network request.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .credentials import redact

SCHEMA_VERSION = 3
MAX_EVENTS = 20_000
MAX_DRAFTS = 300
MAX_EVENT_DATA = 32_000
MAX_TEXT = 2000  # one string in an activity event
MAX_DRAFT_TEXT = 200_000  # one stored README, proposal or diff; GitHub renders at most 512 KiB of a README

# Columns added in schema 2 (account-wide discovery, generation policy). Added in place to an
# existing schema-1 database after a backup copy is written; nothing is dropped or rewritten.
REPO_COLUMNS_V2 = (
    ("repo_id", "INTEGER"),          # GitHub's stable repository id; survives renames
    ("source", "TEXT"),              # config | discovered
    ("meta", "TEXT"),                # JSON: visibility, archived, disabled, fork, empty, owner
    ("presence", "TEXT"),            # present | missing | transferred | inaccessible (discovery)
    ("restricted", "INTEGER NOT NULL DEFAULT 0"),  # never sent to Claude (export-restricted)
    ("head_changed_at", "TEXT"),     # when the monitored head last moved (quiet period)
    ("pending_since", "TEXT"),       # oldest unprocessed change still waiting for generation
    ("last_generated_at", "TEXT"),   # last paid generation for this repository
    ("last_published_at", "TEXT"),   # last time a proposal was confirmed on the target branch
    ("adopted_at", "TEXT"),          # adoption proposal opened by the worker
    ("analyzed_head", "TEXT"),       # schema 3: head of the last fully processed revision (compare baseline)
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    name TEXT PRIMARY KEY,
    target_branch TEXT,
    default_branch TEXT,
    head_sha TEXT,
    quick_key TEXT,            -- head + releases + config; unchanged means nothing to fetch
    analyzed_fp TEXT,         -- fingerprint of the last fully processed input set
    generated_llm_fp TEXT,     -- Claude inputs that already have stored output
    generated_sections TEXT,   -- JSON {readme_path: {section: content}} for generated_llm_fp
    generated_flags TEXT,      -- JSON review flags for generated_sections
    state TEXT NOT NULL DEFAULT 'new',
    state_detail TEXT,
    waiting_since TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    bot_sha TEXT,              -- last commit the monitor wrote to its bot branch
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_checked_at TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,       -- preview|publishing|awaiting_merge|published|rejected|superseded|failed
    files TEXT NOT NULL,       -- JSON {path: {"sections": {id: hash}, "base_blob": sha}}
    needs_review TEXT,         -- JSON list of review flags
    pr_number INTEGER,
    pr_url TEXT,
    published_sha TEXT,
    detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS proposals_repo ON proposals(repo, id);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    run_id INTEGER,
    model TEXT NOT NULL,
    state TEXT NOT NULL,       -- reserved|reconciled|released|ambiguous
    reserved_usd REAL NOT NULL,
    actual_usd REAL,
    input_tokens INTEGER, output_tokens INTEGER,
    cache_write_tokens INTEGER, cache_read_tokens INTEGER,
    request_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    trigger TEXT NOT NULL,
    account TEXT,
    outcome TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS http_cache (url TEXT PRIMARY KEY, etag TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, repo TEXT, kind TEXT NOT NULL, message TEXT NOT NULL
);
"""

# Schema 3: the activity feed and stored drafts for the setup interface.
SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    run_id INTEGER,
    trigger TEXT,
    repo TEXT,
    repo_id INTEGER,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    revision TEXT,
    message TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS events_repo ON events(repo, id);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    run_id INTEGER,
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,        -- dry_run | proposal | adoption
    proposal_id INTEGER,
    files TEXT NOT NULL,       -- JSON {path: {old, new, diff, changed, conflicts}}
    flags TEXT NOT NULL,
    notes TEXT NOT NULL,
    estimate_usd REAL
);
CREATE INDEX IF NOT EXISTS drafts_repo ON drafts(repo, id);
"""


def _add_repo_columns(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(repos)")}
    for col, decl in REPO_COLUMNS_V2:
        if col not in have:
            conn.execute(f"ALTER TABLE repos ADD COLUMN {col} {decl}")


# Each step only adds what is missing, so a migration interrupted at any point can simply rerun;
# user_version is written last.
MIGRATION_STEPS = [
    lambda conn: conn.executescript(SCHEMA),
    _add_repo_columns,
    lambda conn: conn.execute("CREATE INDEX IF NOT EXISTS repos_repo_id ON repos(repo_id)"),
    lambda conn: conn.executescript(SCHEMA_V3),
]


def _clean(value, depth=0, limit=MAX_TEXT):
    """Redact secret-looking text in every string; bound string length and nesting depth. A cut
    string says so, so a shortened README or diff is never mistaken for the whole one."""
    if isinstance(value, str):
        text = redact(value)[0]
        return text if len(text) <= limit else f"{text[:limit]} … [{len(text) - limit:,} more characters not stored]"
    if depth >= 20 and isinstance(value, (dict, list, tuple)):
        return "[nested too deeply]"
    if isinstance(value, dict):
        return {str(k): _clean(v, depth + 1, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v, depth + 1, limit) for v in value]
    return value


def _bounded(data: dict) -> str:
    """Serialize within MAX_EVENT_DATA by halving the longest lists; marks truncated."""
    text = json.dumps(data, default=str)
    while len(text) > MAX_EVENT_DATA:
        lists = [(len(json.dumps(v, default=str)), k) for k, v in data.items() if isinstance(v, list) and v]
        if not lists:
            data = {k: v for k, v in data.items() if not isinstance(v, (dict, str)) or len(json.dumps(v, default=str)) < 2000}
            data["truncated"] = True
            text = json.dumps(data, default=str)
            # never slice the text: a cut JSON document would make every later events() read fail
            return text if len(text) <= MAX_EVENT_DATA else json.dumps({"truncated": True})
        _, k = max(lists)
        data[k] = data[k][: len(data[k]) // 2]
        data["truncated"] = True
        text = json.dumps(data, default=str)
    return text


# What counts against a budget: real cost when known, otherwise the full reservation.
SPENT_SQL = """
SELECT COALESCE(SUM(CASE state WHEN 'reconciled' THEN actual_usd
                               WHEN 'released' THEN 0 ELSE reserved_usd END), 0)
FROM usage WHERE created_at >= ?
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{path} was created by a newer ReadmeMonitor (schema {version}); upgrade the code or restore a backup")
        self._migrate(version)

    def _migrate(self, version: int):
        if 0 < version < SCHEMA_VERSION:
            # Rollback: stop the monitor and restore-db this file with the previous code version.
            self.backup_to(self.path.parent / "backups" / f"pre-schema{SCHEMA_VERSION}-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}.db")
        for step in MIGRATION_STEPS:
            step(self.conn)
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self):
        self.conn.close()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # ---- settings / pause -------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str):
        with self.tx() as c:
            c.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    @property
    def globally_paused(self) -> bool:
        return self.get_setting("paused", "0") == "1"

    # ---- repos ------------------------------------------------------------
    def repo(self, name: str) -> sqlite3.Row:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO repos(name) VALUES(?)", (name,))
        return self.conn.execute("SELECT * FROM repos WHERE name=?", (name,)).fetchone()

    def update_repo(self, name: str, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO repos(name) VALUES(?)", (name,))
            c.execute(f"UPDATE repos SET {cols} WHERE name=?", (*fields.values(), name))

    def repos(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM repos ORDER BY name").fetchall()

    def is_restricted(self, name: str) -> bool:
        row = self.conn.execute("SELECT restricted FROM repos WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        return bool(row and row[0])

    def repo_by_id(self, repo_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM repos WHERE repo_id=?", (repo_id,)).fetchone()

    def rename_repo(self, old: str, new: str):
        """A GitHub rename keeps history, spending, proposals and pending work."""
        with self.tx() as c:
            if c.execute("SELECT 1 FROM repos WHERE name=?", (new,)).fetchone():
                # A placeholder row for the new name (never processed under it) gives way.
                c.execute("DELETE FROM repos WHERE name=? AND repo_id IS NULL AND last_checked_at IS NULL", (new,))
            c.execute("UPDATE repos SET name=? WHERE name=?", (new, old))
            c.execute("UPDATE proposals SET repo=? WHERE repo=?", (new, old))
            c.execute("UPDATE usage SET repo=? WHERE repo=?", (new, old))
            c.execute("UPDATE errors SET repo=? WHERE repo=?", (new, old))

    # ---- proposals --------------------------------------------------------
    def create_proposal(self, repo, fingerprint, base_sha, mode, state, files, needs_review) -> int:
        with self.tx() as c:
            # One live proposal per repository: older unfinished ones are superseded.
            c.execute(
                "UPDATE proposals SET state='superseded', updated_at=? WHERE repo=? AND state IN ('preview','publishing','awaiting_merge')",
                (now(), repo),
            )
            cur = c.execute(
                "INSERT INTO proposals(repo, fingerprint, base_sha, mode, state, files, needs_review, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (repo, fingerprint, base_sha, mode, state, json.dumps(files), json.dumps(needs_review), now(), now()),
            )
            return cur.lastrowid

    def update_proposal(self, pid: int, **fields):
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE proposals SET {cols} WHERE id=?", (*fields.values(), pid))

    def proposal(self, pid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()

    def live_proposal(self, repo: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM proposals WHERE repo=? AND state IN ('publishing','awaiting_merge') ORDER BY id DESC LIMIT 1", (repo,)
        ).fetchone()

    def latest_proposal(self, repo: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM proposals WHERE repo=? ORDER BY id DESC LIMIT 1", (repo,)).fetchone()

    # ---- usage ------------------------------------------------------------
    def spent_since(self, since: str, repo: str | None = None, run_id: int | None = None, conn=None) -> float:
        sql, args = SPENT_SQL, [since]
        if repo:
            sql += " AND repo=?"
            args.append(repo)
        if run_id is not None:
            sql += " AND run_id=?"
            args.append(run_id)
        return float((conn or self.conn).execute(sql, args).fetchone()[0])

    def reserve(self, repo: str, run_id: int | None, model: str, usd: float) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO usage(repo, run_id, model, state, reserved_usd, created_at, updated_at) VALUES(?,?,?,'reserved',?,?,?)",
                (repo, run_id, model, usd, now(), now()),
            )
            return cur.lastrowid

    def try_reserve(self, repo: str, run_id: int | None, model: str, usd: float, limits) -> tuple[int | None, str | None]:
        """Check every limit and reserve in one write transaction, so two processes can never
        both pass the check against the same remaining allowance.
        limits: [(label, since, repo or None, run_id or None, limit_usd or None)]."""
        with self.tx() as c:
            for label, since, r, rid, limit in limits:
                if limit is None:
                    continue
                spent = self.spent_since(since, repo=r, run_id=rid, conn=c)
                if spent + usd > limit + 1e-9:
                    return None, f"{label} limit ${limit:.2f} would be exceeded (spent ${spent:.4f}, next request reserves ${usd:.4f})"
            cur = c.execute(
                "INSERT INTO usage(repo, run_id, model, state, reserved_usd, created_at, updated_at) VALUES(?,?,?,'reserved',?,?,?)",
                (repo, run_id, model, usd, now(), now()),
            )
            return cur.lastrowid, None

    def open_reservations(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM usage WHERE state IN ('reserved','ambiguous') ORDER BY id").fetchall()

    def reconcile(self, uid: int, actual_usd: float, tokens: dict, request_id: str | None):
        with self.tx() as c:
            c.execute(
                "UPDATE usage SET state='reconciled', actual_usd=?, input_tokens=?, output_tokens=?, cache_write_tokens=?, cache_read_tokens=?, request_id=?, updated_at=? WHERE id=?",
                (actual_usd, tokens.get("input", 0), tokens.get("output", 0), tokens.get("cache_write", 0), tokens.get("cache_read", 0), request_id, now(), uid),
            )

    def set_usage_state(self, uid: int, state: str):
        with self.tx() as c:
            c.execute("UPDATE usage SET state=?, updated_at=? WHERE id=?", (state, now(), uid))

    def recover_reservations(self, current_run: int) -> int:
        """A reservation left 'reserved' by an earlier, interrupted run may have been billed.
        Keep its full amount counted, marked ambiguous."""
        with self.tx() as c:
            return c.execute(
                "UPDATE usage SET state='ambiguous', updated_at=? WHERE state='reserved' AND (run_id IS NULL OR run_id<>?)",
                (now(), current_run),
            ).rowcount

    # ---- runs / errors / http cache --------------------------------------
    def start_run(self, trigger: str, account: str) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO runs(started_at, trigger, account) VALUES(?,?,?)", (now(), trigger, account)).lastrowid

    def finish_run(self, run_id: int, outcome: str, summary: dict):
        with self.tx() as c:
            c.execute("UPDATE runs SET finished_at=?, outcome=?, summary=? WHERE id=?", (now(), outcome, json.dumps(summary), run_id))
            c.execute("DELETE FROM runs WHERE id <= (SELECT MAX(id) FROM runs) - 2000")

    def last_runs(self, n: int = 5) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    def record_error(self, repo: str | None, kind: str, message: str):
        with self.tx() as c:
            c.execute("INSERT INTO errors(at, repo, kind, message) VALUES(?,?,?,?)", (now(), repo, kind, message[:500]))
            c.execute("DELETE FROM errors WHERE id <= (SELECT MAX(id) FROM errors) - 500")

    def last_errors(self, n: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM errors ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    # ---- events / drafts / cancellation ---------------------------------
    def add_event(self, run_id, trigger, repo, stage, status, message, data=None, revision=None, repo_id=None) -> int:
        """Secret-looking text is redacted in the message and in every string inside data."""
        msg = redact(str(message))[0][:300]
        payload = _bounded(_clean(dict(data or {})))
        with self.tx() as c:
            eid = c.execute(
                "INSERT INTO events(at, run_id, trigger, repo, repo_id, stage, status, revision, message, data) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now(), run_id, trigger, repo, repo_id, stage, status, revision, msg, payload)).lastrowid
            c.execute("DELETE FROM events WHERE id <= ?", (eid - MAX_EVENTS,))
            return eid

    @staticmethod
    def _event(row) -> dict:
        d = dict(row)
        d["data"] = json.loads(d["data"] or "{}")
        return d

    def events(self, after: int | None = None, limit: int = 200, run_id: int | None = None, repo: str | None = None) -> list[dict]:
        """Ascending by id. Without `after`, the newest `limit` events."""
        where, args = [], []
        if after is not None:
            where.append("id > ?")
            args.append(after)
        if run_id is not None:
            where.append("run_id = ?")
            args.append(run_id)
        if repo is not None:
            where.append("repo = ?")
            args.append(repo)
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        if after is None:
            rows = self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()[::-1]
        else:
            rows = self.conn.execute(sql + " ORDER BY id LIMIT ?", (*args, limit)).fetchall()
        return [self._event(r) for r in rows]

    def last_event_id(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    def add_draft(self, run_id, repo, kind, files, flags, notes, estimate_usd=None, proposal_id=None) -> int:
        with self.tx() as c:
            did = c.execute(
                "INSERT INTO drafts(at, run_id, repo, kind, proposal_id, files, flags, notes, estimate_usd) VALUES(?,?,?,?,?,?,?,?,?)",
                (now(), run_id, repo, kind, proposal_id, json.dumps(_clean(files, limit=MAX_DRAFT_TEXT)), json.dumps(_clean(list(flags))),
                 json.dumps(_clean(list(notes))), estimate_usd)).lastrowid
            c.execute("DELETE FROM drafts WHERE id <= ?", (did - MAX_DRAFTS,))
            return did

    def link_draft(self, draft_id: int, proposal_id: int):
        with self.tx() as c:
            c.execute("UPDATE drafts SET proposal_id=? WHERE id=?", (proposal_id, draft_id))

    def draft(self, did: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        for k in ("files", "flags", "notes"):
            d[k] = json.loads(d[k])
        return d

    def latest_draft_id(self, repo: str) -> int | None:
        row = self.conn.execute("SELECT MAX(id) FROM drafts WHERE repo=?", (repo,)).fetchone()
        return row[0] if row else None

    def request_cancel(self, launch: str | None = None):
        """Asks the cycles already recorded to stop at their next safe boundary. Keyed to run ids,
        not timestamps: with one-second timestamps a request made while idle could stop a run
        starting in the same second. `launch` also names the one run the interface launched,
        which may not have recorded its run yet; every launch has a new token, so it can never
        match a later run."""
        newest = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM runs").fetchone()[0]
        self.set_setting("cancel_requested", str(newest))
        if launch:
            self.set_setting("cancel_launch", launch)

    def cancel_requested(self, run_id: int, launch: str | None = None) -> bool:
        """True only for runs that existed when Cancel was pressed, or for the launch it named;
        a later run is never stopped by it."""
        at = self.get_setting("cancel_requested")
        if at and at.isdigit() and run_id <= int(at):
            return True
        return launch is not None and self.get_setting("cancel_launch") == launch
    # Nothing clears these: run ids never repeat (AUTOINCREMENT) and every launch has a new token,
    # so a request left behind can never stop a later run, and clearing it at the end of one run
    # would drop the request for another run still going (a dry run takes no worker lock).

    def cache_get(self, url: str):
        row = self.conn.execute("SELECT etag, body FROM http_cache WHERE url=?", (url,)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def cache_put(self, url: str, etag: str, body: str):
        with self.tx() as c:
            c.execute("INSERT INTO http_cache(url, etag, body) VALUES(?,?,?) ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, body=excluded.body", (url, etag, body))

    # ---- backup / restore -------------------------------------------------
    def backup_to(self, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        try:
            self.conn.backup(target)
        finally:
            target.close()

    def restore_from(self, src: Path) -> int:
        """Replace this database with the file `src` (opened read-only, never modified), then carry
        forward every usage row this database had that the source lacks, so restoring an older
        copy can never erase recorded spending and re-open an allowance. Carried rows that were
        still 'reserved' become 'ambiguous'. Returns the number of usage rows carried forward."""
        cols = ("repo", "run_id", "model", "state", "reserved_usd", "actual_usd", "input_tokens", "output_tokens",
                "cache_write_tokens", "cache_read_tokens", "request_id", "created_at", "updated_at")
        mine = [dict(r) for r in self.conn.execute(f"SELECT {', '.join(cols)} FROM usage")]
        source = sqlite3.connect(f"file:{quote(src.resolve().as_posix())}?mode=ro", uri=True)
        try:
            version = source.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"{src} was created by a newer ReadmeMonitor (schema {version})")
            source.backup(self.conn)
        finally:
            source.close()
        self._migrate(version)
        key = ("repo", "model", "reserved_usd", "created_at")
        # A multiset, not a set: two identical reservations in the same second are two charges.
        have: dict[tuple, int] = {}
        for r in self.conn.execute(f"SELECT {', '.join(key)} FROM usage"):
            have[tuple(r)] = have.get(tuple(r), 0) + 1
        carried = 0
        with self.tx() as c:
            for r in mine:
                k = tuple(r[k] for k in key)
                if have.get(k, 0) > 0:
                    have[k] -= 1  # this row is already in the restored copy
                    continue
                if r["state"] == "reserved":
                    r["state"] = "ambiguous"
                c.execute(f"INSERT INTO usage({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", tuple(r[k] for k in cols))
                carried += 1
        return carried


def check_integrity(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
