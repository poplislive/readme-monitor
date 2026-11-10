"""Acceptance criteria 1-3 and 5-8 (4 is in test_acceptance_git_remote.py and test_acceptance_demo.py,
9-11 in test_acceptance_interface.py). Each test is named after its criterion and its docstring says
what is simulated. Real here: the HTTP server, Worker, Generator, Publisher, database, configuration
writer, activation records and the scheduled-task XML with its read-back parser. Simulated: the
computer's identity (name, Windows, MachineGuid), the operating system's credential store, GitHub
and Claude (readme_monitor.simulation), and Task Scheduler (an in-memory stand-in that keeps the XML).
A real Windows scheduled run, real GitHub and a real provider are verified separately (docs/verification.md).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import anthropic
import httpx2
import pytest

from readme_monitor import cli, credentials, db as dbm, runtime, scheduler, server
from readme_monitor.config import Schedule, load
from readme_monitor.db import DB, REPO_COLUMNS_V2, SCHEMA
from readme_monitor.llm import Generator
from readme_monitor.publish import Publisher
from readme_monitor.scheduler import NS
from readme_monitor.simulation import FakeAccount, FakeClaude, FakeGitHub, blob_sha, build_demo

from .conftest import CONFIG, REPO as DEMO_REPO, initial_readme, run_cycle
from .test_engine_schedule import utc, zone
from .test_server_api import (Account, InProcess, KEY, REPO, TOKEN, Client, enable_identity, fake_task_scheduler,  # noqa: F401
                              start, vault, wait)
from .test_server_demo import read_stream

NEUTRAL = "DESK-PC-42"
windows_only = pytest.mark.skipif(os.name != "nt", reason="the task state is read through schtasks.exe, which exists only on Windows")


def computer(monkeypatch, name=NEUTRAL, guid="guid-a"):
    """Simulated machine identity: any computer name, Windows, a made-up MachineGuid."""
    monkeypatch.setattr(runtime, "hostname", lambda: name)
    monkeypatch.setattr(runtime, "machine_guid", lambda: guid)
    monkeypatch.setattr(runtime, "WINDOWS", True)
    monkeypatch.setattr(scheduler, "current_user", lambda: f"{name}\\sam")


# ---- 1 ---------------------------------------------------------------------------------------


@pytest.mark.real_host
def test_criterion_01_fresh_user_on_a_neutral_computer_completes_setup_through_the_http_api(start, tmp_path, vault, monkeypatch):
    """Simulated: computer DESK-PC-42, credential store, GitHub account, Task Scheduler. The dry run is the real CLI
    command run in a thread. The user's own budget, schedule and repository settings end up in the state the API returns."""
    computer(monkeypatch)
    account = Account()
    monkeypatch.setattr(server, "GitHub", lambda token, **kw: account)
    monkeypatch.setattr(cli, "GitHub", lambda token, **kw: account)
    spawned = []
    monkeypatch.setattr(server.App, "spawn", lambda self, args: spawned.append(InProcess([*args, "--home", str(self.home)])) or spawned[-1])
    held = fake_task_scheduler(monkeypatch)
    app, c = start(tmp_path / "first")
    state = c.json("GET", "/api/state")[1]
    assert state["installation"]["host"] == NEUTRAL and state["installation"]["active"] is False and state["onboarding"]["complete"] is False

    mine = tmp_path / "my-runtime"
    assert c.json("POST", "/api/installation/home", {"path": str(mine)})[0] == 200
    assert c.json("POST", "/api/credentials/github", {"value": TOKEN})[1]["login"] == "your-account"
    assert c.json("POST", "/api/credentials/anthropic", {"value": KEY})[0] == 200
    found = c.json("POST", "/api/github/discover")[1]
    assert [x["name"] for x in found["repositories"]] == [REPO]
    mode = {"publish_mode": "review_pr", "managed_sections": ["project-structure"], "api_export": "none"}
    assert c.json("PUT", f"/api/repos/{REPO}", mode) == (200, {"ok": True})
    assert c.json("PUT", "/api/settings", {"budget": {"per_run_usd": 0.37, "daily_usd": 1.1, "monthly_usd": 4.5, "pacing": "none"}})[0] == 200
    assert c.json("PUT", "/api/schedule", {"mode": "weekly", "time": "07:45", "weekdays": ["tue", "fri"], "timezone": "local"})[1]["applied"] is False
    assert c.json("POST", "/api/run", {"kind": "dry_run"})[1] == {"ok": True, "started": True}
    spawned[0].join(60)
    st, r = c.json("POST", "/api/installation/activate", {"confirm": True})
    assert st == 200 and r["installation"]["active"] is True
    applied = c.json("PUT", "/api/schedule", {"mode": "weekly", "time": "07:45", "weekdays": ["tue", "fri"], "timezone": "local"})[1]
    assert applied["applied"] is True and applied["schedule"]["in_sync"] is True and "<Friday />" in held["xml"]

    state = c.json("GET", "/api/state")[1]
    assert state["installation"]["active"] is True and state["installation"]["activation"]["host"] == NEUTRAL
    assert state["budget"]["per_run_usd"] == 0.37 and state["budget"]["daily_usd"] == 1.1 and state["budget"]["monthly_usd"] == 4.5
    assert state["schedule"]["summary"] == "Tue and Fri at 07:45 (this computer's time)"
    assert state["onboarding"]["complete"] is True, state["onboarding"]
    assert [r["name"] for r in state["repos"]] == [REPO] and state["repos"][0]["mode"] == "review_pr"
    assert json.loads((mine / "activation.json").read_text(encoding="utf-8"))["host"] == NEUTRAL


