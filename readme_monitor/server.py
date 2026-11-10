"""Local setup interface: `readme-monitor ui` (docs/api.md, the contract with the page).

Standard library only. Binds 127.0.0.1, pairs one browser through a single-use code written to the
runtime folder, and checks Host, session cookie, Origin and a CSRF header so other web pages and
other computers cannot drive it. The backend owns every decision; the page only shows and asks.
State is read from the same sources as `readme-monitor status`; runs start as separate processes
so closing the page or stopping this server never stops a check. One SQLite connection per request,
and no write transaction is held across a network call.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import asdict
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, cli, config, credentials, discovery, runtime, scheduler
from .config import REPO_RE, ConfigError, load
from .db import DB, SPENT_SQL
from .github import GitHub, GitHubError
from .worker import Locked, file_lock

log = logging.getLogger(__name__)

PACKAGE = Path(__file__).resolve().parent
WEB_ROOT = PACKAGE / "web"
SERVER_FILE = "ui-server.json"
PAIR_SECONDS = 120
MAX_BODY = 64 * 1024
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".json": "application/json", ".map": "application/json", ".svg": "image/svg+xml",
         ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".ico": "image/x-icon", ".woff2": "font/woff2",
         ".woff": "font/woff", ".ttf": "font/ttf", ".txt": "text/plain; charset=utf-8"}
SCHEDULE_KEYS = ("mode", "interval_minutes", "time", "weekdays", "timezone")
CONFIG_WRITERS = ("put_repo", "delete_repo", "settings", "schedule")
REPO_KEYS = ("branch", "readme_paths", "managed_sections", "disabled_sections", "publish_mode", "api_export",
             "source_include", "source_exclude", "paused")

LAUNCHER_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><title>ReadmeMonitor</title>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem">
<h1>ReadmeMonitor</h1><p>{text}</p></body></html>"""
NOT_PAIRED = "Open this page from the launcher: run <code>readme-monitor ui</code> in a terminal. It signs this browser in for you."
NO_BUILD = ("The interface has not been built in this copy. Build it with <code>npm ci</code> and <code>npm run build</code> in the "
            "<code>ui</code> folder, then reload. The local API in docs/api.md works without it.")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_bytes(obj) -> bytes:
    # < > & escaped: repository text stays inert even if something sniffs or embeds the answer.
    text = json.dumps(obj, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return text.encode("utf-8")


def instance_id(home: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(Path(home).resolve())).encode()).hexdigest()[:16]


def pair_path(home: Path) -> Path:
    """The pairing file lives in the account's own profile (next to the active-folder pointer), not in
    the runtime folder, whose permissions may be inherited from a shared drive."""
    return runtime.pointer_path().parent / ".readme-monitor-ui" / f"{instance_id(home)}-pair.json"


def write_pairing(home: Path) -> str:
    """A fresh single-use code; replaces any earlier one."""
    path = pair_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    if path.is_symlink():
        path.unlink()  # never write through a planted link
    code = secrets.token_urlsafe(24)
    runtime._write(path, {"code": code, "created": time.time()})
    if os.name != "nt":
        os.chmod(path, 0o600)
    return code


def pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def running_port(home: Path) -> int | None:
    """Port of a live server for this runtime folder: its pid is alive and the port answers as us."""
    rec = runtime._read(home / SERVER_FILE)
    if not rec or not pid_alive(rec.get("pid")) or not isinstance(rec.get("port"), int):
        return None
    port = rec["port"]
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.sendall(f"GET /api/session HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            head = s.recv(4096).decode("latin-1")
    except OSError:
        return None
    return port if f"X-RM-Instance: {instance_id(home)}" in head else None


def _task_time(text: str | None) -> str | None:
    """schtasks prints local, locale-formatted times; only the en-US form is converted, anything else
    gives null (never a guessed value). _task_view falls back to the task's XML definition."""
    if not text:
        return None
    try:
        return datetime.strptime(text, "%m/%d/%Y %I:%M:%S %p").astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def _task_view(t: dict | None, effective: dict | None) -> dict | None:
    """The registered task as the page shows it. `schtasks /V /FO LIST` labels and times are in the
    display language; only the English form is read. Otherwise the task is known from its
    `schtasks /Query /XML` definition (`effective`, language independent): next_run then comes from
    the product's own next_checks for daily and weekly local schedules (exact wall-clock starts) and
    is null for an interval (its phase is the task's, not ours); last_run is not in the XML and is null.
    The enabled flag is only known from the English list; elsewhere it is assumed true."""
    if not t and effective is None:
        return None
    t = t or {}
    next_run = _task_time(t.get("Next Run Time"))
    if next_run is None and effective and t.get("Status") != "Disabled" and t.get("Next Run Time") != "N/A":
        sched = scheduler.Schedule(**{**effective, "weekdays": tuple(effective["weekdays"])})
        if sched.mode in ("daily", "weekly"):
            planned = scheduler.next_checks(sched, datetime.now(timezone.utc))
            next_run = planned[0].isoformat(timespec="seconds") if planned else None
    return {"installed": True, "enabled": t.get("Status") != "Disabled", "status": t.get("Status") or "",
            "next_run": next_run, "last_run": _task_time(t.get("Last Run Time")), "last_result": t.get("Last Result")}


class App:
    """Server state shared by request threads: runtime folder, sessions, the current run."""

    def __init__(self, home: Path, demo: bool = False, latency: tuple[float, float] = (0.15, 1.5)):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.port = 0
        self.sessions: dict[str, str] = {}  # session token -> csrf token
        self.stop = threading.Event()
        self.run_lock = threading.Lock()
        self.pair_lock = threading.Lock()
        self.config_lock = threading.Lock()  # config.toml read-modify-write: two quick saves never lose one
        self.proc = None    # the last run process this server started
        self.thread = None  # simulation: the background run
        self.heartbeat, self.poll = 15.0, 0.25
        self.demo = None
        if demo:
            from . import simulation  # fake transports: no keyring, no schtasks, no network

            self.demo = simulation.build_demo(self.home, latency=latency[0])
            self.demo.claude.latency = latency[1]
            self.demo.db.close()  # each run opens its own connection in its thread
            self.sim_active = True
            self.sim_task = self.demo.cfg.schedule
            db = DB(self.home / "state.db")
            db.set_setting("schedule_saved_at", _now())
            db.close()
        elif not (self.home / "config.toml").exists():
            shutil.copyfile(cli.EXAMPLE_CONFIG, self.home / "config.toml")  # what `init` does first

    # ---- helpers ------------------------------------------------------------
    def scrub(self, body: bytes) -> bytes:
        """Simulation: the temporary folder's path names the real account, so answers show a neutral one."""
        if not self.demo:
            return body
        return body.replace(_json_bytes(str(self.home))[1:-1], b"DEMO-FOLDER")

    def ctx(self) -> cli.Ctx:
        return cli.Ctx(SimpleNamespace(home=str(self.home), config=None, api=False))

    def active(self, ctx) -> bool:
        if self.demo:
            return self.sim_active
        return runtime.activation_problem(self.home, ctx.host_lock()) is None

    def _sim_task(self):
        return self.sim_task if self.sim_active else None

    def busy(self) -> bool:
        if self.demo:
            return self.thread is not None and self.thread.is_alive()
        if self.proc is not None and self.proc.poll() is None:
            return True
        try:  # the scheduled task or a terminal may be running a check
            with file_lock(self.home / "worker.lock"):
                return False
        except Locked:
            return True

    def credential(self, kind: str) -> tuple[bool, str]:
        if self.demo:
            return True, "simulation"
        value, source = credentials.get(kind)
        return bool(value), source

    def spawn(self, args: list[str]):
        """A detached `python -m readme_monitor ...`: it outlives the page and this server."""
        cmd = [sys.executable, "-m", "readme_monitor", *args, "--home", str(self.home)]
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": str(PACKAGE.parent)}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        return subprocess.Popen(cmd, **kw)

    def _demo_run(self, dry_run: bool, repos, advance: bool):
        db = DB(self.demo.db_path)
        self.demo.db, self.demo.cfg = db, load(self.demo.config_path)
        try:
            if dry_run:
                self.demo.run_cycle(dry_run=True, repos=repos)
            else:
                with file_lock(self.home / "worker.lock"):
                    self.demo.run_cycle("manual", repos=repos)
                if advance:
                    self.demo.advance()  # the next Run shows the next part of the scripted scenario
        except Exception:
            log.exception("simulation run failed")
        finally:
            db.close()

    def start(self, dry_run: bool, repos: list[str] | None, adopt: str | None = None) -> None:
        if self.demo:
            self.thread = threading.Thread(target=self._demo_run, args=(dry_run, [adopt] if adopt else repos, not adopt and not dry_run),
                                           daemon=True)
            self.thread.start()
            return
        if adopt:
            args = ["init", "--repo", adopt]
        else:
            args = ["run-once", "--dry-run"] if dry_run else ["run-once", "--trigger", "manual"]
            for r in repos or ():
                args += ["--repo", r]
        self.proc = self.spawn(args)

    # ---- state --------------------------------------------------------------
    def schedule_view(self, ctx) -> dict:
        cfg = ctx.cfg
        if self.demo:
            s = cli.schedule_state(ctx, read_back=self._sim_task, task_query=lambda: None)
            task = ({"installed": True, "enabled": cfg.schedule.mode != "manual", "status": "Ready (simulated)", "next_run": None,
                     "last_run": None, "last_result": None} if self._sim_task() else None)
        else:
            s = cli.schedule_state(ctx)
            task = _task_view(s["task"], s["effective"])
        checks = s["next_checks"]
        if task and task["next_run"]:
            checks = [task["next_run"], *[c for c in checks if c > task["next_run"]]][:3]
        notes = []
        if cfg.schedule.mode == "manual":
            notes.append("Manual mode: nothing runs until you start a check.")
        else:
            notes.append("Checks run only while you are signed in to Windows and the computer is awake; a check missed while "
                         "it was asleep or off runs once when it is available again.")
        rules = []  # a clause whose setting is zero says nothing true, so it is left out
        if cfg.quiet_minutes:
            rules.append(f"a repository must be quiet for {cfg.quiet_minutes} minutes after its last commit")
        if cfg.min_generation_interval_hours:
            rules.append(f"a repository gets at most one paid update every {cfg.min_generation_interval_hours:g} hours")
        rules.append("spending follows the daily and monthly limits" + (" with even pacing" if cfg.pacing == "even" else ""))
        notes.append("A check does not always produce an update: " + ", ".join(rules)
                     + ", and only changes relevant to managed sections lead to a new draft.")
        return {"configured": s["configured"], "effective": s["effective"], "in_sync": s["in_sync"], "task": task,
                "summary": s["summary"], "next_checks": checks, "timezone_name": s["timezone_name"], "notes": notes}

    @staticmethod
    def run_view(row) -> dict | None:
        if row is None:
            return None
        r = dict(row)
        return {"id": r["id"], "trigger": r["trigger"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                "outcome": r["outcome"], "summary": json.loads(r["summary"]) if r["summary"] else None}

    def state(self, ctx) -> dict:
        cfg, db = ctx.cfg, ctx.db
        data = cli.status_data(ctx, task_query=(lambda: None) if self.demo else None)
        inst = cli.installation(ctx)
        active = self.active(ctx)
        if self.demo:
            # A simulated computer and account: the sample screens never show the real ones.
            inst.update(home=str(self.home), host="DEMO-PC", account="demo", supported=True, active=active, host_lock=None,
                        problem=None if active else "Simulation: the worker is not enabled (in memory only).",
                        activation={"host": "DEMO-PC", "activated_at": _now(), "installation_id": "simulation"} if active else None)
        act = inst["activation"]
        installation = {**{k: inst[k] for k in ("home", "host", "account", "platform", "supported", "active", "problem", "host_lock")},
                        "activation": {k: act[k] for k in ("host", "activated_at", "installation_id", "migrated_from") if k in act} if act else None,
                        "can_change_home": not active and not self.demo}
        sched = self.schedule_view(ctx)
        now = datetime.now(timezone.utc)
        runs = [self.run_view(r) for r in db.last_runs(20)]
        current = next((r for r in runs if r["finished_at"] is None
                        and (now - datetime.fromisoformat(r["started_at"])).total_seconds() < cfg.run_timeout_seconds), None)
        last = next((r for r in runs if r["finished_at"]), None)
        unreadable = (self.home / runtime.ACTIVATION_FILE).exists() and runtime.activation(self.home) is None
        if unreadable and not self.demo:
            rs, detail = "unavailable", inst["problem"]
        elif not active:
            rs, detail = "inactive", inst["problem"] or "not enabled"
        elif data["paused"]:
            rs, detail = "paused", "Paused: scheduled checks exit without contacting GitHub or Claude."
        elif current or self.busy():
            rs, detail = "running", "A check is running."
        elif sched["task"] and sched["task"]["enabled"] and cfg.schedule.mode != "manual":
            rs, detail = "scheduled_idle", sched["summary"]
        else:
            rs, detail = "manual_idle", "No schedule: checks run when you start them."
        gh_present, gh_source = self.credential("github")
        an_present, an_source = self.credential("anthropic")
        login = (simulation_owner() if self.demo else db.get_setting("github_login")) or None
        b = data["budget"]
        repos = [{k: e.get(k) for k in ("name", "repo_id", "visibility", "source", "enrolled", "mode", "api_export", "restricted", "branch",
                                        "readme_paths", "managed_sections", "disabled_sections", "source_include", "source_exclude",
                                        "paused", "disposition", "detail", "state", "state_detail", "head", "last_checked",
                                        "pending_since", "next_eligible", "last_generated", "last_published", "latest_draft_id",
                                        "spent_month_usd")}
                 | {"proposal": {k: e["proposal"][k] for k in ("id", "state", "mode", "pr_number", "pr_url", "published_sha", "detail", "created")}
                    if e["proposal"] else None}
                 for e in data["repos"]]
        exports = any(r.api_export == "allowlist" for r in discovery.effective_repos(cfg, db))
        # Only a finished dry run that previewed at least one repository counts as the onboarding step.
        dry = db.conn.execute("SELECT 1 FROM runs WHERE trigger='dry_run' AND finished_at IS NOT NULL AND outcome='ok' "
                              "AND COALESCE(json_extract(summary, '$.previewed'), 0) >= 1 LIMIT 1").fetchone()
        enrolled = [r for r in cfg.repos]
        steps = [
            ("runtime", active, "Enabled on this computer" if active else (inst["problem"] or "not enabled")),
            ("github", gh_present and bool(login), f"Signed in as {login}" if gh_present and login else
             ("Credential stored; identity not checked yet" if gh_present else "No GitHub credential stored")),
            ("repositories", bool(enrolled) or cfg.discovery.enabled,
             f"{len(enrolled)} enrolled" + (f"; discovery on for {cfg.discovery.owner}" if cfg.discovery.enabled else "")),
            ("model", (an_present and cfg.budget.configured) or not exports,
             "Not needed: no repository sends source to Claude" if not exports else
             ("Anthropic credential and all three limits set" if an_present and cfg.budget.configured else
              "Needs an Anthropic credential and per-run, daily and monthly limits")),
            ("publication", (bool(enrolled) or cfg.discovery.enabled) and all(r.publish_mode for r in enrolled) and bool(db.get_setting("schedule_saved_at")),
             "Publication modes and schedule saved" if db.get_setting("schedule_saved_at") else "Save a schedule"),
            ("dry_run", dry is not None, "A dry run previewed a repository" if dry else "No finished dry run that previewed a repository yet"),
        ]
        return {
            "version": __version__, "simulation": bool(self.demo), "now": _now(), "cursor": db.last_event_id(),
            "installation": installation,
            "runtime": {"state": rs, "detail": detail, "current_run": current, "last_run": last,
                        "next_check": sched["next_checks"][0] if rs == "scheduled_idle" and sched["next_checks"] else None},
            "credentials": {"github": {"present": gh_present, "source": gh_source, "login": login},
                            "anthropic": {"present": an_present, "source": an_source}},
            "budget": {"configured": b["configured"], "per_run_usd": b["per_run_usd"], "daily_usd": b["daily_usd"],
                       "monthly_usd": b["monthly_usd"], "pacing": cfg.pacing, "spent_today_usd": b["spent_today_usd"],
                       "spent_month_usd": b["spent_month_usd"], "pacing_limit_today_usd": b["pacing_limit_today_usd"],
                       "open_reservations": len(b["open_reservations"]),
                       "open_reserved_usd": round(sum(r["reserved_usd"] for r in b["open_reservations"]), 4)},
            "model": {"id": cfg.model, "effort": cfg.effort, "priced": cfg.price() is not None, "provider": "anthropic"},
            "policy": {"quiet_minutes": cfg.quiet_minutes, "min_generation_interval_hours": cfg.min_generation_interval_hours,
                       "max_postpone_hours": cfg.max_postpone_hours},
            "schedule": sched,
            "discovery": {"enabled": cfg.discovery.enabled, "owner": cfg.discovery.owner, "last": data["discovery"]},
            "repos": repos,
            "onboarding": {"complete": all(d for _, d, _ in steps),
                           "steps": [{"id": i, "done": bool(d), "detail": t} for i, d, t in steps]},
        }

    def diagnostics(self, ctx) -> list[dict]:
        if self.demo:
            rows = [("PASS", "simulation", "in-memory GitHub and scripted model; nothing real is contacted"),
                    ("PASS", "python", sys.version.split()[0]), ("PASS", "config", str(ctx.config_path)),
                    ("PASS", "database", str(self.home / "state.db"))]
        else:
            rows = cli.doctor_checks(ctx)
        return [{"status": s, "name": n, "detail": credentials.redact(str(d))[0]} for s, n, d in rows]


def simulation_owner() -> str:
    from .simulation import OWNER

    return OWNER


# ---- routes -------------------------------------------------------------------------------

ROUTES: list[tuple[str, re.Pattern, str]] = [(m, re.compile(p + r"\Z"), f) for m, p, f in [
    ("GET", r"/api/session", "session"), ("GET", r"/api/state", "get_state"), ("GET", r"/api/events", "events"),
    ("GET", r"/api/events/stream", "stream"), ("GET", r"/api/runs", "runs"), ("GET", r"/api/runs/(\d+)", "run"),
    ("GET", r"/api/drafts/(\d+)", "draft"), ("GET", r"/api/usage", "usage"), ("GET", r"/api/diagnostics", "diagnostics"),
    ("POST", r"/api/installation/activate", "activate"), ("POST", r"/api/installation/deactivate", "deactivate"),
    ("POST", r"/api/installation/home", "set_home"), ("POST", r"/api/credentials/(github|anthropic)", "store_credential"),
    ("DELETE", r"/api/credentials/(github|anthropic)", "remove_credential"), ("POST", r"/api/github/discover", "discover"),
    ("PUT", r"/api/repos/([^/]+)/([^/]+)", "put_repo"), ("DELETE", r"/api/repos/([^/]+)/([^/]+)", "delete_repo"),
    ("POST", r"/api/repos/([^/]+)/([^/]+)/adopt", "adopt"), ("PUT", r"/api/settings", "settings"), ("PUT", r"/api/schedule", "schedule"),
    ("POST", r"/api/pause", "pause"), ("POST", r"/api/resume", "resume"), ("POST", r"/api/run", "run_cycle"), ("POST", r"/api/cancel", "cancel"),
]]


def _int(query, key, default=None, low=0, high=None):
    v = query.get(key, [None])[0]
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except ValueError:
        raise ApiError(400, "bad_request", f"{key} must be a whole number") from None
    if n < low:
        raise ApiError(400, "bad_request", f"{key} must be at least {low}")
    return min(n, high) if high else n


def _repo_name(owner: str, name: str) -> str:
    full = f"{unquote(owner)}/{unquote(name)}"
    if not REPO_RE.match(full):
        raise ApiError(400, "bad_request", "repository must look like owner/name")
    return full


def _confirmed(body):
    if body.get("confirm") is not True:
        raise ApiError(400, "confirmation_required", "This action needs {\"confirm\": true}.")


def _set(table: dict, values: dict, keys):
    for k in keys:
        if k in values:
            if values[k] is None:
                table.pop(k, None)
            else:
                table[k] = values[k]


class Handler(BaseHTTPRequestHandler):
    server_version = "ReadmeMonitor"
    sys_version = ""
    protocol_version = "HTTP/1.0"  # one request per connection; the event stream ends with it

    def log_message(self, format, *args):  # request lines carry the pairing code; nothing is logged
        pass

    @property
    def app(self) -> App:
        return self.server.app

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # ---- responses --------------------------------------------------------
    def _headers(self, status: int, ctype: str, length: int | None, extra: dict | None = None, api: bool = False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-RM-Instance", instance_id(self.app.home))
        if api:
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None, api: bool = False):
        self._headers(status, ctype, len(body), extra, api)
        self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, self.app.scrub(_json_bytes(obj)), "application/json; charset=utf-8", api=True)

    def _page(self, status: int, text: str, extra: dict | None = None):
        self._send(status, LAUNCHER_PAGE.format(text=text).encode(), "text/html; charset=utf-8", {"Cache-Control": "no-store", **(extra or {})})

    # ---- request handling -------------------------------------------------
    def _session(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        token = cookie["rm_session"].value if "rm_session" in cookie else None
        return token if token and token in self.app.sessions else None

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad_request", "invalid Content-Length") from None
        if length > MAX_BODY:
            raise ApiError(413, "too_large", "request body is larger than 64 KB")
        if length <= 0:
            return {}
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise ApiError(415, "unsupported_media_type", "send JSON (Content-Type: application/json)")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_request", "the body is not valid JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "bad_request", "the body must be a JSON object")
        return body

    def _dispatch(self, method: str):
        try:
            host = self.headers.get("Host") or ""
            if host not in (f"127.0.0.1:{self.app.port}", f"localhost:{self.app.port}"):
                raise ApiError(403, "forbidden_host", "requests must address this computer's loopback address")
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == "/auth":
                if method != "GET":
                    raise ApiError(405, "method_not_allowed", "use GET")
                return self._auth(query)
            if url.path == "/api" or url.path.startswith("/api/"):
                session = self._session()
                if session is None:
                    raise ApiError(401, "not_paired", "open the interface from `readme-monitor ui`")
                if method != "GET":
                    if self.headers.get("Origin") != f"http://{host}":
                        raise ApiError(403, "bad_origin", "cross-site request refused")
                    if not hmac.compare_digest((self.headers.get("X-RM-CSRF") or "").encode(), self.app.sessions[session].encode()):
                        raise ApiError(403, "bad_csrf", "missing or wrong X-RM-CSRF header")
                return self._api(method, url.path, query, session)
            if method != "GET":
                raise ApiError(405, "method_not_allowed", "use GET")
            return self._static(unquote(url.path))
        except ApiError as e:
            self._json(e.status, {"error": {"code": e.code, "message": e.message}})
        except ConfigError as e:
            self._json(400, {"error": {"code": "invalid_config", "message": credentials.redact(str(e))[0]}})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            log.error("ui request failed: %s", credentials.redact(f"{type(e).__name__}: {e}")[0])
            try:
                self._json(500, {"error": {"code": "internal", "message": "The request failed; see the log in the runtime folder."}})
            except OSError:
                pass

    def _auth(self, query):
        code = (query.get("code") or [""])[0]
        path = pair_path(self.app.home)
        with self.app.pair_lock:
            rec = None if path.is_symlink() else runtime._read(path)
            fresh = bool(rec) and isinstance(rec.get("code"), str) and time.time() - float(rec.get("created") or 0) <= PAIR_SECONDS
            ok = fresh and hmac.compare_digest(rec["code"].encode(), code.encode())
            if ok or (rec and not fresh) or path.is_symlink():
                path.unlink(missing_ok=True)  # single use; an expired code is removed too
        if not ok:
            return self._page(403, "This sign-in link is no longer valid. Run <code>readme-monitor ui</code> again.")
        token = secrets.token_urlsafe(32)
        self.app.sessions[token] = secrets.token_urlsafe(32)
        self._send(302, b"", "text/plain", {"Location": "/", "Set-Cookie": f"rm_session={token}; HttpOnly; SameSite=Strict; Path=/",
                                           "Cache-Control": "no-store"})

    def _static(self, path: str):
        root = WEB_ROOT.resolve()
        try:
            target = (root / path.lstrip("/")).resolve()
        except (OSError, ValueError):
            raise ApiError(404, "not_found", "not found") from None
        if not target.is_relative_to(root):
            raise ApiError(404, "not_found", "not found")
        index = root / "index.html"
        if target.is_file() and target != index:
            cache = "public, max-age=31536000, immutable" if target.is_relative_to(root / "assets") else "no-cache"
            return self._send(200, target.read_bytes(), TYPES.get(target.suffix.lower(), "application/octet-stream"), {"Cache-Control": cache})
        if self._session() is None:
            return self._page(200, NOT_PAIRED)
        if not index.is_file():
            return self._page(200, NO_BUILD)
        self._send(200, index.read_bytes(), TYPES[".html"], {"Cache-Control": "no-cache"})

    def _api(self, method: str, path: str, query, session: str):
        allowed = False
        for m, pattern, name in ROUTES:
            match = pattern.match(path)
            if match:
                allowed = True
                if m == method:
                    break
        else:
            raise (ApiError(405, "method_not_allowed", "method not allowed") if allowed else ApiError(404, "not_found", "unknown endpoint"))
        body = self._body() if method in ("POST", "PUT") else {}
        ctx = self.app.ctx()
        try:
            with self.app.config_lock if name in CONFIG_WRITERS else contextlib.nullcontext():
                result = getattr(self, "api_" + name)(ctx, match.groups(), query, body, session)
        finally:
            if ctx._db is not None:
                ctx._db.close()
        if result is not None:
            self._json(200, result)

    # ---- read endpoints ---------------------------------------------------
    def api_session(self, ctx, g, q, b, session):
        return {"csrf": self.app.sessions[session], "version": __version__, "simulation": bool(self.app.demo)}

    def api_get_state(self, ctx, g, q, b, s):
        return self.app.state(ctx)

    def api_events(self, ctx, g, q, b, s):
        after = _int(q, "after")
        events = ctx.db.events(after=after, limit=_int(q, "limit", 200, 1, 500), run_id=_int(q, "run_id"),
                               repo=(q.get("repo") or [None])[0])
        return {"events": events, "cursor": ctx.db.last_event_id()}

    def api_stream(self, ctx, g, q, b, s):
        """Server-sent events. Only reads; a reconnect resumes after Last-Event-ID and never starts anything."""
        app, db = self.app, ctx.db
        last = self.headers.get("Last-Event-ID")
        try:
            cursor = int(last) if last else _int(q, "after")
        except ValueError:
            cursor = None
        if cursor is None:
            cursor = db.last_event_id()  # a new client gets what happens from now on; history is /api/events
        self._headers(200, "text/event-stream; charset=utf-8", None, api=True)
        beat = time.monotonic()
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while not app.stop.is_set():
                rows = db.events(after=cursor, limit=500)
                for e in rows:
                    self.wfile.write(b"id: %d\nevent: activity\ndata: " % e["id"] + self.app.scrub(_json_bytes(e)) + b"\n\n")
                    cursor = e["id"]
                if rows:
                    self.wfile.flush()
                if time.monotonic() - beat >= app.heartbeat:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    beat = time.monotonic()
                if self._client_gone():
                    break
                app.stop.wait(app.poll)
        except OSError:
            pass  # the client went away
        return None

    def _client_gone(self) -> bool:
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def api_runs(self, ctx, g, q, b, s):
        return {"runs": [App.run_view(r) for r in ctx.db.last_runs(_int(q, "limit", 20, 1, 200))]}

    def api_run(self, ctx, g, q, b, s):
        row = ctx.db.conn.execute("SELECT * FROM runs WHERE id=?", (int(g[0]),)).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "no such run")
        return {"run": App.run_view(row), "events": ctx.db.events(after=0, limit=20_000, run_id=int(g[0]))}

    def api_draft(self, ctx, g, q, b, s):
        d = ctx.db.draft(int(g[0]))
        if d is None:
            raise ApiError(404, "not_found", "no such draft (drafts are kept for the newest 300)")
        return d

    def api_usage(self, ctx, g, q, b, s):
        db = ctx.db
        rows = [{k: r[k] for k in ("id", "repo", "run_id", "model", "state", "reserved_usd", "actual_usd", "input_tokens",
                                   "output_tokens", "created_at")}
                for r in db.conn.execute("SELECT * FROM usage ORDER BY id DESC LIMIT 100")]
        since = datetime.fromtimestamp(time.time() - 30 * 86400, timezone.utc).strftime("%Y-%m-%d")
        spent = SPENT_SQL.replace("SELECT COALESCE(SUM(", "SELECT substr(created_at, 1, 10) AS day, COALESCE(SUM(")
        by_day = [{"day": r[0], "usd": round(r[1], 6)} for r in db.conn.execute(spent + " GROUP BY day ORDER BY day", (since,))]
        return {"rows": rows, "by_day": by_day}

    def api_diagnostics(self, ctx, g, q, b, s):
        return {"checks": self.app.diagnostics(ctx), "generated_at": _now()}

    # ---- installation -----------------------------------------------------
    def api_activate(self, ctx, g, q, body, s):
        _confirmed(body)
        if self.app.demo:
            self.app.sim_active, self.app.sim_task = True, ctx.cfg.schedule
        else:
            try:
                runtime.activate(self.app.home, scheduler.current_user(), ctx.host_lock())
            except runtime.NotActivated as e:
                raise ApiError(409, "refused", str(e)) from None
        return {"ok": True, "installation": self.app.state(ctx)["installation"]}

    def api_deactivate(self, ctx, g, q, body, s):
        _confirmed(body)
        if self.app.demo:
            self.app.sim_active, self.app.sim_task = False, None
            return {"ok": True, "task_removed": True, "revoked": "simulation"}
        return {"ok": True, **runtime.deactivate(self.app.home)}

    def api_set_home(self, ctx, g, q, body, s):
        app = self.app
        if app.demo:
            raise ApiError(409, "simulation", "the simulation keeps its temporary folder")
        if app.active(ctx):
            raise ApiError(409, "active", "turn the worker off for this folder before choosing another one")
        raw = body.get("path")
        if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
            raise ApiError(400, "bad_request", "path must be a folder path")
        p = Path(raw.strip())
        if not p.is_absolute() or raw.startswith(("\\\\", "//")):
            raise ApiError(400, "bad_path", "use an absolute path on a local drive")
        p = p.resolve()
        checkout = PACKAGE.parent if (PACKAGE.parent / "pyproject.toml").exists() else PACKAGE
        if p.is_relative_to(PACKAGE) or p.is_relative_to(checkout):
            raise ApiError(400, "bad_path", "the runtime folder must be outside the program's own files")
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / f".write-test-{secrets.token_hex(4)}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            raise ApiError(400, "not_writable", f"the folder cannot be created or written ({type(e).__name__})") from None
        if not (p / "config.toml").exists():
            shutil.copyfile(cli.EXAMPLE_CONFIG, p / "config.toml")
        runtime._write(runtime.pointer_path(), {"home": str(p)})  # commands without --home now use it
        old = app.home
        app.home = p
        runtime._write(p / SERVER_FILE, {"port": app.port, "pid": os.getpid()})
        (old / SERVER_FILE).unlink(missing_ok=True)
        return {"ok": True, "home": str(p)}

    # ---- credentials ------------------------------------------------------
    def api_store_credential(self, ctx, g, q, body, s):
        kind, value = g[0], body.get("value")
        if not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(c.isspace() for c in value.strip()):
            raise ApiError(400, "bad_request", "value must be the credential text")
        value = value.strip()
        if self.app.demo:  # accepted and discarded
            return {"ok": True, **({"login": simulation_owner(), "scopes": "repo", "token_kind": "classic"} if kind == "github" else {})}
        answer = {}
        if kind == "github":
            gh = GitHub(value)
            try:
                login = gh.whoami()["login"]  # one read-only identity request
            except GitHubError as e:
                raise ApiError(400, "credential_rejected", f"GitHub did not accept this credential (HTTP {e.status}); nothing was stored."
                               if e.status else "GitHub could not be reached; nothing was stored.") from None
            answer = {"login": login, "scopes": gh.scopes, "token_kind": "classic" if gh.scopes is not None else "fine-grained or app"}
        try:
            credentials.store(kind, value)
        except Exception as e:
            raise ApiError(500, "credential_store_unavailable", f"the operating system's credential store refused ({type(e).__name__})") from None
        if kind == "github":
            ctx.db.set_setting("github_login", answer["login"])
        return {"ok": True, **answer}

    def api_remove_credential(self, ctx, g, q, b, s):
        if self.app.demo:
            return {"ok": True, "removed": True}
        try:
            removed = credentials.remove(g[0])
        except Exception as e:
            raise ApiError(500, "credential_store_unavailable", f"the operating system's credential store refused ({type(e).__name__})") from None
        if g[0] == "github":
            ctx.db.set_setting("github_login", "")
        return {"ok": True, "removed": removed}

    def api_discover(self, ctx, g, q, b, s):
        """Read-only listing; nothing is recorded."""
        cfg = ctx.cfg
        if self.app.demo:
            from .simulation import OWNER

            owned = [{"id": f.id, "full_name": n, "owner": {"login": OWNER}, "private": True, "visibility": "private",
                      "default_branch": f.default_branch, "archived": f.archived, "fork": False}
                     for n, f in ((f"{OWNER}/{k.split('/')[1]}", f) for k, f in self.app.demo.account.repos.items())]
            login, coverage = OWNER, {"owner": OWNER, "listed": len(owned), "public": 0, "private": len(owned), "skipped_not_owned": 0,
                                      "complete": True, "limitations": [], "token_kind": "simulation"}
        else:
            token, _ = credentials.get("github")
            if not token:
                raise ApiError(409, "credentials_missing", "store a GitHub credential first")
            gh = GitHub(token)
            try:
                login = gh.whoami()["login"]
                owned, coverage = discovery.list_owned(gh, login)
            except GitHubError as e:
                raise ApiError(502, "github_error", credentials.redact(str(e))[0][:300]) from None
        inventory = cli.discover_inventory(cfg, [dict(r) for r in ctx.db.repos()], owned)
        return {"ok": True, "login": login, "coverage": coverage,
                "repositories": [{"id": i["id"], "name": i["name"], "visibility": i["visibility"], "archived": i["archived"],
                                  "fork": i["fork"], "default_branch": i["default_branch"], "enrolled": i["configured"],
                                  "disposition": i["disposition"], "detail": i["detail"]} for i in inventory]}

    # ---- configuration ----------------------------------------------------
    def api_put_repo(self, ctx, g, q, body, s):
        name = _repo_name(*g)
        unknown = set(body) - set(REPO_KEYS) - {"confirm_export", "confirm_direct"}
        if unknown:
            raise ApiError(400, "bad_request", f"unknown fields: {sorted(unknown)}")
        if body.get("managed_sections") == []:  # config would read an empty list as "the default sections"
            raise ApiError(400, "bad_request", "Choose at least one managed section, or pause this repository instead.")
        raw = config.load_raw(ctx.config_path)
        entries = raw.setdefault("repo", [])
        entry = next((e for e in entries if str(e.get("name", "")).lower() == name.lower()), None)
        cur = entry or {}
        export = body.get("api_export", cur.get("api_export", "none"))
        if export == "allowlist":
            if ctx.cfg.is_restricted(name) or ctx.db.is_restricted(name):
                raise ApiError(409, "export_restricted", f"{name} is export-restricted; its source can never be sent to Claude")
            if cur.get("api_export") != "allowlist" and body.get("confirm_export") is not True:
                raise ApiError(400, "confirmation_required", "sending source files to Claude needs confirm_export: true")
        if body.get("publish_mode") == "direct" and cur.get("publish_mode") != "direct" and body.get("confirm_direct") is not True:
            raise ApiError(400, "confirmation_required", "committing straight to the branch needs confirm_direct: true")
        if entry is None:
            entry = {"name": name}
            entries.append(entry)
        _set(entry, body, REPO_KEYS)
        config.save(ctx.config_path, raw)
        return {"ok": True}

    def api_delete_repo(self, ctx, g, q, b, s):
        name = _repo_name(*g)
        raw = config.load_raw(ctx.config_path)
        kept = [e for e in raw.get("repo", []) if str(e.get("name", "")).lower() != name.lower()]
        if len(kept) == len(raw.get("repo", [])):
            raise ApiError(404, "not_found", f"{name} has no [[repo]] entry")
        raw["repo"] = kept
        if not kept:
            raw.pop("repo")
        config.save(ctx.config_path, raw)
        return {"ok": True}

    def api_settings(self, ctx, g, q, body, s):
        unknown = set(body) - {"budget", "model", "effort", "policy", "discovery"}
        if unknown:
            raise ApiError(400, "bad_request", f"unknown fields: {sorted(unknown)}")
        for k in ("budget", "policy", "discovery"):
            if k in body and not isinstance(body[k], dict):
                raise ApiError(400, "bad_request", f"{k} must be an object")
        if body.get("model") is not None and (not isinstance(body["model"], str) or not body["model"].strip()):
            raise ApiError(400, "bad_request", "model must be a model id, not empty")
        raw = config.load_raw(ctx.config_path)
        monitor = raw.setdefault("monitor", {})
        if "budget" in body:
            _set(raw.setdefault("budget", {}), body["budget"], ("per_run_usd", "daily_usd", "monthly_usd", "pacing"))
        _set(monitor, body, ("model", "effort"))
        if "policy" in body:
            _set(monitor, body["policy"], ("quiet_minutes", "min_generation_interval_hours", "max_postpone_hours"))
            _set(raw.setdefault("policy", {}), body["policy"], ("export_restricted",))
        if "discovery" in body:
            _set(raw.setdefault("discovery", {}), body["discovery"], ("owner", "publish_mode", "adoptions_per_day", "exclude"))
        config.save(ctx.config_path, raw)
        return {"ok": True}

    def api_schedule(self, ctx, g, q, body, s):
        app = self.app
        unknown = set(body) - set(SCHEDULE_KEYS)
        if unknown:
            raise ApiError(400, "bad_request", f"unknown fields: {sorted(unknown)}")
        if body.get("mode", "interval") == "interval" and isinstance(body.get("interval_minutes"), int) and not 5 <= body["interval_minutes"] <= 1440:
            raise ApiError(400, "bad_request", "interval_minutes must be between 5 and 1440")
        raw = config.load_raw(ctx.config_path)
        raw["schedule"] = {**raw.get("schedule", {}), **body}
        config.save(ctx.config_path, raw)
        ctx._cfg = None
        ctx.db.set_setting("schedule_saved_at", _now())
        if not app.active(ctx):
            return {"ok": True, "applied": False, "reason": "inactive", "schedule": app.schedule_view(ctx)}
        if app.demo:
            app.sim_task = ctx.cfg.schedule
        else:
            try:
                cli.register_task(ctx, ctx.cfg.schedule)  # only replaces the definition; a running check continues
            except RuntimeError as e:
                raise ApiError(502, "task_registration_failed", f"Task Scheduler refused the schedule: {credentials.redact(str(e))[0][:300]}") from None
        return {"ok": True, "applied": True, "schedule": app.schedule_view(ctx)}

    # ---- work ---------------------------------------------------------------
    def _pause(self, ctx, body, paused: bool):
        repo = body.get("repo")
        if repo is None:
            ctx.db.set_setting("paused", "1" if paused else "0")
        else:
            if not isinstance(repo, str):
                raise ApiError(400, "bad_request", "repo must be owner/name")
            ctx.db.update_repo(discovery.find_repo(ctx.cfg, ctx.db, repo).name, paused=int(paused))
        return {"ok": True}

    def api_pause(self, ctx, g, q, body, s):
        return self._pause(ctx, body, True)

    def api_resume(self, ctx, g, q, body, s):
        return self._pause(ctx, body, False)

    def _start(self, ctx, dry_run: bool, repos=None, adopt=None) -> dict:
        app = self.app
        with app.run_lock:  # two rapid requests start one cycle
            if app.busy():
                return {"ok": True, "started": False, "reason": "already_running"}
            if not dry_run:
                if not app.active(ctx):
                    return {"ok": True, "started": False, "reason": "inactive"}
                if ctx.db.globally_paused:
                    return {"ok": True, "started": False, "reason": "paused"}
            if not app.credential("github")[0]:
                return {"ok": True, "started": False, "reason": "credentials_missing"}
            app.start(dry_run, repos, adopt)
        return {"ok": True, "started": True}

    def api_run_cycle(self, ctx, g, q, body, s):
        kind, repos = body.get("kind"), body.get("repos")
        if kind not in ("live", "dry_run"):
            raise ApiError(400, "bad_request", 'kind must be "live" or "dry_run"')
        if repos is not None and (not isinstance(repos, list) or not all(isinstance(r, str) and REPO_RE.match(r) for r in repos)):
            raise ApiError(400, "bad_request", "repos must be a list of owner/name")
        return self._start(ctx, kind == "dry_run", [discovery.find_repo(ctx.cfg, ctx.db, r).name for r in repos] if repos else None)

    def api_adopt(self, ctx, g, q, body, s):
        _confirmed(body)
        return self._start(ctx, False, adopt=discovery.find_repo(ctx.cfg, ctx.db, _repo_name(*g)).name)

    def api_cancel(self, ctx, g, q, b, s):
        running = self.app.busy()
        if running:
            ctx.db.request_cancel()
        return {"ok": True, "requested": running}


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let two servers share a port
    daemon_threads = True


