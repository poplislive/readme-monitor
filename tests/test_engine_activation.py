"""Installation activation: any computer name, host_lock, one active folder per account, legacy
migration, deactivation, and what an unactivated installation may still do. Simulated machine
identity only; the account pointer and Task Scheduler are replaced by conftest."""

import json
import os

import pytest

from readme_monitor import cli, config, runtime, scheduler
from readme_monitor.db import DB
from readme_monitor.simulation import FakeClaude, FakeGitHub

pytestmark = pytest.mark.real_host  # the real guard, with a simulated machine identity


@pytest.fixture
def pc(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "hostname", lambda: "ANY-NAME-7")
    monkeypatch.setattr(runtime, "machine_guid", lambda: "guid-1")
    monkeypatch.setattr(runtime, "WINDOWS", True)
    monkeypatch.delenv("READMEMONITOR_HOME", raising=False)
    home = (tmp_path / "rt").resolve()
    home.mkdir()
    return home


def test_any_computer_name_can_be_activated_and_a_rename_keeps_it(pc, monkeypatch):
    assert "not enabled" in runtime.activation_problem(pc)
    data = runtime.activate(pc, "ANY-NAME-7\\someone")
    assert data["host"] == "ANY-NAME-7" and data["version"] == 2 and data["installation_id"]
    assert runtime.activation_problem(pc) is None
    monkeypatch.setattr(runtime, "hostname", lambda: "RENAMED-PC")
    assert runtime.activation_problem(pc) is None  # same MachineGuid: still this computer
    assert runtime.activate(pc, "x")["installation_id"] == data["installation_id"]  # idempotent


def test_host_lock_pins_the_computer_name(pc):
    with pytest.raises(runtime.NotActivated, match="host_lock allows only WORKSTATION-1"):
        runtime.activate(pc, "u", host_lock="WORKSTATION-1")
    runtime.activate(pc, "u", host_lock="any-name-7")  # case-insensitive
    assert runtime.activation_problem(pc, "any-name-7") is None
    assert "host_lock" in runtime.activation_problem(pc, "WORKSTATION-1")


def test_one_active_folder_per_account(pc, tmp_path):
    second = (tmp_path / "second").resolve()
    runtime.activate(pc, "u")
    assert config.default_home() == pc  # the pointer names the active folder
    with pytest.raises(runtime.NotActivated, match="already has an active runtime folder"):
        runtime.activate(second, "u")
    runtime.deactivate(pc)
    runtime.activate(second, "u")
    assert config.default_home() == second
    os.environ["READMEMONITOR_HOME"] = str(pc)
    try:
        assert config.default_home() == pc  # the environment variable still wins
    finally:
        del os.environ["READMEMONITOR_HOME"]


def test_copied_folder_stays_inactive_until_reactivated_deliberately_and_keeps_its_ledger(pc, monkeypatch):
    first = runtime.activate(pc, "u")
    db = DB(pc / "state.db")
    db.reserve("o/r", 1, "m", 0.3)
    db.close()
    monkeypatch.setattr(runtime, "machine_guid", lambda: "guid-other")
    assert "another computer" in runtime.activation_problem(pc)
    with pytest.raises(runtime.NotActivated):
        runtime.require_active(pc)
    second = runtime.activate(pc, "u")
    assert second["installation_id"] != first["installation_id"]
    assert len(list(pc.glob("activation.previous-*.json"))) == 1
    assert DB(pc / "state.db").spent_since("0") == pytest.approx(0.3)  # the ledger is kept as it is


def test_moved_folder_on_the_same_computer_stays_inactive(pc, tmp_path):
    runtime.activate(pc, "u")
    moved = tmp_path / "moved"
    moved.mkdir()
    (moved / runtime.ACTIVATION_FILE).write_text((pc / runtime.ACTIVATION_FILE).read_text(), encoding="utf-8")
    assert "moved or copied" in runtime.activation_problem(moved)


def test_deactivate_removes_the_task_and_keeps_data(pc, monkeypatch):
    runtime.activate(pc, "u")
    (pc / "state.db").write_bytes(b"x")
    removed = []
    monkeypatch.setattr(scheduler, "query", lambda: {"Status": "Ready"})
    monkeypatch.setattr(scheduler, "uninstall", lambda: removed.append(1) or "SUCCESS")
    r = runtime.deactivate(pc)
    assert r["task_removed"] and removed == [1] and r["revoked"]
    assert runtime.activation_problem(pc) and (pc / "state.db").exists()
    assert not runtime.pointer_path().exists()


def test_not_windows_cannot_be_activated(pc, monkeypatch):
    monkeypatch.setattr(runtime, "WINDOWS", False)
    assert "only be enabled on Windows" in runtime.activation_problem(pc)
    with pytest.raises(runtime.NotActivated):
        runtime.activate(pc, "u")


def _legacy(where, **over):
    data = {"host": "ANY-NAME-7", "machine_guid": "guid-1", "home": str(where), "account": "u", "bound_at": "2026-01-01T00:00:00+00:00"}
    data.update(over)
    (where / runtime.LEGACY_FILE).write_text(json.dumps(data), encoding="utf-8")