# ---- 2 ---------------------------------------------------------------------------------------


@pytest.mark.real_host
def test_criterion_02_an_unactivated_installation_cannot_spend_publish_or_schedule(start, tmp_path, vault, monkeypatch, offline_host):
    """Simulated: GitHub, Claude, Task Scheduler (calls are recorded), machine identity. The real guard is in force
    (marker real_host): the folder was never activated. Nothing may reach the model, GitHub, the ledger or the scheduler."""
    computer(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    cfg_path = home / "config.toml"
    cfg_path.write_text(CONFIG.replace("{mode}", "direct"), encoding="utf-8")
    cfg, db, gh, claude = load(cfg_path), DB(home / "state.db"), FakeGitHub({"README.md": initial_readme(), "src/demo/app.py": "A = 1\n",
                                                                           "pyproject.toml": '[project]\nname = "demo"\n'}), FakeClaude()
    with pytest.raises(runtime.NotActivated):  # a live cycle: the generator refuses before reserving or sending anything
        run_cycle(cfg, db, gh, claude, home)
    with pytest.raises(runtime.NotActivated):  # the publisher refuses by itself, even when handed a finished proposal
        Publisher(cfg, db, gh).publish(cfg.repos[0], gh.branches["main"], "main", {"README.md": ("# x\n", blob_sha(gh.files()["README.md"]))},
                                       1, {}, {}, [])
    reads = []
    monkeypatch.setattr(credentials, "get", lambda kind: reads.append(kind) or ("token", "test"))
    for argv in (["run-once"], ["install-task"], ["run-task-now"], ["preview", "--repo", DEMO_REPO, "--api"]):
        assert cli.main([*argv, "--home", str(home)]) == 3, argv
    assert reads == []  # refused before any credential was read
    spawned = []
    monkeypatch.setattr(server.App, "spawn", lambda self, args: spawned.append(args))
    app, c = start(home)
    assert c.json("POST", "/api/run", {"kind": "live"})[1] == {"ok": True, "started": False, "reason": "inactive"}
    assert c.json("POST", f"/api/repos/{DEMO_REPO}/adopt", {"confirm": True})[1]["reason"] == "inactive"
    r = c.json("PUT", "/api/schedule", {"mode": "daily", "time": "06:00"})[1]
    assert r["applied"] is False and r["reason"] == "inactive"
    assert c.json("GET", "/api/state")[1]["runtime"]["state"] == "inactive" and spawned == []
    assert claude.calls == [] and gh.writes == [] and gh.prs == {}
    assert db.conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0
    assert not [a for a in offline_host if a[0] in ("/Create", "/Run", "/Change")]  # nothing was scheduled or started


# ---- 3 ---------------------------------------------------------------------------------------


def legacy_home(path):
    """A 0.2.0-style runtime folder: schema-2 database with history, a pending reservation and a ledger, plus production-host.json."""
    path.mkdir()
    (path / "config.toml").write_text(f'[[repo]]\nname = "{REPO}"\n', encoding="utf-8")
    (path / runtime.LEGACY_FILE).write_text(json.dumps({"host": NEUTRAL, "machine_guid": "guid-a", "home": str(path.resolve()), "account": "sam",
                                                        "bound_at": "2026-05-01T08:00:00+00:00"}), encoding="utf-8")
    conn = sqlite3.connect(path / "state.db")
    conn.executescript(SCHEMA)
    for col, decl in REPO_COLUMNS_V2:
        if col != "analyzed_head":
            conn.execute(f"ALTER TABLE repos ADD COLUMN {col} {decl}")
    conn.execute("INSERT INTO repos(name, state, analyzed_fp, quick_key) VALUES(?, 'awaiting_merge', 'fp', 'qk')", (REPO,))
    conn.execute("INSERT INTO proposals(repo, fingerprint, base_sha, mode, state, files, created_at, updated_at) "
                 "VALUES(?, 'fp', 'abc', 'review_pr', 'awaiting_merge', '{}', 't', 't')", (REPO,))
    for state, reserved, actual in (("reconciled", 0.06, 0.0425), ("reserved", 0.07, None), ("ambiguous", 0.05, None)):
        conn.execute("INSERT INTO usage(repo, run_id, model, state, reserved_usd, actual_usd, created_at, updated_at) "
                     "VALUES(?, 1, 'm', ?, ?, ?, '2026-11-10T01:00:00+00:00', 'x')", (REPO, state, reserved, actual))
    conn.execute("INSERT INTO runs(started_at, trigger, outcome) VALUES('2026-11-10T01:00:00+00:00', 'scheduled', 'ok')")
    conn.execute("INSERT INTO settings(key, value) VALUES('paused', '1')")
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()
    return path


def status(home, capsys):
    assert cli.main(["status", "--json", "--home", str(home)]) == 0
    return json.loads(capsys.readouterr().out)["installation"]


@pytest.mark.real_host
def test_criterion_03_legacy_migration_keeps_activation_history_reservations_and_ledger_but_not_on_another_machine(
        start, tmp_path, vault, monkeypatch, capsys):
    """Simulated: the old and the new computer (MachineGuid values guid-a and guid-b). Real: the migration code, the
    activation records, the database upgrade with its automatic backup, the HTTP API reading the result."""
    computer(monkeypatch)
    old, other_pc, copied = legacy_home(tmp_path / "old"), legacy_home(tmp_path / "other-pc"), None
    copied = shutil.copytree(old, tmp_path / "copied-before-upgrade")

    inst = status(old, capsys)  # same computer, same folder: the activation is carried forward
    assert inst["active"] and inst["activation"]["migrated_from"] == runtime.LEGACY_FILE and inst["problem"] is None
    db = DB(old / "state.db")
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 3 and list((old / "backups").glob("pre-schema3-*.db"))
    assert [r["state"] for r in db.conn.execute("SELECT state FROM usage ORDER BY id")] == ["reconciled", "reserved", "ambiguous"]
    assert db.spent_since("0") == pytest.approx(0.0425 + 0.07 + 0.05)  # the reserved amount still counts until it is settled
    assert len(db.open_reservations()) == 2 and db.latest_proposal(REPO)["state"] == "awaiting_merge" and db.globally_paused
    assert db.last_runs(1)[0]["trigger"] == "scheduled"
    db.close()

    app, c = start(old)  # the interface shows the same numbers
    state = c.json("GET", "/api/state")[1]
    assert state["installation"]["active"] and state["budget"]["open_reservations"] == 2 and state["budget"]["open_reserved_usd"] == pytest.approx(0.12)
    assert sorted(r["state"] for r in c.json("GET", "/api/usage")[1]["rows"]) == ["ambiguous", "reconciled", "reserved"]

    # a copy made before the upgrade (different folder) and a record from another computer stay inactive
    inst = status(copied, capsys)
    assert inst["active"] is False and "earlier version" in inst["problem"]
    monkeypatch.setattr(runtime, "machine_guid", lambda: "guid-b")
    inst = status(other_pc, capsys)
    assert inst["active"] is False and "earlier version" in inst["problem"] and not (other_pc / runtime.ACTIVATION_FILE).exists()
    inst = status(old, capsys)  # the already-upgraded folder moved to the other computer
    assert inst["active"] is False and "another computer" in inst["problem"]
    with pytest.raises(runtime.NotActivated):
        runtime.require_active(old)
    ledger = DB(old / "state.db")  # inactive, but the recorded spending is untouched
    assert ledger.spent_since("0") == pytest.approx(0.0425 + 0.07 + 0.05)
    monkeypatch.setattr(runtime, "machine_guid", lambda: "guid-a")
    clone = shutil.copytree(old, tmp_path / "clone-of-upgraded")  # an upgraded folder copied on the same computer
    assert "moved or copied" in status(clone, capsys)["problem"]


# ---- 4 ---------------------------------------------------------------------------------------


def test_criterion_04_deterministic_run_goes_from_inspection_to_publication_through_the_real_components(tmp_path):
    """Simulated: the shipped in-memory GitHub and scripted model (readme_monitor.simulation). Real: Worker, Generator,
    validation, Publisher, ledger, database. The same world is published by pull request and by direct commit."""
    demo = build_demo(tmp_path / "demo")
    demo.run_cycle()
    trail = lambda name: [(e["stage"], e["status"]) for e in demo.db.events(limit=500) if e["repo"] == f"octo-demo/{name}"]  # noqa: E731
    assert trail("atlas-api") == [("inspect", "done"), ("evidence", "done"), ("draft", "started"), ("draft", "done"), ("validate", "done"),
                                  ("publish", "started"), ("publish", "done"), ("await", "info"), ("result", "done")]
    atlas = demo.repo("atlas-api")
    assert len(atlas.prs) == 1 and "Generated features" in atlas.files("readme-monitor/update")["README.md"]
    assert demo.db.conn.execute("SELECT state FROM usage WHERE repo='octo-demo/atlas-api'").fetchone()[0] == "reconciled"
    demo.advance()
    demo.run_cycle(trigger="scheduled")
    notes = demo.repo("field-notes")  # direct mode: a commit on the target branch, read back, no pull request
    published = [e for e in demo.db.events(limit=500) if e["repo"] == "octo-demo/field-notes" and e["stage"] == "publish" and e["status"] == "done"][-1]
    assert notes.prs == {} and published["data"]["commit_sha"] == notes.branches["main"] and published["data"]["mode"] == "direct"
    assert demo.db.latest_proposal("octo-demo/field-notes")["state"] == "published"


# ---- 5 ---------------------------------------------------------------------------------------


def repo_view(c):
    state = c.json("GET", "/api/state")[1]
    events = c.json("GET", f"/api/events?repo={DEMO_REPO}&limit=500")[1]["events"]
    return next(r for r in state["repos"] if r["name"] == DEMO_REPO), events


def test_criterion_05_no_change_runs_make_no_model_call_and_no_git_write(env, start, vault):
    """Simulated: GitHub and Claude. The second and third cycles must be invisible apart from the run record."""
    e = env()
    e.run()
    calls, writes = len(e.claude.calls), list(e.gh.writes)
    e.run()
    e.run()
    assert len(e.claude.calls) == calls and e.gh.writes == writes
    app, c = start(e.home)
    runs = c.json("GET", "/api/runs")[1]["runs"]
    assert len(runs) == 3 and [r["outcome"] for r in runs] == ["ok"] * 3
    for run in runs[:2]:
        events = c.json("GET", f"/api/runs/{run['id']}")[1]["events"]
        assert [(ev["stage"], ev["status"]) for ev in events] == [("run", "done")]  # nothing per repository
    repo, _ = repo_view(c)
    assert repo["state"] == "awaiting_merge" and repo["proposal"]["state"] == "awaiting_merge"


@pytest.mark.parametrize("case", ["malformed", "person_edit", "moved_branch", "no_permission", "provider_down", "no_budget"])
def test_criterion_05_each_failure_shows_the_right_event_and_state_in_the_api(env, start, vault, monkeypatch, case):
    """Simulated: GitHub, Claude (including a provider outage and a malformed answer). The API, not the database, is read."""
    responder = None
    if case == "person_edit":
        responder = lambda p: {"sections": [{"id": s, "content": f"Generated {s}.", "evidence": [], "uncertain": False,  # noqa: E731
                                             "review_notes": ""} for s in p["requested_sections"]]}
    if case == "malformed":
        responder = lambda p: "not json"  # noqa: E731
    if case == "provider_down":
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        responder = lambda p: anthropic.InternalServerError("down", response=httpx2.Response(500, request=req), body=None)  # noqa: E731
    e = env(responder=responder)
    if case == "person_edit":
        e.run()
        e.gh.merge(1)
        e.run()
        e.gh.push_commit({"README.md": e.gh.files()["README.md"].replace("Generated status", "A person rewrote status")})
    if case == "moved_branch":
        real, n = e.gh.head_sha, {"i": 0}

        def moving(full, branch):
            n["i"] += 1
            if n["i"] == 2:
                e.gh.push_commit({"src/demo/app.py": "RACE = 1\n"})
            return real(full, branch)

        monkeypatch.setattr(e.gh, "head_sha", moving)
    if case == "no_permission":
        e.gh.push = False
    if case == "no_budget":
        uid = e.db.reserve("owner/other", None, "claude-sonnet-5-5", 2.0)
        e.db.reconcile(uid, 2.0, {}, None)
    e.run()
    app, c = start(e.home)
    repo, events = repo_view(c)
    result = [ev for ev in events if ev["stage"] == "result"][-1]
    expected = {"malformed": ("failed", "error"), "person_edit": ("conflict", "conflict"), "moved_branch": ("deferred", "stale"),
                "no_permission": ("blocked", "permission_missing"), "provider_down": ("failed", "error"),
                "no_budget": ("deferred", "budget_paused")}[case]
    assert (result["data"]["outcome"], repo["state"]) == expected
    assert result["message"].startswith(DEMO_REPO) and repo["state_detail"]
    if case == "malformed":
        assert any(ev["stage"] == "validate" and ev["status"] == "failed" for ev in events)
    if case == "provider_down":
        assert any(ev["stage"] == "draft" and ev["status"] == "failed" for ev in events)
    if case == "no_budget":
        assert "daily limit" in result["message"] and e.claude.calls == []
    if case == "person_edit":
        assert "A person rewrote status" in e.gh.files()["README.md"] and "edited by a person" in repo["state_detail"]
    if case in ("no_permission", "moved_branch", "no_budget"):
        assert e.gh.writes == [] and e.gh.prs == {}


# ---- 6 ---------------------------------------------------------------------------------------


def test_criterion_06_overlapping_triggers_over_several_repositories_never_duplicate_work(tmp_path, monkeypatch, capsys):
    """Simulated: GitHub (three repositories behind one account) and a slow Claude, so the second trigger really arrives
    while the first cycle is in its model request. Real: the CLI commands, the worker lock, the ledger."""
    acct = FakeAccount("your-account")
    names = [f"your-account/repo-{i}" for i in range(3)]
    for n in names:
        acct.add(n, FakeGitHub({"README.md": initial_readme().replace("Demo", n), "src/x.py": "X = 1\n"}))
    (tmp_path / "config.toml").write_text(
        "[monitor]\nquiet_minutes = 0\nmin_generation_interval_hours = 0\n[budget]\nper_run_usd = 1.0\ndaily_usd = 2.0\nmonthly_usd = 10.0\n"
        + "".join(f'[[repo]]\nname = "{n}"\nsource_include = ["src/*.py"]\nmanaged_sections = ["features", "project-structure"]\n'
                  'publish_mode = "review_pr"\napi_export = "allowlist"\n' for n in names), encoding="utf-8")
    claude = FakeClaude(latency=0.4)
    monkeypatch.setattr(cli.credentials, "get", lambda kind: ("token", "test"))
    monkeypatch.setattr(cli, "GitHub", lambda token, db=None: acct)
    monkeypatch.setattr(cli.Generator, "client", property(lambda self: claude))
    codes = {}

    def trigger(kind):
        codes[kind] = cli.main(["run-once", "--trigger", kind, "--home", str(tmp_path)])

    first = threading.Thread(target=trigger, args=("manual",))
    first.start()
    wait(lambda: claude.calls)  # the first cycle holds the worker lock and is waiting for the model
    second = threading.Thread(target=trigger, args=("scheduled",))
    second.start()
    second.join(30)
    first.join(60)
    assert codes == {"manual": 0, "scheduled": 0} and capsys.readouterr().out.count("Skipped") == 1
    for kind in ("scheduled", "manual", "scheduled"):  # and later triggers find nothing left to do
        assert cli.main(["run-once", "--trigger", kind, "--home", str(tmp_path)]) == 0
    db = DB(tmp_path / "state.db")
    assert len(claude.calls) == 3 and db.conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 3
    assert db.spent_since("0") == pytest.approx(3 * 0.004) and db.open_reservations() == []
    for fake in acct.repos.values():
        assert len(fake.prs) == 1 and [w[0] for w in fake.writes].count("put_file") == 1 and fake.writes.count(("create_pr", 1)) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM runs WHERE outcome='ok'").fetchone()[0] >= 4


# ---- 7 ---------------------------------------------------------------------------------------


@windows_only
@pytest.mark.real_host
def test_criterion_07_schedule_edits_change_the_backend_pause_survives_a_restart(start, tmp_path, vault, monkeypatch):
    """Simulated: computer identity and Task Scheduler (keeps and returns the XML; every call is recorded). Real: the
    server, config writer, XML generator and read-back parser. A second server on the same folder stands for a restart."""
    computer(monkeypatch)
    held = fake_task_scheduler(monkeypatch)
    inner, calls = scheduler._schtasks, []
    monkeypatch.setattr(scheduler, "_schtasks", lambda *a: calls.append(a[0]) or inner(*a))
    app, c = start(tmp_path / "home")
    assert c.json("POST", "/api/installation/activate", {"confirm": True})[0] == 200
    base = {"interval_minutes": 15, "time": "09:00", "weekdays": ["mon", "tue", "wed", "thu", "fri"], "timezone": "local"}  # the defaults
    for body, trigger, summary in [
        ({"mode": "daily", "time": "08:30", "timezone": "local"}, "CalendarTrigger", "Every day at 08:30 (this computer's time)"),
        ({"mode": "weekly", "time": "06:00", "weekdays": ["sat", "sun"], "timezone": "local"}, "ScheduleByWeek", "Weekends at 06:00 (this computer's time)"),
        ({"mode": "interval", "interval_minutes": 60, "timezone": "local"}, "Repetition", "Every hour"),
        ({"mode": "manual"}, "Triggers", "Manual only: checks run when you start them"),
    ]:
        body = {**base, **body}
        st, r = c.json("PUT", "/api/schedule", body)
        assert st == 200 and r["applied"] is True and r["schedule"]["in_sync"] and trigger in held["xml"], r
        assert r["schedule"]["summary"] == summary == scheduler.summary(scheduler.parse_task_xml(held["xml"]))
        assert load(app.home / "config.toml").schedule.mode == body["mode"]
    assert "/Run" not in calls and "/End" not in calls  # re-registering a task neither starts nor stops a check
    c.json("PUT", "/api/schedule", {"mode": "daily", "time": "08:30"})
    assert c.json("POST", "/api/pause", {}) == (200, {"ok": True})
    assert c.json("POST", "/api/run", {"kind": "live"})[1]["reason"] == "paused"

    app2, c2 = start(app.home)  # restart: nothing lives in the server's memory
    state = c2.json("GET", "/api/state")[1]
    assert state["runtime"]["state"] == "paused" and state["installation"]["active"] is True
    assert state["schedule"]["configured"]["time"] == "08:30" and state["schedule"]["in_sync"] is True
    assert c2.json("POST", "/api/resume", {}) == (200, {"ok": True})
    assert c2.json("GET", "/api/state")[1]["runtime"]["state"] == "scheduled_idle"
    assert c2.json("POST", "/api/installation/deactivate", {"confirm": True})[0] == 200
    assert "xml" not in held and c2.json("GET", "/api/state")[1]["schedule"]["effective"] is None  # deactivating removed the task


@pytest.mark.real_host
def test_criterion_07_switching_mode_keeps_the_schedule_in_sync(start, tmp_path, vault, monkeypatch):
    """Simulated: computer identity and Task Scheduler. Found while writing criterion 7."""
    computer(monkeypatch)
    fake_task_scheduler(monkeypatch)
    app, c = start(tmp_path / "home")
    assert c.json("POST", "/api/installation/activate", {"confirm": True})[0] == 200
    assert c.json("PUT", "/api/schedule", {"mode": "weekly", "time": "06:00", "weekdays": ["sat"]})[1]["schedule"]["in_sync"]
    assert c.json("PUT", "/api/schedule", {"mode": "interval", "interval_minutes": 60})[1]["schedule"]["in_sync"]


def test_criterion_07_daylight_saving_and_calendar_edges_keep_the_wall_clock():
    """Simulated: the computer's time zone (a fixed 2026 New York rule set when no zone database is installed). The real
    next-check calculator and task XML. Windows applies the wall-clock StartBoundary itself; that is not simulated."""
    ny = zone("America/New_York")
    localize = lambda naive: naive.replace(tzinfo=ny)  # noqa: E731
    daily = Schedule(mode="daily", time="09:00")
    spring = scheduler.next_checks(daily, utc(2026, 3, 7, 15), localize)
    fall = scheduler.next_checks(daily, utc(2026, 10, 31, 14), localize)
    assert [t.hour for t in spring] == [13, 13, 13] and spring[0].day == 8  # 09:00 local is 13:00 UTC from the change on
    assert [t.hour for t in fall] == [14, 14, 14]  # and 14:00 UTC after the clocks go back
    assert (spring[0] - utc(2026, 3, 7, 14)).total_seconds() == 23 * 3600  # the day the clocks change is 23 hours long
    leap = scheduler.next_checks(Schedule(mode="daily", time="09:00", timezone="utc"), utc(2028, 2, 28, 10))
    assert [(t.month, t.day) for t in leap] == [(2, 29), (3, 1), (3, 2)]
    # weekly schedules are local only (Windows counts weekdays in local time)
    year_end = scheduler.next_checks(Schedule(mode="weekly", time="06:00", weekdays=("sat", "sun")), utc(2027, 12, 30, 12), localize)
    assert [(t.year, t.month, t.day, t.hour) for t in year_end] == [(2028, 1, 1, 11), (2028, 1, 2, 11), (2028, 1, 8, 11)]
    local = scheduler.task_xml(*xml_args(Schedule(mode="daily", time="09:00")))
    assert "T09:00:00</StartBoundary>" in local and "Z</StartBoundary>" not in local  # a wall-clock time, so Windows follows the clock change


def xml_args(schedule):
    from pathlib import Path

    return Path("python.exe"), Path("src"), Path("rt"), Path("rt/config.toml"), schedule, 15, f"{NEUTRAL}\\sam"


def test_criterion_07_missed_runs_make_one_catch_up_and_a_start_inside_a_running_check_is_ignored(env):
    """Simulated: GitHub and Claude; the time the computer was off is simply cycles that did not happen. The task
    policies are asserted on the real XML; Windows enforcing them is verified separately on a Windows host."""
    root = ET.fromstring(scheduler.task_xml(*xml_args(Schedule(mode="interval", interval_minutes=15))).replace('encoding="UTF-16"', ""))
    assert root.find(".//t:StartWhenAvailable", NS).text == "true"  # one catch-up start after sleep or shutdown, never a burst
    assert root.find(".//t:MultipleInstancesPolicy", NS).text == "IgnoreNew"  # a start inside a running check is skipped
    e = env()
    e.run()
    for i in range(6):  # six commits while nothing was running
        e.gh.push_commit({"src/demo/app.py": f"V = {i}\n"}, f"change {i}")
    e.run()  # the catch-up
    assert len(e.claude.calls) == 2 and [w[0] for w in e.gh.writes].count("create_pr") == 1 and len(e.gh.prs) == 1
    e.run()
    assert len(e.claude.calls) == 2


def test_criterion_07_an_interrupted_migration_recovers_on_the_next_start(tmp_path, monkeypatch):
    """Simulated: a power cut (an exception) at each database migration step and during the activation-record write."""
    steps = list(dbm.MIGRATION_STEPS)
    for stop in range(len(steps)):
        legacy = legacy_home(tmp_path / f"interrupted-at-step-{stop}")
        boom = {"armed": True}

        def failing(conn, real=steps[stop]):
            if boom["armed"]:
                raise OSError("power cut")
            return real(conn)

        monkeypatch.setattr(dbm, "MIGRATION_STEPS", [failing if i == stop else s for i, s in enumerate(steps)])
        with pytest.raises(OSError):
            DB(legacy / "state.db")
        boom["armed"] = False
        db = DB(legacy / "state.db")  # the next start finishes the job
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.spent_since("0") == pytest.approx(0.0425 + 0.07 + 0.05) and db.latest_proposal(REPO)["state"] == "awaiting_merge"
        db.close()
    monkeypatch.setattr(dbm, "MIGRATION_STEPS", steps)
    legacy = legacy_home(tmp_path / "interrupted-activation")
    computer(monkeypatch)
    monkeypatch.setattr(runtime, "pointer_path", lambda: tmp_path / "pointer.json")
    (legacy / "activation.json.tmp").write_text("{half", encoding="utf-8")
    real, calls = runtime.os.replace, {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("power cut")
        return real(a, b)

    monkeypatch.setattr(runtime.os, "replace", flaky)
    with pytest.raises(OSError):
        runtime.migrate_legacy(legacy)
    assert runtime.activation_problem(legacy)  # still inactive, never half-active
    assert runtime.migrate_legacy(legacy) == "migrated" and runtime.activation_problem(legacy) is None


# ---- 8 ---------------------------------------------------------------------------------------


def test_criterion_08_closing_and_reopening_the_interface_changes_no_schedule_and_replays_no_side_effect(env, start, vault, offline_host, monkeypatch):
    """Simulated: GitHub, Claude, Task Scheduler (calls recorded). Real: two server lifetimes on one runtime folder,
    new sessions, event-stream reconnects with Last-Event-ID. The page itself is only a client of these requests."""
    e = env()
    e.run()
    e.run()
    spawned = []
    monkeypatch.setattr(server.App, "spawn", lambda self, args: spawned.append(args))
    config_before = (e.home / "config.toml").read_bytes()

    def observe():
        return {"model_calls": len(e.claude.calls), "git_writes": list(e.gh.writes), "runs": e.db.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                "usage": e.db.conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0], "scheduler": [a for a in offline_host if a[0] != "/Query"], "spawned": list(spawned),
                "config": (e.home / "config.toml").read_bytes()}

    before = observe()
    seen = []
    for _ in range(2):  # open, look around, close; then open again with a new session
        app, c = start(e.home)
        state = c.json("GET", "/api/state")[1]
        assert c.req("GET", "/")[0] == 200 and c.json("GET", "/api/events?limit=500")[0] == 200
        cursor = state["cursor"]
        got, _ = read_stream(app.port, c.cookie, cursor - 2, until=lambda ids: cursor in ids)
        assert [i for i, _ in got] == [cursor - 1, cursor]  # resumes exactly after the last event it had, no duplicates
        again, beats = read_stream(app.port, c.cookie, cursor, timeout=2)
        assert again == []  # caught up: heartbeats only, no event is replayed
        seen.append((state["schedule"]["configured"], state["cursor"], state["runtime"]["state"]))
        app.stop.set()
    assert observe() == before and seen[0] == seen[1] and before["config"] == config_before