def serve(app: App, port: int = 8765, tries: int = 20) -> Server:
    """Bind 127.0.0.1 on `port` or the next free one (port 0: any free port)."""
    last = None
    for p in ([0] if port == 0 else range(port, port + tries)):
        try:
            httpd = Server(("127.0.0.1", p), Handler)
        except OSError as e:
            last = e
            continue
        httpd.app = app
        app.port = httpd.server_address[1]
        return httpd
    raise RuntimeError(f"no free port between {port} and {port + tries - 1}: {last}")


def stop(app: App, httpd: Server):
    app.stop.set()
    httpd.shutdown()
    httpd.server_close()


def _show(url: str, browser: bool):
    if browser and webbrowser.open(url):
        cli.out(f"Opened the ReadmeMonitor setup page in your browser. If it did not appear, open (once, within two minutes): {url}")
    else:
        cli.out(f"Open this address in your browser (once, within two minutes): {url}")


DEMO_PREFIX = "readme-monitor-demo-"


def clean_stale_demos(max_age: float = 86400) -> list[Path]:
    """Remove simulation folders left behind by a killed process: only readme-monitor-demo-* folders
    in the system temp directory, older than a day, whose server pid is not alive."""
    removed = []
    for d in Path(tempfile.gettempdir()).glob(DEMO_PREFIX + "*"):
        try:
            if d.is_symlink() or not d.is_dir() or time.time() - d.stat().st_mtime < max_age:
                continue
            rec = runtime._read(d / SERVER_FILE)
            if rec and pid_alive(rec.get("pid")):
                continue
        except OSError:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d)
    return removed


