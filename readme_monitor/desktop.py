"""The desktop application's helper: `readme-monitor desktop-serve`, started by the shell only.

It serves the same authenticated loopback interface as `readme-monitor ui` (server.py) on a free
port, and talks to the shell that started it through its own stdin/stdout pipes, one JSON object
per line. Nothing on those pipes reaches a command line, a URL, a log or the disk:

  -> {"event": "ready", "port": 49731, "instance": "...", "code": "...", "version": "0.3.0", "update": {...}}
  <- {"id": 1, "cmd": "state" | "run" | "pause" | "resume" | "sign_in" | "prepare_update" | "finish_update" | "quit", ...}
  -> {"id": 1, "ok": true, ...}

`code` signs the shell's own window in once (POST /auth, see server._desktop_auth); `sign_in`
issues a fresh one. When stdin closes (the shell quit or crashed) the helper stops serving and
exits; runs it launched keep going, exactly like closing `readme-monitor ui`.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import __version__, runtime, server
from .db import DB, check_integrity
from .worker import Locked, file_lock

log = logging.getLogger(__name__)
_out = threading.Lock()


def emit(obj: dict):
    with _out:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def new_code(app: server.App) -> str:
    with app.pair_lock:
        app.desktop_code, app.desktop_code_at = secrets.token_urlsafe(32), time.time()
        return app.desktop_code


def state(app: server.App) -> dict:
    ctx = app.ctx()
    try:
        paused = bool(ctx.db.globally_paused)
        active = app.active(ctx)
    finally:
        if ctx._db is not None:
            ctx._db.close()
    return {"active": active, "paused": paused, "busy": app.busy(), "updating": runtime.maintenance(app.home) is not None,
            "home": str(app.home), "version": __version__}


def set_paused(app: server.App, paused: bool) -> dict:
    ctx = app.ctx()
    try:
        ctx.db.set_setting("paused", "1" if paused else "0")
    finally:
        ctx.db.close()
    return state(app)


def run_now(app: server.App) -> dict:
    ctx = app.ctx()
    try:
        return app.start_checked(ctx, dry_run=False)
    finally:
        if ctx._db is not None:
            ctx._db.close()


def prepare_update(home: Path, to_version: str, demo: bool = False) -> dict:
    """Before the shell installs an update: wait for no check to be running (never interrupt one),
    take a consistent database backup and set the maintenance flag that every worker entry point
    honours, all while holding worker.lock. Returns {"deferred": true} while a check is running."""
    try:
        with file_lock(home / "worker.lock"):
            backup = None
            db_path = home / "state.db"
            if db_path.exists() and not demo:
                dest = home / "backups" / f"pre-update-{__version__}-to-{to_version}-{time.strftime('%Y%m%dT%H%M%S')}.db"
                db = DB(db_path)
                try:
                    db.backup_to(dest)
                finally:
                    db.close()
                if check_integrity(dest) != "ok":
                    return {"ok": False, "error": "the database backup failed its integrity check; the update was not started"}
                backup = str(dest)
            rec = runtime.begin_maintenance(home, __version__, to_version, backup)
            log.info("update to %s prepared; backup %s", to_version, backup)
            return {"ok": True, "deferred": False, "backup": backup, "maintenance": rec}
    except Locked:
        return {"ok": True, "deferred": True, "reason": "a check is running; the update waits until it finishes"}


def finish_update(home: Path) -> dict | None:
    """At start-up after an update (or an abandoned one): health-check, then clear the flag. The
    database opens (and runs its versioned migrations) under worker.lock. A failure keeps the flag,
    so scheduled checks stay skipped and the data stays as it is; nothing is rolled back."""
    if not (home / runtime.MAINTENANCE_FILE).exists():
        return None
    try:
        with file_lock(home / "worker.lock", wait=5):
            db_path = home / "state.db"
            if db_path.exists():
                DB(db_path).close()  # migrations, or the newer-schema refusal, happen here
                if check_integrity(db_path) != "ok":
                    raise RuntimeError("the database failed its integrity check")
            rec = runtime.end_maintenance(home)
    except Exception as e:  # reported to the shell and kept in the log; the flag stays
        log.error("update health check failed: %s", e)
        return {"ok": False, "error": str(e)[:300]}
    log.info("update finished: %s -> %s", (rec or {}).get("from_version"), __version__)
    return {"ok": True, "from_version": (rec or {}).get("from_version"), "version": __version__}


def handle(app: server.App, msg: dict, demo: bool) -> dict:
    cmd = msg.get("cmd")
    if cmd == "state":
        return state(app)
    if cmd == "run":
        return run_now(app)
    if cmd in ("pause", "resume"):
        return set_paused(app, cmd == "pause")
    if cmd == "sign_in":
        return {"code": new_code(app)}
    if cmd == "prepare_update":
        to = msg.get("version")
        if not isinstance(to, str) or not to:
            return {"ok": False, "error": "version is required"}
        return prepare_update(app.home, to, demo)
    if cmd == "finish_update":
        return finish_update(app.home) or {"ok": True, "nothing": True}
    return {"ok": False, "error": f"unknown command {cmd!r}"}


def serve(home: Path, demo: bool = False) -> int:
    if demo:
        server.clean_stale_demos()
        home = Path(tempfile.mkdtemp(prefix=server.DEMO_PREFIX))
    update = None if demo else finish_update(Path(home).resolve())
    app = server.App(home, demo=demo)
    httpd = server.serve(app, 0)
    runtime._write(app.home / server.SERVER_FILE, {"port": app.port, "pid": os.getpid()})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    emit({"event": "ready", "port": app.port, "instance": server.instance_id(app.home), "code": new_code(app),
          "version": __version__, "home": "DEMO-FOLDER" if demo else str(app.home), "update": update})
    try:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("cmd") == "quit":
                emit({"id": msg.get("id"), "ok": True})
                break
            try:
                reply = handle(app, msg, demo)
            except Exception as e:
                log.exception("desktop command failed")
                reply = {"ok": False, "error": type(e).__name__}
            emit({"id": msg.get("id"), **reply})
    finally:
        server.stop(app, httpd)
        rec = runtime._read(app.home / server.SERVER_FILE)
        if rec and rec.get("port") == app.port:
            (app.home / server.SERVER_FILE).unlink(missing_ok=True)
        if demo:
            if app.thread is not None:
                app.thread.join(30)
            shutil.rmtree(app.home, ignore_errors=True)
    return 0
