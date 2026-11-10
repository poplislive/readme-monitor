import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from readme_monitor import cli
from readme_monitor.config import ConfigError, Schedule, load
from readme_monitor.scheduler import task_xml

ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


@pytest.mark.parametrize("body,msg", [
    ('[[repo]]\nname = "bad name"', "owner/repository"),
    ('[[repo]]\nname = "o/r"\nreadme_paths = ["../x.md"]', "relative path"),
    ('[[repo]]\nname = "o/r"\npublish_mode = "yolo"', "publish_mode"),
    ('[[repo]]\nname = "o/r"\napi_export = "allowlist"', "source_include"),
    ('[[repo]]\nname = "o/r"\nmanaged_sections = ["hero"]', "unknown section"),
    ('[[repo]]\nname = "o/r"\n[[repo]]\nname = "O/R"', "more than once"),
    ('[budget]\ndaily_usd = -1', "between"),
    ('[surprise]', "unknown top-level"),
])
def test_config_validation(tmp_path, body, msg):
    with pytest.raises(ConfigError, match=msg):
        load(write(tmp_path, body))


def test_example_config_is_valid_and_spends_nothing(tmp_path):
    cfg = load(cli.EXAMPLE_CONFIG)
    assert not cfg.budget.configured and cfg.repos == () and not cfg.discovery.enabled
    assert cfg.model == "claude-sonnet-5-5" and cfg.price() is not None
    assert cfg.schedule.mode == "interval" and cfg.host_lock == ""
    assert "your-account/your-repository" in cli.EXAMPLE_CONFIG.read_text(encoding="utf-8")


def test_task_xml_is_well_formed_and_safe():
    xml = task_xml(Path(r"C:\Program Files\x\pythonw.exe"), Path(r"C:\src\ReadmeMonitor"), Path(r"C:\Users\u\AppData\Local\ReadmeMonitor"),
                   Path(r"C:\Users\u\AppData\Local\ReadmeMonitor\config.toml"), Schedule(interval_minutes=5), 15, r"WORKSTATION-1\user")
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))
    assert root.find(".//t:Repetition/t:Interval", ns).text == "PT5M"
    assert root.find(".//t:MultipleInstancesPolicy", ns).text == "IgnoreNew"
    assert root.find(".//t:StartWhenAvailable", ns).text == "true"
    assert root.find(".//t:ExecutionTimeLimit", ns).text == "PT15M"
    args = root.find(".//t:Arguments", ns).text
    assert "run-once --trigger scheduled" in args and "key" not in args.lower() and "token" not in args.lower()


def test_cli_commands_documented_in_readme_exist():
    """Every `readme-monitor <command>` shown in the docs is a real command."""
    parser = cli.build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)
    docs = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    used = set()
    for doc in docs:
        used |= set(re.findall(r"readme-monitor ([a-z][a-z-]+)", doc.read_text(encoding="utf-8")))
    assert used, "no commands found in documentation"
    assert used <= commands, f"documented but missing: {sorted(used - commands)}"
    for required in ("doctor", "init", "preview", "run-once", "status", "pause", "resume", "install-task", "uninstall-task"):
        assert required in commands


def test_readme_relative_links_exist():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)", text):
        assert (ROOT / target.split("#")[0]).exists(), target


def test_pause_resume_and_status(tmp_path, capsys):
    cfg = write(tmp_path, '[[repo]]\nname = "o/r"\n')
    base = ["--home", str(tmp_path), "--config", str(cfg)]
    assert cli.main(["pause", *base]) == 0
    assert cli.main(["status", *base]) == 0
    assert "PAUSED" in capsys.readouterr().out
    assert cli.main(["resume", "--repo", "o/r", *base]) == 0
    assert cli.main(["resume", *base]) == 0
    cli.main(["status", *base])
    out = capsys.readouterr().out
    assert "active" in out and "NOT CONFIGURED" in out


def test_backup_and_restore(tmp_path, capsys):
    cfg = write(tmp_path, '[[repo]]\nname = "o/r"\n')
    base = ["--home", str(tmp_path), "--config", str(cfg)]
    cli.main(["pause", *base])
    assert cli.main(["backup-db", *base]) == 0
    backup = next((tmp_path / "backups").glob("state-*.db"))
    cli.main(["resume", *base])
    assert cli.main(["restore-db", str(backup), *base]) == 0
    cli.main(["status", *base])
    assert "PAUSED" in capsys.readouterr().out


def test_run_once_without_github_credential_is_actionable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.credentials, "get", lambda kind: (None, "missing"))
    cfg = write(tmp_path, '[[repo]]\nname = "o/r"\n')
    with pytest.raises(SystemExit, match="set-credential github"):
        cli.main(["run-once", "--home", str(tmp_path), "--config", str(cfg)])
    cli.main(["status", "--home", str(tmp_path), "--config", str(cfg)])
    assert "credentials_missing" in capsys.readouterr().out
    from readme_monitor.db import DB

    db = DB(tmp_path / "state.db")
    ev = db.events(limit=5)[-1]
    assert ev["stage"] == "run" and ev["status"] == "failed" and ev["data"]["outcome"] == "credentials_missing"
    assert ev["run_id"] == db.last_runs(1)[0]["id"]
    db.close()


def test_run_once_crash_stores_a_failed_run_event_without_the_message(tmp_path, monkeypatch, capsys):
    from readme_monitor.db import DB
    from readme_monitor.worker import Worker

    secret = "ghp_" + "Q1w2E3r4T5y6U7i8O9p0" * 2
    monkeypatch.setattr(cli.credentials, "get", lambda kind: ("token-for-test", "credential-manager"))

    def boom(self):
        raise RuntimeError(f"broke while holding {secret}")

    monkeypatch.setattr(Worker, "run", boom)
    cfg = write(tmp_path, '[[repo]]\nname = "o/r"\n')
    assert cli.main(["run-once", "--home", str(tmp_path), "--config", str(cfg)]) == 1
    assert secret not in capsys.readouterr().out
    db = DB(tmp_path / "state.db")
    ev = db.events(limit=5)[-1]
    assert ev["stage"] == "run" and ev["status"] == "failed" and ev["data"] == {"outcome": "crashed", "error": "RuntimeError"}
    assert secret not in json.dumps(db.events(limit=50)) and "broke" not in ev["message"]
    assert db.last_runs(1)[0]["outcome"] == "crashed"
    db.close()
