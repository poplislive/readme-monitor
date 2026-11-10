"""Simulation mode (`ui --demo`) over HTTP: the real pipeline against the fake transports, the
event stream, run serialization, schedule read-back and inert repository text."""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest
import requests

from readme_monitor import credentials, scheduler
from readme_monitor.db import DB
from .test_server_api import Client, assert_shapes, start, wait  # noqa: F401 (fixture)

STAGES = ["inspect", "evidence", "draft", "validate", "publish", "result"]


@pytest.fixture
def offline(monkeypatch):
    """Simulation must never reach keyring, Task Scheduler or the network."""
    def refuse(*a, **k):
        raise AssertionError("the simulation touched a real service")

    for name in ("get", "store", "remove"):
        monkeypatch.setattr(credentials, name, refuse)
    monkeypatch.setattr(scheduler, "_schtasks", refuse)
    monkeypatch.setattr(requests.Session, "request", refuse)


def run(app, c, kind="live", **body):
    r = c.json("POST", "/api/run", {"kind": kind, **body})[1]
    assert r == {"ok": True, "started": True}, r
    wait(lambda: not app.thread.is_alive(), 60)


def test_demo_read_shapes_and_live_cycle_stages(start, tmp_path, offline):
    app, c = start(tmp_path / "demo", demo=True)
    state = assert_shapes(c)
    assert state["simulation"] is True and c.json("GET", "/api/session")[1]["simulation"] is True
    assert len(state["repos"]) == 5 and all(r["name"].startswith("octo-demo/") for r in state["repos"])
    assert state["installation"]["active"] is True and state["runtime"]["state"] == "scheduled_idle"
    assert state["credentials"]["github"]["present"] is True
    assert state["schedule"]["effective"] == state["schedule"]["configured"] and state["schedule"]["in_sync"]
    cursor = state["cursor"]
    run(app, c)
    events = c.json("GET", f"/api/events?after={cursor}&limit=500")[1]["events"]
    ids = [e["id"] for e in events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    atlas = [e["stage"] for e in events if e["repo"] == "octo-demo/atlas-api"]
    it = iter(atlas)
    assert all(stage in it for stage in STAGES), atlas  # in this order
    first = next(e for e in events if e["repo"] == "octo-demo/atlas-api" and e["stage"] == "inspect")
    assert first["data"]["first_scan"] is True and first["trigger"] == "manual"
    result = [e for e in events if e["repo"] == "octo-demo/atlas-api" and e["stage"] == "result"][-1]
    assert result["data"]["outcome"] == "awaiting_merge"
    st, detail = c.json("GET", f"/api/runs/{first['run_id']}")
    assert st == 200 and detail["run"]["outcome"] == "ok" and {e["id"] for e in detail["events"]} >= {first["id"]}
    assert c.json("GET", "/api/usage")[1]["rows"]  # the scripted model call was reserved and reconciled
    # the scripted scenario advanced: the next run sees the source changes
    run(app, c)
    later = c.json("GET", f"/api/events?after={ids[-1]}&limit=500")[1]["events"]
    assert any(e["repo"] == "octo-demo/field-notes" and e["stage"] == "publish" and e["data"].get("commit_sha") for e in later)
    st, diag = c.json("GET", "/api/diagnostics")
    assert st == 200 and diag["checks"][0]["name"] == "simulation"


def read_stream(port, cookie, last_id=None, until=None, timeout=10.0):
    """Collects (id, event) pairs from the stream until `until(ids)` is true."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Host": f"127.0.0.1:{port}", "Cookie": f"rm_session={cookie}"}
    if last_id is not None:
        headers["Last-Event-ID"] = str(last_id)
    conn.request("GET", "/api/events/stream", headers=headers)
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
    got, current, beats = [], {}, 0
    end = time.monotonic() + timeout
    while time.monotonic() < end and not (until and until([i for i, _ in got])):
        line = resp.fp.readline().decode().rstrip("\n")
        if line.startswith(":"):
            beats += 1
        elif line.startswith("id: "):
            current["id"] = int(line[4:])
        elif line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[6:])
        elif line == "" and "id" in current:
            got.append((current["id"], current))
            current = {}
        if until is None and beats:
            break
    conn.close()
    return got, beats


def test_event_stream_resumes_without_duplicates_or_new_runs(start, tmp_path, offline):
    app, c = start(tmp_path / "demo", demo=True)
    start_cursor = c.json("GET", "/api/state")[1]["cursor"]
    run(app, c, kind="dry_run")
    final = c.json("GET", "/api/state")[1]["cursor"]
    assert final > start_cursor + 3
    got, _ = read_stream(app.port, c.cookie, start_cursor, until=lambda ids: final in ids)
    ids = [i for i, _ in got]
    assert ids == list(range(start_cursor + 1, final + 1))
    assert all(e["event"] == "activity" and e["data"]["id"] == i for i, e in got)
    runs_before = c.json("GET", "/api/runs")[1]["runs"]
    middle = ids[len(ids) // 2]
    again, _ = read_stream(app.port, c.cookie, middle, until=lambda x: final in x)
    assert [i for i, _ in again] == list(range(middle + 1, final + 1))  # nothing the client already had
    assert c.json("GET", "/api/runs")[1]["runs"] == runs_before and not app.thread.is_alive()  # a reconnect starts nothing
    none, beats = read_stream(app.port, c.cookie, final, timeout=5)  # caught up: only heartbeats
    assert none == [] and beats >= 1


def test_event_stream_ends_when_the_server_stops(start, tmp_path, offline):
    app, c = start(tmp_path / "demo", demo=True)
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    conn.request("GET", "/api/events/stream", headers={"Host": f"127.0.0.1:{app.port}", "Cookie": f"rm_session={c.cookie}"})
    resp = conn.getresponse()
    assert resp.fp.readline() == b"retry: 3000\n"
    app.stop.set()
    began = time.monotonic()
    while resp.fp.readline():
        pass
    assert time.monotonic() - began < 3
    conn.close()


def _held_launch(monkeypatch, app, fail=False):
    """Hold the launched run back before it records its run, like a subprocess still starting,
    until the test releases it. Meanwhile another writer (a scheduled run, say) records a run."""
    original, go, seen = app.demo.run_cycle, threading.Event(), {}

    def held(*a, **k):
        assert go.wait(30)
        other = DB(app.demo.db_path)
        rid = other.start_run("scheduled", "someone")
        seen["other_stopped"] = other.cancel_requested(rid)  # started after Cancel: must not be stopped
        other.finish_run(rid, "ok", {})
        other.close()
        if fail:
            raise RuntimeError("the launch ended before recording a run")
        return original(*a, **k)

    monkeypatch.setattr(app.demo, "run_cycle", held)
    return original, go, seen


def test_cancel_pressed_before_the_launched_run_is_recorded_stops_that_run_only(start, tmp_path, offline, monkeypatch):
    app, c = start(tmp_path / "demo", demo=True, latency=(0.05, 0.3))
    before = c.json("GET", "/api/runs")[1]["runs"]
    _, go, seen = _held_launch(monkeypatch, app)
    assert c.json("POST", "/api/run", {"kind": "live"})[1]["started"] is True
    assert c.json("POST", "/api/cancel")[1] == {"ok": True, "requested": True}
    assert c.json("GET", "/api/runs")[1]["runs"] == before  # Cancel came before the launched run was recorded
    go.set()
    wait(lambda: not app.thread.is_alive(), 60)
    runs = c.json("GET", "/api/runs?limit=2")[1]["runs"]
    assert [r["outcome"] for r in runs] == ["cancelled", "ok"] and seen["other_stopped"] is False


def test_cancel_during_a_launch_that_records_no_run_does_not_stop_the_next_run(start, tmp_path, offline, monkeypatch):
    app, c = start(tmp_path / "demo", demo=True)
    original, go, seen = _held_launch(monkeypatch, app, fail=True)
    assert c.json("POST", "/api/run", {"kind": "live"})[1]["started"] is True
    assert c.json("POST", "/api/cancel")[1] == {"ok": True, "requested": True}
    go.set()
    wait(lambda: not app.thread.is_alive(), 60)
    assert seen["other_stopped"] is False
    monkeypatch.setattr(app.demo, "run_cycle", original)
    run(app, c)
    assert c.json("GET", "/api/runs?limit=1")[1]["runs"][0]["outcome"] == "ok"

def test_rapid_runs_cancel_pause_and_schedule(start, tmp_path, offline):
    app, c = start(tmp_path / "demo", demo=True, latency=(0.05, 0.3))
    answers = []
    threads = [threading.Thread(target=lambda: answers.append(c.json("POST", "/api/run", {"kind": "live"})[1])) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(a["started"] for a in answers) == [False, True]
    assert [a.get("reason") for a in answers if not a["started"]] == ["already_running"]
    assert c.json("GET", "/api/state")[1]["runtime"]["state"] == "running"
    assert c.json("POST", "/api/cancel")[1] == {"ok": True, "requested": True}
    wait(lambda: not app.thread.is_alive(), 60)
    assert c.json("GET", "/api/runs?limit=1")[1]["runs"][0]["outcome"] == "cancelled"

    assert c.json("POST", "/api/pause", {}) == (200, {"ok": True})
    assert c.json("POST", "/api/run", {"kind": "live"})[1]["reason"] == "paused"
    assert c.json("POST", "/api/pause", {"repo": "octo-demo/trail-maps"}) == (200, {"ok": True})
    assert c.json("POST", "/api/resume", {}) == (200, {"ok": True})
    state = c.json("GET", "/api/state")[1]
    assert next(r for r in state["repos"] if r["name"] == "octo-demo/trail-maps")["paused"] is True

    st, r = c.json("PUT", "/api/schedule", {"mode": "weekly", "time": "06:45", "weekdays": ["sat"], "timezone": "local"})
    assert st == 200 and r["applied"] is True and r["schedule"]["in_sync"] is True
    assert r["schedule"]["effective"]["weekdays"] == ["sat"] and r["schedule"]["summary"] == "Sat at 06:45 (this computer's time)"
    assert 'weekdays = ["sat"]' in (app.home / "config.toml").read_text(encoding="utf-8")
    assert c.json("PUT", "/api/schedule", {"mode": "interval", "interval_minutes": 2})[0] == 400
    assert c.json("POST", "/api/installation/deactivate", {"confirm": True})[0] == 200
    st, r = c.json("PUT", "/api/schedule", {"mode": "daily", "time": "05:00"})
    assert r["applied"] is False and r["schedule"]["effective"] is None
    assert c.json("POST", "/api/run", {"kind": "live"})[1]["reason"] == "inactive"
    assert c.json("GET", "/api/state")[1]["runtime"]["state"] == "inactive"


def test_demo_credentials_are_discarded_and_discovery_lists(start, tmp_path, offline):
    app, c = start(tmp_path / "demo", demo=True)
    secret = "ghp_" + "Q" * 36
    st, r = c.json("POST", "/api/credentials/github", {"value": secret})
    assert st == 200 and r["login"] == "octo-demo"
    st, found = c.json("POST", "/api/github/discover")
    assert st == 200 and len(found["repositories"]) == 5 and all(x["enrolled"] for x in found["repositories"])
    for f in app.home.rglob("*"):
        if f.is_file():
            assert secret.encode() not in f.read_bytes()
    assert all(secret.encode() not in b for b in c.seen)


def test_readme_markup_is_returned_as_inert_json(start, tmp_path, offline):
    app, c = start(tmp_path / "demo", demo=True)
    hostile = '\n<script>alert(1)</script> [x](javascript:alert(2)) <img src=x onerror="alert(3)">\n'
    fake = app.demo.repo("field-notes")
    fake.push_commit({"README.md": fake.files()["README.md"] + hostile, "maps/new.txt": "x\n"}, "Hand edit")
    run(app, c, kind="dry_run", repos=["octo-demo/field-notes"])
    did = next(r for r in c.json("GET", "/api/state")[1]["repos"] if r["name"] == "octo-demo/field-notes")["latest_draft_id"]
    status, headers, body = c.req("GET", f"/api/drafts/{did}")
    assert status == 200 and headers["content-type"].startswith("application/json") and headers["x-content-type-options"] == "nosniff"
    assert b"<script" not in body and b"<img" not in body
    old = json.loads(body)["files"]["README.md"]["old"]
    assert "<script>alert(1)</script>" in old and "javascript:alert(2)" in old and 'onerror="alert(3)"' in old


def test_demo_answers_never_name_the_real_computer_or_account(start, tmp_path, offline, monkeypatch):
    from readme_monitor import runtime

    monkeypatch.setattr(runtime, "hostname", lambda: "MARKERHOST7")
    monkeypatch.setattr(scheduler, "current_user", lambda: "MARKERHOST7\\markeruser7")
    app, c = start(tmp_path / "demo", demo=True)
    run(app, c, kind="dry_run")
    run(app, c)
    run_id = c.json("GET", "/api/runs")[1]["runs"][0]["id"]
    for path in ("/api/state", "/api/events?limit=500", "/api/runs", f"/api/runs/{run_id}", "/api/diagnostics", "/api/usage",
                 "/api/session"):
        assert c.json("GET", path)[0] == 200
    st, r = c.json("POST", "/api/github/discover")
    assert st == 200
    for body in c.seen:
        text = body.decode("utf-8").lower()
        assert "markerhost7" not in text and "markeruser7" not in text
        assert str(tmp_path).lower().replace("\\", "\\\\") not in text  # no real folder path (it names the account)
    state = c.json("GET", "/api/state")[1]
    assert state["installation"]["host"] == "DEMO-PC" and state["installation"]["account"] == "demo"


def test_demo_commit_dates_are_recent(tmp_path):
    from datetime import datetime, timedelta, timezone

    from readme_monitor.simulation import build_demo

    demo = build_demo(tmp_path / "demo")
    fake = demo.repo("atlas-api")
    seeded = datetime.fromisoformat(fake.commits[fake.branches["main"]]["date"])
    assert timedelta(days=1) < datetime.now(timezone.utc) - seeded < timedelta(days=7)
    demo.advance()
    new = datetime.fromisoformat(fake.commits[fake.branches["main"]]["date"])
    assert datetime.now(timezone.utc) - new < timedelta(minutes=5)
    demo.db.close()


def test_schedule_notes_leave_out_zero_settings(start, tmp_path, offline):
    app, c = start(tmp_path / "demo", demo=True)  # the demo sets quiet_minutes and the update interval to 0
    notes = " ".join(c.json("GET", "/api/state")[1]["schedule"]["notes"])
    assert "0 minutes" not in notes and "every 0 hours" not in notes and "daily and monthly limits" in notes


def test_stale_demo_folders_are_removed_and_nothing_else(tmp_path, monkeypatch):
    import os

    from readme_monitor import runtime, server

    monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))
    old = time.time() - 2 * 86400

    def folder(name, pid=None, age=old):
        d = tmp_path / name
        d.mkdir()
        if pid is not None:
            runtime._write(d / server.SERVER_FILE, {"port": 1, "pid": pid})
        os.utime(d, (age, age))
        return d

    stale = folder("readme-monitor-demo-stale", pid=2**30)
    no_record = folder("readme-monitor-demo-norecord")
    alive = folder("readme-monitor-demo-alive", pid=os.getpid())
    fresh = folder("readme-monitor-demo-fresh", age=time.time())
    other = folder("someone-elses-folder")
    assert sorted(server.clean_stale_demos()) == sorted([stale, no_record])
    assert not stale.exists() and not no_record.exists() and alive.exists() and fresh.exists() and other.exists()