def launch(home: Path, port: int = 8765, browser: bool = True, demo: bool = False) -> int:
    if demo:
        clean_stale_demos()
        home = Path(tempfile.mkdtemp(prefix=DEMO_PREFIX))
    else:
        existing = running_port(home)
        if existing:
            _show(f"http://127.0.0.1:{existing}/auth?code={write_pairing(home)}", browser)
            return 0
    app = App(home, demo=demo)
    httpd = serve(app, port)
    runtime._write(app.home / SERVER_FILE, {"port": app.port, "pid": os.getpid()})
    _show(f"http://127.0.0.1:{app.port}/auth?code={write_pairing(app.home)}", browser)
    cli.out("Simulation: nothing is sent to GitHub or Anthropic and Task Scheduler is not touched." if demo else
            f"Serving {app.home} on 127.0.0.1:{app.port}. Press Ctrl+C to stop; running checks continue.")
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.5)  # a short join keeps Ctrl+C responsive on Windows
    except KeyboardInterrupt:
        pass
    finally:
        stop(app, httpd)
        rec = runtime._read(app.home / SERVER_FILE)
        if rec and rec.get("pid") == os.getpid():
            (app.home / SERVER_FILE).unlink(missing_ok=True)
        if demo:
            if app.thread is not None:
                app.thread.join(30)
            shutil.rmtree(app.home, ignore_errors=True)
    return 0
