"""Desktop release: the launch abstraction, the macOS and Linux scheduler backends (with stand-ins for
launchctl and systemctl, so nothing real is registered), the update maintenance flag every worker
honours, the update preparation and health check, the desktop window's sign-in and the helper's
stdin/stdout protocol (a real `desktop-serve --demo` subprocess)."""

from __future__ import annotations

import json
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from readme_monitor import cli, desktop, launcher, runtime, sched_launchd, sched_systemd, scheduler
from readme_monitor.config import Schedule
from readme_monitor.db import DB
from readme_monitor.worker import file_lock

from .test_server_api import Client, start  # noqa: F401 (fixture)

ARGV = ["/Applications/Readme Monitör.app/Contents/Resources/engine/rm-engine", "run-once", "--trigger", "scheduled",
        "--home", "/Users/sam/Library/Application Support/ReadmeMonitor", "--config", "/x/100% config.toml"]
SCHEDULES = [Schedule(mode="interval", interval_minutes=45), Schedule(mode="daily", time="09:00"),
             Schedule(mode="weekly", time="07:30", weekdays=("mon", "wed", "sun")), Schedule(mode="manual")]


# ---- launcher ------------------------------------------------------------------------------

def test_source_checkout_runs_this_interpreter_as_a_module(monkeypatch):
    monkeypatch.setattr(launcher, "FROZEN", False)
    assert launcher.engine()[-2:] == ["-m", "readme_monitor"] and launcher.unstable_reason() is None
    assert (launcher.workdir() / "readme_monitor").is_dir()


def test_installed_build_runs_its_own_executable(monkeypatch, tmp_path):
    exe = tmp_path / "engine" / ("rm-engine.exe" if sys.platform == "win32" else "rm-engine")
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(launcher, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(exe.parent / "_internal"), raising=False)
    assert launcher.engine() == [str(exe)] and launcher.workdir() == exe.parent.resolve()
    if sys.platform == "win32":
        (exe.parent / "rm-engine-bg.exe").write_bytes(b"")
        assert launcher.engine(windowless=True) == [str(exe.parent / "rm-engine-bg.exe")]


def test_temporary_or_mounted_copies_are_never_scheduled(monkeypatch, tmp_path):
    monkeypatch.setenv("APPIMAGE", "/home/sam/ReadmeMonitor.AppImage")
    assert "AppImage" in launcher.unstable_reason()
    monkeypatch.delenv("APPIMAGE")
    monkeypatch.setattr(launcher, "FROZEN", True)
    monkeypatch.setattr(sys, "_MEIPASS", str(Path(launcher.tempfile.gettempdir()) / "_MEI12345"), raising=False)
    assert "temporary folder" in launcher.unstable_reason()


# ---- macOS launchd --------------------------------------------------------------------------

@pytest.mark.parametrize("s", SCHEDULES)
def test_launchd_plist_round_trips(s):
    d = plistlib.loads(sched_launchd.definition(s, ARGV, Path("/Applications/x"), 20).encode())
    assert d["ProgramArguments"] == ARGV and d["RunAtLoad"] is False  # an argv list: no shell quoting at all
    assert sched_launchd.parse(d) == s


def test_launchd_weekdays_and_utc_refusal():
    d = sched_launchd.plist(Schedule(mode="weekly", time="06:15", weekdays=("mon", "sun")), ARGV, Path("/"))
    assert d["StartCalendarInterval"] == [{"Weekday": 1, "Hour": 6, "Minute": 15}, {"Weekday": 7, "Hour": 6, "Minute": 15}]
    with pytest.raises(scheduler.Unsupported):
        sched_launchd.plist(Schedule(mode="daily", time="06:15", timezone="utc"), ARGV, Path("/"))


def test_launchd_never_reloads_while_a_check_runs(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sched_launchd, "plist_path", lambda: tmp_path / "agent.plist")
    monkeypatch.setattr(sched_launchd.os, "getuid", lambda: 501, raising=False)

    def launchctl(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "\tstate = running\n" if args[0] == "print" else "", "")

    monkeypatch.setattr(sched_launchd, "_launchctl", launchctl)
    with pytest.raises(RuntimeError, match="running"):
        sched_launchd.install(Schedule(mode="daily"), ARGV, Path("/"), 20)
    assert not any(c[0] in ("bootout", "bootstrap") for c in calls) and not (tmp_path / "agent.plist").exists()