def test_legacy_record_migrates_only_on_a_full_match(pc, tmp_path, monkeypatch):
    _legacy(pc)
    assert runtime.migrate_legacy(pc) == "migrated"
    rec = runtime.activation(pc)
    assert rec["migrated_from"] == "production-host.json" and runtime.activation_problem(pc) is None
    assert (pc / runtime.LEGACY_FILE).exists()  # the legacy file is kept
    assert runtime.migrate_legacy(pc) == "present"  # idempotent
    for name, over in [("guid", {"machine_guid": "guid-2"}), ("home", {"home": str(tmp_path / "elsewhere")}), ("host", {"host": "OTHER"})]:
        home = tmp_path / name
        home.mkdir()
        _legacy(home, **{"home": str(home), **over})
        assert runtime.migrate_legacy(home) == "mismatch"
        assert "earlier version" in runtime.activation_problem(home)


def test_interrupted_legacy_migration_can_simply_rerun(pc, monkeypatch):
    _legacy(pc)
    (pc / "activation.json.tmp").write_text("{half", encoding="utf-8")  # a crash mid-write left this
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("power cut")
        return real(a, b)

    monkeypatch.setattr(runtime.os, "replace", flaky)
    with pytest.raises(OSError):
        runtime.migrate_legacy(pc)
    assert runtime.activation(pc) is None and runtime.activation_problem(pc)
    assert runtime.migrate_legacy(pc) == "migrated"
    assert runtime.activation_problem(pc) is None


def test_cli_migrates_the_legacy_record_at_start_and_status_reports_installation(pc, capsys):
    _legacy(pc)
    (pc / "config.toml").write_text('[[repo]]\nname = "o/r"\n', encoding="utf-8")
    assert cli.main(["status", "--json", "--home", str(pc)]) == 0
    inst = json.loads(capsys.readouterr().out)["installation"]
    assert inst["active"] and inst["activation"]["migrated_from"] == "production-host.json" and inst["problem"] is None


def test_cli_activate_deactivate_and_deprecated_bind_host(pc, capsys):
    (pc / "config.toml").write_text('[installation]\nhost_lock = "ANY-NAME-7"\n', encoding="utf-8")
    assert cli.main(["bind-host", "--home", str(pc)]) == 0
    out = capsys.readouterr().out
    assert "deprecated" in out and "Worker enabled on ANY-NAME-7" in out
    assert cli.main(["status", "--home", str(pc)]) == 0
    assert "Installation: ACTIVE" in capsys.readouterr().out
    assert cli.main(["deactivate", "--home", str(pc)]) == 0
    assert "revoked" in capsys.readouterr().out and runtime.activation_problem(pc)


def test_unactivated_installation_still_runs_dry_run_and_read_only_commands(pc, monkeypatch, capsys):
    from .conftest import initial_readme

    (pc / "config.toml").write_text('[budget]\nper_run_usd = 1.0\ndaily_usd = 1.0\nmonthly_usd = 1.0\n[[repo]]\nname = "owner/demo"\n'
                                    'source_include = ["src/**/*.py"]\nmanaged_sections = ["features", "project-structure"]\n'
                                    'publish_mode = "direct"\napi_export = "allowlist"\n', encoding="utf-8")
    fake = FakeGitHub({"README.md": initial_readme(), "src/a.py": "A = 1\n"})
    asked = []
    monkeypatch.setattr(cli.credentials, "get", lambda kind: asked.append(kind) or ("token", "test"))
    monkeypatch.setattr(cli, "GitHub", lambda token, db=None: fake)
    claude = FakeClaude()
    monkeypatch.setattr(cli.Generator, "client", property(lambda self: claude))
    for argv in (["run-once", "--dry-run"], ["run-once", "--dry-run", "--repo", "owner/demo"], ["status"], ["schedule", "--json"],
                 ["doctor"]):
        assert cli.main([*argv, "--home", str(pc)]) in (0, 1), argv  # doctor reports FAIL for the inactive folder, never refuses
    assert fake.writes == [] and claude.calls == [] and asked.count("anthropic") <= 1  # doctor checks presence only
    db = DB(pc / "state.db")
    assert db.conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM drafts WHERE kind='dry_run'").fetchone()[0] == 2
    for argv in (["run-once"], ["install-task"], ["run-task-now"], ["preview", "--repo", "owner/demo", "--api"]):
        assert cli.main([*argv, "--home", str(pc)]) == 3, argv
    assert fake.writes == [] and claude.calls == []


def test_deactivating_a_migrated_installation_is_not_undone_by_the_legacy_record(pc):
    _legacy(pc)
    assert runtime.migrate_legacy(pc) == "migrated"
    runtime.deactivate(pc)
    assert runtime.migrate_legacy(pc) == "none"  # the old record must not switch the worker back on
    assert runtime.activation_problem(pc)
