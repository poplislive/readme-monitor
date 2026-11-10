"""Local API server against a real HTTP server on an ephemeral loopback port: security, read
shapes for a fresh home, and the full fresh-user sequence with patched identity, an in-memory
credential store and the fake GitHub. No real GitHub, Anthropic, keyring or Task Scheduler."""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

from readme_monitor import cli, credentials, runtime, scheduler, server
from readme_monitor.simulation import FakeAccount, FakeGitHub, _readme

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2
KEY = "sk-ant-api03-" + "Zz9Yy8Xx7Ww6Vv5Uu4" * 2
REPO = "your-account/your-repository"


class Client:
    def __init__(self, port: int):
        self.port, self.cookie, self.csrf, self.seen = port, None, None, []

    def req(self, method, path, body=None, host=None, origin=True, csrf=True, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if self.cookie:
            h["Cookie"] = f"rm_session={self.cookie}"
        if method != "GET":
            if origin:
                h["Origin"] = f"http://127.0.0.1:{self.port}"
            if csrf and self.csrf:
                h["X-RM-CSRF"] = self.csrf
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if data is not None:
            h["Content-Length"] = str(len(data))
        h.update(headers or {})
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        c.endheaders(data)
        r = c.getresponse()
        b = r.read()
        c.close()
        self.seen.append(b)
        return r.status, {k.lower(): v for k, v in r.getheaders()}, b

    def json(self, method, path, body=None, **kw):
        status, _, b = self.req(method, path, body, **kw)
        return status, json.loads(b) if b else None

    def pair(self, home: Path):
        status, headers, _ = self.req("GET", f"/auth?code={server.write_pairing(home)}")
        assert status == 302 and headers["location"] == "/"
        assert "HttpOnly" in headers["set-cookie"] and "SameSite=Strict" in headers["set-cookie"]
        self.cookie = re.search(r"rm_session=([^;]+)", headers["set-cookie"])[1]
        self.csrf = self.json("GET", "/api/session")[1]["csrf"]
        return self


@pytest.fixture
def start(monkeypatch):
    monkeypatch.setattr(runtime, "hostname", lambda: "WORKSTATION-1")
    monkeypatch.setattr(scheduler, "current_user", lambda: "WORKSTATION-1\\person")
    started = []

    def make(home: Path, demo=False, latency=(0.0, 0.0), pair=True):
        app = server.App(home, demo=demo, latency=latency)
        httpd = server.serve(app, 0)
        app.heartbeat = 0.3
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append((app, httpd))
        client = Client(app.port)
        return app, (client.pair(app.home) if pair else client)

    yield make
    for app, httpd in started:
        server.stop(app, httpd)
        if app.thread is not None:
            app.thread.join(30)
        if app.proc is not None and hasattr(app.proc, "join"):
            app.proc.join(30)


@pytest.fixture
def vault(monkeypatch):
    """In-memory stand-in for the operating system's credential store."""
    store: dict[str, str] = {}
    monkeypatch.setattr(credentials, "get", lambda kind: (store.get(kind), "credential-manager" if kind in store else "missing"))
    monkeypatch.setattr(credentials, "store", lambda kind, value: store.__setitem__(kind, value))
    monkeypatch.setattr(credentials, "remove", lambda kind: store.pop(kind, None) is not None)
    return store


def wait(cond, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    raise AssertionError("condition not reached")


# ---- security ----------------------------------------------------------------------------


def test_pairing_is_single_use_and_expires(start, tmp_path):
    app, c = start(tmp_path / "home", pair=False)
    code = server.write_pairing(app.home)
    path = server.pair_path(app.home)
    assert path.parent == runtime.pointer_path().parent / ".readme-monitor-ui"  # the account profile, not the runtime folder
    assert path.exists() and not list(app.home.glob("*pair*"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    assert c.req("GET", f"/auth?code=wrong")[0] == 403
    assert c.req("GET", f"/auth?code={code}")[0] == 302
    assert not path.exists()
    assert c.req("GET", f"/auth?code={code}")[0] == 403  # single use
    runtime._write(path, {"code": "old-code", "created": time.time() - server.PAIR_SECONDS - 5})
    assert c.req("GET", "/auth?code=old-code")[0] == 403
    assert not path.exists()


def test_pairing_file_that_is_a_symlink_is_refused(start, tmp_path):
    app, c = start(tmp_path / "home", pair=False)
    planted = tmp_path / "planted.json"
    runtime._write(planted, {"code": "planted", "created": time.time()})
    path = server.pair_path(app.home)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(planted)
    except OSError:
        pytest.skip("creating symlinks needs a privilege this account lacks")
    assert c.req("GET", "/auth?code=planted")[0] == 403
    assert not path.is_symlink() and planted.exists()


def test_session_host_origin_and_csrf_are_enforced(start, tmp_path):
    app, c = start(tmp_path)
    anon = Client(app.port)
    assert anon.req("GET", "/api/state")[0] == 401
    assert anon.req("GET", "/api/events/stream")[0] == 401
    status, _, page = anon.req("GET", "/")
    assert status == 200 and b"readme-monitor ui" in page
    assert c.req("GET", "/api/state", host="evil.example")[0] == 403
    assert c.req("GET", "/api/state", host=f"attacker.test:{app.port}")[0] == 403
    assert c.json("GET", "/api/state", host=f"localhost:{app.port}")[0] == 200
    assert c.req("POST", "/api/pause", {}, origin=False)[0] == 403
    assert c.req("POST", "/api/pause", {}, csrf=False)[0] == 403
    assert c.req("POST", "/api/pause", {}, headers={"Origin": "http://evil.example"})[0] == 403
    assert c.req("POST", "/api/pause", {}, headers={"X-RM-CSRF": "guess"})[0] == 403
    assert c.json("POST", "/api/pause", {}) == (200, {"ok": True})
    assert c.req("GET", "/api/nothing-here")[0] == 404
    assert c.req("DELETE", "/api/state")[0] == 405
    assert c.req("POST", "/api/pause", raw=b"x" * (server.MAX_BODY + 1), headers={"Content-Type": "application/json"})[0] == 413
    assert c.req("POST", "/api/pause", raw=b"repo=x", headers={"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
    assert c.req("POST", "/api/pause", raw=b"[1]", headers={"Content-Type": "application/json"})[0] == 400


def test_security_headers_static_files_and_traversal(start, tmp_path, monkeypatch):
    web = tmp_path / "pkg" / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    (web / "assets" / "app-1a2b3c.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "pkg" / "secret.txt").write_text("outside the web root", encoding="utf-8")
    monkeypatch.setattr(server, "WEB_ROOT", web)
    app, c = start(tmp_path / "home")
    for path in ("/api/state", "/", "/assets/app-1a2b3c.js", "/api/missing", "/some/page"):
        status, h, _ = c.req("GET", path)
        assert "default-src 'self'" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer"
    assert c.req("GET", "/api/state")[1]["cache-control"] == "no-store"
    status, h, body = c.req("GET", "/assets/app-1a2b3c.js")
    assert status == 200 and h["content-type"].startswith("text/javascript") and "immutable" in h["cache-control"]
    status, h, body = c.req("GET", "/settings/schedule")  # single-page app route
    assert status == 200 and b"<title>app</title>" in body and h["cache-control"] == "no-cache"
    for evil in ("/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/assets/..%5c..%5csecret.txt"):
        status, _, body = c.req("GET", evil)
        assert b"outside the web root" not in body, evil
    monkeypatch.setattr(server, "WEB_ROOT", tmp_path / "missing")
    status, _, body = c.req("GET", "/")
    assert status == 200 and b"npm run build" in body


# ---- fresh user --------------------------------------------------------------------------

STATE_KEYS = {"version", "simulation", "now", "cursor", "installation", "runtime", "credentials", "budget", "model", "policy",
              "schedule", "discovery", "repos", "onboarding"}


def assert_shapes(c: Client):
    st, session = c.json("GET", "/api/session")
    assert st == 200 and set(session) == {"csrf", "version", "simulation"}
    st, state = c.json("GET", "/api/state")
    assert st == 200 and set(state) == STATE_KEYS
    assert set(state["installation"]) == {"home", "host", "account", "platform", "supported", "active", "problem", "activation",
                                          "host_lock", "can_change_home"}
    assert set(state["runtime"]) == {"state", "detail", "current_run", "last_run", "next_check"}
    assert state["runtime"]["state"] in ("inactive", "unavailable", "paused", "running", "scheduled_idle", "manual_idle")
    assert set(state["credentials"]["github"]) == {"present", "source", "login"}
    assert set(state["budget"]) == {"configured", "per_run_usd", "daily_usd", "monthly_usd", "pacing", "spent_today_usd",
                                    "spent_month_usd", "pacing_limit_today_usd", "open_reservations", "open_reserved_usd"}
    assert set(state["model"]) == {"id", "effort", "priced", "provider"}
    assert set(state["schedule"]) == {"configured", "effective", "in_sync", "task", "summary", "next_checks", "timezone_name", "notes"}
    assert set(state["schedule"]["configured"]) == {"mode", "interval_minutes", "time", "weekdays", "timezone"}
    assert [s["id"] for s in state["onboarding"]["steps"]] == ["runtime", "github", "repositories", "model", "publication", "dry_run"]
    for r in state["repos"]:
        assert {"name", "repo_id", "visibility", "source", "enrolled", "mode", "api_export", "restricted", "branch", "readme_paths",
                "managed_sections", "disabled_sections", "source_include", "source_exclude", "paused", "disposition", "detail", "state",
                "state_detail", "head", "last_checked", "pending_since", "next_eligible", "last_generated", "last_published",
                "proposal", "latest_draft_id", "spent_month_usd"} == set(r)
    st, ev = c.json("GET", "/api/events?limit=5")
    assert st == 200 and set(ev) == {"events", "cursor"}
    assert c.json("GET", "/api/runs")[1].keys() == {"runs"}
    assert set(c.json("GET", "/api/usage")[1]) == {"rows", "by_day"}
    st, diag = c.json("GET", "/api/diagnostics")
    assert st == 200 and set(diag) == {"checks", "generated_at"} and all(set(x) == {"status", "name", "detail"} for x in diag["checks"])
    return state


def test_fresh_home_read_endpoints(start, tmp_path, vault):
    app, c = start(tmp_path / "fresh")
    state = assert_shapes(c)
    assert state["simulation"] is False and state["repos"] == [] and state["cursor"] == 0
    assert state["installation"]["active"] is False and state["installation"]["can_change_home"] is True
    assert state["installation"]["host"] == "WORKSTATION-1"
    assert state["runtime"]["state"] == "inactive"
    assert state["credentials"]["github"] == {"present": False, "source": "missing", "login": None}
    assert state["onboarding"]["complete"] is False
    assert c.json("GET", "/api/runs/1")[0] == 404 and c.json("GET", "/api/drafts/1")[0] == 404


class Account(FakeAccount):
    """The fake GitHub behind the fresh user's credential."""

    def __init__(self):
        super().__init__("your-account")
        files = {"src/app.py": "def main():\n    return 0\n", "pyproject.toml": '[project]\nname = "app"\n'}
        self.fake = self.add(REPO, FakeGitHub({**files, "README.md": _readme(REPO, ("project-structure",), files)}))
        self.tokens = []

    def whoami(self):
        return {"login": self.owner, "public_repos": 0, "owned_private_repos": 1}

    def owned_repos(self):
        return [{"id": self.fake.id, "full_name": REPO, "owner": {"login": self.owner}, "private": True, "visibility": "private",
                 "default_branch": "main", "archived": False, "fork": False}]


class InProcess:
    """Stands in for the detached process: runs the same CLI command in a thread."""

    def __init__(self, argv):
        self.argv = argv
        self.t = threading.Thread(target=cli.main, args=(argv,), daemon=True)
        self.t.start()

    def poll(self):
        return None if self.t.is_alive() else 0

    def join(self, timeout):
        self.t.join(timeout)


class Recorded:
    def __init__(self, argv, running=False):
        self.argv, self.running = argv, running

    def poll(self):
        return None if self.running else 0


def fake_task_scheduler(monkeypatch):
    """Task Scheduler that keeps the registered definition, so the read-back is the real parser's."""
    held = {}

    def schtasks(*args):
        if args[0] == "/Create":
            held["xml"] = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
            return subprocess.CompletedProcess(args, 0, "SUCCESS", "")
        if args[0] == "/Query" and "/XML" in args and "xml" in held:
            return subprocess.CompletedProcess(args, 0, held["xml"], "")
        if args[0] == "/Query" and "xml" in held:
            return subprocess.CompletedProcess(args, 0, "Status: Ready\nNext Run Time: N/A\nLast Result: 0\n", "")
        if args[0] == "/Delete":
            held.clear()
            return subprocess.CompletedProcess(args, 0, "SUCCESS", "")
        return subprocess.CompletedProcess(args, 1, "", "ERROR: The system cannot find the file specified.")

    monkeypatch.setattr(scheduler, "_schtasks", schtasks)
    return held


def enable_identity(monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", True)
    monkeypatch.setattr(runtime, "machine_guid", lambda: "00000000-test-guid")


def test_full_fresh_user_sequence(start, tmp_path, vault, monkeypatch):
    account = Account()
    monkeypatch.setattr(server, "GitHub", lambda token, **kw: account.tokens.append(token) or account)
    monkeypatch.setattr(cli, "GitHub", lambda token, **kw: account)
    spawned = []

    def spawn(self, args):
        argv = [*args, "--home", str(self.home)]
        proc = InProcess(argv) if "--dry-run" in args else Recorded(argv)  # a live run would call the model: recorded only
        spawned.append(proc)
        return proc

    monkeypatch.setattr(server.App, "spawn", spawn)
    held = fake_task_scheduler(monkeypatch)
    app, c = start(tmp_path / "first")

    # runtime folder
    new_home = tmp_path / "runtime"
    assert c.json("POST", "/api/installation/home", {"path": "relative/folder"})[0] == 400
    st, r = c.json("POST", "/api/installation/home", {"path": str(new_home)})
    assert st == 200 and r["home"] == str(new_home.resolve())
    assert json.loads(runtime.pointer_path().read_text())["home"] == str(new_home.resolve())
    assert (new_home / "config.toml").exists()

    # credentials
    assert c.json("POST", "/api/credentials/github", {"value": TOKEN}) == (200, {"ok": True, "login": "your-account", "scopes": "repo",
                                                                               "token_kind": "classic"})
    assert c.json("POST", "/api/credentials/anthropic", {"value": KEY})[0] == 200
    assert vault == {"github": TOKEN, "anthropic": KEY} and account.tokens == [TOKEN]

    # discovery (read-only)
    st, found = c.json("POST", "/api/github/discover")
    assert st == 200 and found["login"] == "your-account" and found["coverage"]["complete"] is True
    assert [(x["name"], x["enrolled"]) for x in found["repositories"]] == [(REPO, False)]

    # enroll with options
    path = f"/api/repos/{REPO}"
    opts = {"publish_mode": "review_pr", "managed_sections": ["features", "project-structure"], "source_include": ["src/**/*.py"]}
    assert c.json("PUT", path, {**opts, "api_export": "allowlist"})[1]["error"]["code"] == "confirmation_required"
    assert c.json("PUT", path, {**opts, "publish_mode": "direct"})[1]["error"]["code"] == "confirmation_required"
    assert c.json("PUT", path, {**opts, "api_export": "allowlist", "confirm_export": True}) == (200, {"ok": True})
    assert c.json("PUT", path, {"managed_sections": ["no-such-section"]})[1]["error"]["code"] == "invalid_config"

    # budget and schedule (inactive: saved, not applied)
    assert c.json("PUT", "/api/settings", {"budget": {"per_run_usd": 0.25, "daily_usd": 0.5, "monthly_usd": 3.0, "pacing": "even"}})[0] == 200
    st, r = c.json("PUT", "/api/schedule", {"mode": "daily", "time": "08:30", "timezone": "local"})
    assert st == 200 and r["applied"] is False and r["reason"] == "inactive" and "xml" not in held

    # an unactivated installation refuses live work but allows a dry run
    assert c.json("POST", "/api/run", {"kind": "live"})[1] == {"ok": True, "started": False, "reason": "inactive"}
    assert c.json("POST", f"{path}/adopt", {"confirm": True})[1]["reason"] == "inactive"
    assert spawned == []
    st, r = c.json("POST", "/api/run", {"kind": "dry_run"})
    assert r == {"ok": True, "started": True} and spawned[0].argv[:2] == ["run-once", "--dry-run"]
    spawned[0].join(60)
    state = c.json("GET", "/api/state")[1]
    assert state["runtime"]["last_run"]["trigger"] == "dry_run" and state["runtime"]["last_run"]["outcome"] == "ok"
    steps = {s["id"]: s["done"] for s in state["onboarding"]["steps"]}
    assert steps == {"runtime": False, "github": True, "repositories": True, "model": True, "publication": True, "dry_run": True}
    repo = next(x for x in state["repos"] if x["name"] == REPO)
    assert repo["enrolled"] and repo["mode"] == "review_pr" and repo["api_export"] == "allowlist" and repo["latest_draft_id"]
    st, draft = c.json("GET", f"/api/drafts/{repo['latest_draft_id']}")
    assert st == 200 and draft["kind"] == "dry_run" and draft["repo"] == REPO
    assert account.fake.writes == []  # a dry run writes nothing to GitHub

    # activate, then the schedule is registered and read back
    enable_identity(monkeypatch)
    assert c.json("POST", "/api/installation/activate", {})[0] == 400  # needs confirm
    st, r = c.json("POST", "/api/installation/activate", {"confirm": True})
    assert st == 200 and r["installation"]["active"] is True and r["installation"]["can_change_home"] is False
    assert c.json("POST", "/api/installation/home", {"path": str(tmp_path / "other")})[1]["error"]["code"] == "active"
    st, r = c.json("PUT", "/api/schedule", {"mode": "weekly", "time": "07:15", "weekdays": ["mon", "thu"], "timezone": "local"})
    assert st == 200 and r["applied"] is True
    assert r["schedule"]["effective"] == {"mode": "weekly", "interval_minutes": 15, "time": "07:15", "weekdays": ["mon", "thu"], "timezone": "local"}
    assert r["schedule"]["in_sync"] is True and "<Thursday />" in held["xml"]
    assert 'mode = "weekly"' in (new_home / "config.toml").read_text(encoding="utf-8")

    # start
    st, r = c.json("POST", "/api/run", {"kind": "live", "repos": [REPO]})
    assert r == {"ok": True, "started": True}
    assert spawned[-1].argv[:3] == ["run-once", "--trigger", "manual"] and spawned[-1].argv[-2:] == ["--home", str(new_home.resolve())]
    assert ["--repo", REPO] == spawned[-1].argv[3:5]

    # diagnostics reuse doctor and carry no secret
    st, diag = c.json("GET", "/api/diagnostics")
    assert st == 200 and any(x["name"] == "github auth" and x["status"] == "PASS" for x in diag["checks"])

    # secrets never appear in an answer, an event or any file under either runtime folder
    for body in c.seen:
        assert TOKEN.encode() not in body and KEY.encode() not in body
    for folder in (tmp_path / "first", new_home):
        for f in folder.rglob("*"):
            if f.is_file():
                data = f.read_bytes()
                assert TOKEN.encode() not in data and KEY.encode() not in data, f


def test_two_rapid_runs_start_one_and_pause_blocks(start, tmp_path, vault, monkeypatch):
    enable_identity(monkeypatch)
    vault["github"] = TOKEN
    spawned = []
    monkeypatch.setattr(server.App, "spawn", lambda self, args: spawned.append(Recorded(args, running=True)) or spawned[-1])
    app, c = start(tmp_path / "home")
    assert c.json("POST", "/api/installation/activate", {"confirm": True})[0] == 200
    assert c.json("POST", "/api/pause", {}) == (200, {"ok": True})
    assert c.json("POST", "/api/run", {"kind": "live"})[1]["reason"] == "paused"
    assert c.json("GET", "/api/state")[1]["runtime"]["state"] == "paused"
    assert c.json("POST", "/api/resume", {}) == (200, {"ok": True})
    answers = []
    threads = [threading.Thread(target=lambda: answers.append(c.json("POST", "/api/run", {"kind": "live"})[1])) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(a["started"] for a in answers) == [False, True] and len(spawned) == 1
    assert [a["reason"] for a in answers if not a["started"]] == ["already_running"]
    assert c.json("GET", "/api/state")[1]["runtime"]["state"] == "running"
    assert c.json("POST", "/api/cancel")[1] == {"ok": True, "requested": True}


def test_missing_credential_and_restricted_export(start, tmp_path, vault, monkeypatch):
    app, c = start(tmp_path / "home")
    assert c.json("POST", "/api/run", {"kind": "dry_run"})[1]["reason"] == "credentials_missing"
    cfg = app.home / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("export_restricted = []", f'export_restricted = ["{REPO}"]'), encoding="utf-8")
    st, r = c.json("PUT", f"/api/repos/{REPO}", {"api_export": "allowlist", "source_include": ["src/*.py"], "confirm_export": True})
    assert st == 409 and r["error"]["code"] == "export_restricted"
    assert c.json("DELETE", f"/api/repos/{REPO}")[0] == 404


def test_empty_section_list_and_empty_model_are_refused(start, tmp_path):
    app, c = start(tmp_path / "home")
    st, r = c.json("PUT", "/api/repos/your-account/another-repository", {"managed_sections": []})
    assert st == 400 and r["error"]["code"] == "bad_request" and "at least one" in r["error"]["message"]
    assert "your-account/another-repository" not in (app.home / "config.toml").read_text(encoding="utf-8")  # nothing was saved
    assert c.json("PUT", "/api/repos/your-account/another-repository", {"publish_mode": "review_pr"}) == (200, {"ok": True})  # omitted: defaults
    for bad in ("", "   "):
        st, r = c.json("PUT", "/api/settings", {"model": bad})
        assert st == 400 and r["error"]["code"] == "bad_request"
    assert c.json("PUT", "/api/settings", {"model": "claude-sonnet-5-5"}) == (200, {"ok": True})


def test_concurrent_config_saves_keep_every_accepted_change(start, tmp_path):
    app, c = start(tmp_path / "home")
    names = [f"your-account/repository-{i}" for i in range(6)]
    answers = []
    threads = [threading.Thread(target=lambda n=n: answers.append(c.json("PUT", f"/api/repos/{n}", {"publish_mode": "preview"})))
               for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert answers == [(200, {"ok": True})] * len(names)
    text = (app.home / "config.toml").read_text(encoding="utf-8")
    assert all(f'name = "{n}"' in text for n in names) and not list(app.home.glob("*.tmp"))


def test_onboarding_counts_discovery_and_only_a_dry_run_that_previewed(start, tmp_path, vault):
    from readme_monitor.db import DB

    app, c = start(tmp_path / "home")
    assert c.json("PUT", "/api/settings", {"discovery": {"owner": "your-account"}})[0] == 200
    assert c.json("PUT", "/api/schedule", {"mode": "daily", "time": "08:30", "timezone": "local"})[0] == 200

    def steps():
        return {s["id"]: s["done"] for s in c.json("GET", "/api/state")[1]["onboarding"]["steps"]}

    assert steps()["repositories"] and steps()["publication"]  # account-wide discovery without [[repo]] entries
    db = DB(app.home / "state.db")
    for outcome, summary in (("ok", {"previewed": 0, "error": 1, "dry_run": True}), ("crashed", {"previewed": 1}),
                             ("cancelled", {"previewed": 1, "dry_run": True})):
        db.finish_run(db.start_run("dry_run", "person"), outcome, summary)
    db.start_run("dry_run", "person")  # unfinished
    assert steps()["dry_run"] is False
    db.finish_run(db.start_run("dry_run", "person"), "ok", {"previewed": 1, "dry_run": True})
    db.close()
    assert steps()["dry_run"] is True


def test_second_launch_reuses_the_running_server(start, tmp_path, monkeypatch, capsys):
    app, c = start(tmp_path / "home")
    runtime._write(app.home / server.SERVER_FILE, {"port": app.port, "pid": os.getpid()})
    assert server.running_port(app.home) == app.port
    assert server.launch(app.home, browser=False) == 0
    url = re.search(r"http://127\.0\.0\.1:\d+/auth\?code=\S+", capsys.readouterr().out)[0]
    assert f":{app.port}/" in url and Client(app.port).req("GET", url.split(str(app.port), 1)[1])[0] == 302
    runtime._write(app.home / server.SERVER_FILE, {"port": app.port, "pid": 999_999_999})
    assert server.running_port(app.home) is None


def test_task_view_does_not_depend_on_the_display_language():
    from dataclasses import asdict

    weekly = asdict(scheduler.Schedule(mode="weekly", time="07:15", weekdays=("mon", "thu")))
    english = {"Status": "Ready", "Next Run Time": "3/9/2026 7:15:00 AM", "Last Run Time": "3/5/2026 7:15:00 AM", "Last Result": "0"}
    view = server._task_view(english, weekly)
    assert view["next_run"].startswith("2026-03-09") and view["last_run"].startswith("2026-03-05")
    german = {"Status": "Bereit", "Nächste Laufzeit": "09.03.2026 07:15:00"}  # labels and times in another language
    planned = scheduler.next_checks(scheduler.Schedule(mode="weekly", time="07:15", weekdays=("mon", "thu")), server.datetime.now(server.timezone.utc))
    view = server._task_view(german, weekly)
    assert view["installed"] and view["next_run"] == planned[0].isoformat(timespec="seconds") and view["last_run"] is None
    assert server._task_view({}, weekly)["next_run"] == planned[0].isoformat(timespec="seconds")  # list unreadable, XML known
    assert server._task_view(german, asdict(scheduler.Schedule(interval_minutes=30)))["next_run"] is None  # never a guess
    assert server._task_view({"Status": "Ready", "Next Run Time": "N/A"}, weekly)["next_run"] is None
    assert server._task_view(None, None) is None


def test_weekly_schedule_in_utc_is_a_plain_400(start, tmp_path):
    app, c = start(tmp_path / "home")
    st, r = c.json("PUT", "/api/schedule", {"mode": "weekly", "time": "02:00", "weekdays": ["wed"], "timezone": "utc"})
    assert st == 400 and r["error"]["code"] == "invalid_config" and "this computer's time" in r["error"]["message"]
    assert 'mode = "weekly"' not in (app.home / "config.toml").read_text(encoding="utf-8")