# ---- Linux systemd --------------------------------------------------------------------------

@pytest.mark.parametrize("s", SCHEDULES + [Schedule(mode="daily", time="23:45", timezone="utc")])
def test_systemd_timer_round_trips(s):
    text = sched_systemd.timer_text(s)
    assert (text is None) == (s.mode == "manual")
    if text:
        assert sched_systemd.parse_timer(text) == s
        assert ("Persistent=true" in text) == (s.mode != "interval")  # calendar: one catch-up start, never a burst


def test_systemd_service_quotes_spaces_percent_and_non_ascii():
    svc = sched_systemd.service_text(ARGV, Path("/opt/Readme Monitör"), 20)
    exec_line = re.search(r"^ExecStart=(.*)$", svc, re.M)[1]
    assert '"/Applications/Readme Monitör.app/Contents/Resources/engine/rm-engine"' in exec_line
    assert '"/x/100%% config.toml"' in exec_line and "TimeoutStartSec=20min" in svc and "Type=oneshot" in svc


def test_systemd_unit_files_alone_are_not_an_effective_schedule(monkeypatch, tmp_path):
    monkeypatch.setattr(sched_systemd, "unit_dir", lambda: tmp_path)
    (tmp_path / "readme-monitor-sync.service").write_text(sched_systemd.service_text(ARGV, Path("/"), 20))
    (tmp_path / "readme-monitor-sync.timer").write_text(sched_systemd.timer_text(Schedule(mode="daily", time="08:00")))
    state = {"timer": "inactive"}

    def show():
        return {"readme-monitor-sync.service": {"LoadState": "loaded", "ActiveState": "inactive", "ExecMainStatus": "0"},
                "readme-monitor-sync.timer": {"LoadState": "loaded", "ActiveState": state["timer"]}}

    monkeypatch.setattr(sched_systemd, "_show", show)
    assert sched_systemd.query_schedule() is None
    state["timer"] = "active"
    assert sched_systemd.query_schedule() == Schedule(mode="daily", time="08:00")


def test_systemd_reads_back_the_registered_program(monkeypatch, tmp_path):
    monkeypatch.setattr(sched_systemd, "unit_dir", lambda: tmp_path)
    (tmp_path / "readme-monitor-sync.service").write_text(sched_systemd.service_text(ARGV, Path("/"), 20), encoding="utf-8")
    assert sched_systemd.registered_program() == ARGV[0]


def test_a_check_left_by_another_copy_is_reported_not_called_in_effect(tmp_path, monkeypatch):
    home = _home(tmp_path)
    old = str(tmp_path / "old checkout" / ".venv" / "Scripts" / "pythonw.exe")
    monkeypatch.setattr(scheduler, "registered_program", lambda: old)
    monkeypatch.setattr(scheduler, "query_schedule", lambda: Schedule(mode="interval", interval_minutes=15))
    ctx = cli.Ctx(cli.build_parser().parse_args(["schedule", "--home", str(home)]))
    s = cli.schedule_state(ctx)
    assert s["program"] == {"registered": old, "current": False}
    assert scheduler.program_is_current(launcher.engine(windowless=True)[0]) is True and scheduler.program_is_current(None) is None


# ---- maintenance flag -----------------------------------------------------------------------

def _home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('[budget]\nper_run_usd = 0.5\ndaily_usd = 2.0\nmonthly_usd = 15.0\n', encoding="utf-8")
    return home


def test_every_worker_entry_point_skips_during_an_update(tmp_path, capsys, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setattr(cli, "_worker", lambda *a, **k: pytest.fail("no check may start during an update"))
    runtime.begin_maintenance(home, "0.3.0", "0.4.0", None)
    assert cli.main(["run-once", "--trigger", "scheduled", "--home", str(home)]) == 0
    assert "application update (to 0.4.0)" in capsys.readouterr().out
    assert cli.main(["init", "--repo", "a/b", "--home", str(home)]) == 0
    old = runtime._read(home / runtime.MAINTENANCE_FILE)
    runtime._write(home / runtime.MAINTENANCE_FILE, {**old, "started": time.time() - 7 * 3600})  # abandoned long ago
    assert runtime.maintenance(home) is None and (home / runtime.MAINTENANCE_FILE).exists()  # ignored, never deleted


def test_prepare_update_waits_for_a_running_check_then_backs_up_under_the_lock(tmp_path):
    home = _home(tmp_path)
    db = DB(home / "state.db")
    db.set_setting("paused", "1")  # a deliberate pause survives the update untouched
    db.close()
    with file_lock(home / "worker.lock"):
        r = desktop.prepare_update(home, "0.4.0")
    assert r["deferred"] is True and runtime.maintenance(home) is None
    r = desktop.prepare_update(home, "0.4.0")
    assert r["ok"] and not r["deferred"] and Path(r["backup"]).exists() and runtime.maintenance(home)["to_version"] == "0.4.0"
    done = desktop.finish_update(home)
    assert done["ok"] and runtime.maintenance(home) is None and list(home.glob("maintenance.finished-*.json"))
    db = DB(home / "state.db")
    assert db.globally_paused
    db.close()


def test_a_failed_health_check_keeps_the_installation_paused_for_updates(tmp_path, monkeypatch):
    home = _home(tmp_path)
    DB(home / "state.db").close()
    runtime.begin_maintenance(home, "0.4.0", "0.3.0", None)

    def newer(*a, **k):
        raise RuntimeError("state.db was created by a newer ReadmeMonitor (schema 4)")

    monkeypatch.setattr(desktop, "DB", newer)
    r = desktop.finish_update(home)
    assert r["ok"] is False and "newer" in r["error"] and runtime.maintenance(home) is not None


# ---- desktop sign-in and helper protocol ----------------------------------------------------

def test_desktop_sign_in_is_single_use_header_only_and_same_origin(start, tmp_path):  # noqa: F811
    app, c = start(tmp_path / "home", pair=False)
    assert c.req("POST", "/auth", headers={"X-RM-Pair": "x"})[0] == 405  # off unless started by the desktop shell
    code = desktop.new_code(app)
    assert c.req("GET", f"/auth?code={code}")[0] == 403  # never accepted from a URL
    assert c.req("POST", "/auth", origin=False, headers={"X-RM-Pair": code})[0] == 403
    status, headers, _ = c.req("POST", "/auth", headers={"X-RM-Pair": code})
    assert status == 204 and "HttpOnly" in headers["set-cookie"]
    assert c.req("POST", "/auth", headers={"X-RM-Pair": code})[0] == 403  # used up
    assert c.req("GET", "/desktop-auth")[0] == 200


def test_a_fresh_folder_answers_a_burst_of_first_requests(start, tmp_path):  # noqa: F811
    """Seen in the installed app: the window's first page load and the tray's state poll opened a new
    database at once and one request failed with "database is locked"."""
    import threading

    app, c = start(tmp_path / "fresh")
    assert (app.home / "state.db").exists()  # created before the first request
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(c.req("GET", "/api/state")[0])) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert codes == [200] * 12


def test_the_page_is_told_which_scheduler_and_credential_store_this_computer_uses(start, tmp_path):  # noqa: F811
    for demo in (False, True):
        _, c = start(tmp_path / f"h{demo}", demo=demo)
        inst = c.json("GET", "/api/state")[1]["installation"]
        assert inst["scheduler"] == scheduler.name() and inst["credential_store"] and inst["desktop"] is False
        assert "scheduling_unavailable" in inst


def test_helper_protocol_end_to_end():
    p = subprocess.Popen([sys.executable, "-m", "readme_monitor", "desktop-serve", "--demo"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    try:
        ready = json.loads(p.stdout.readline())
        assert ready["event"] == "ready" and ready["port"] > 0 and len(ready["code"]) >= 32 and ready["home"] == "DEMO-FOLDER"
        c = Client(ready["port"])
        status, headers, _ = c.req("POST", "/auth", headers={"X-RM-Pair": ready["code"]})
        assert status == 204
        c.cookie = re.search(r"rm_session=([^;]+)", headers["set-cookie"])[1]
        assert c.json("GET", "/api/session")[0] == 200
        for i, cmd in enumerate(("state", "pause", "resume", "sign_in"), 1):
            p.stdin.write(json.dumps({"id": i, "cmd": cmd}) + "\n")
            p.stdin.flush()
            reply = json.loads(p.stdout.readline())
            assert reply["id"] == i, reply
            if cmd == "pause":
                assert reply["paused"] is True
            if cmd == "sign_in":
                assert reply["code"] != ready["code"]
        p.stdin.close()  # the shell went away: the helper stops serving and exits
        assert p.wait(30) == 0
    finally:
        if p.poll() is None:
            p.kill()
