"""Schema 3 migration (including interruption), event and draft storage, cancellation flags and
the config writer."""

import json
import sqlite3
import textwrap
from dataclasses import replace

import pytest

from readme_monitor import cli, db as dbm
from readme_monitor.config import ConfigError, dumps, load, load_raw, save
from readme_monitor.db import DB, REPO_COLUMNS_V2, SCHEMA

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2


def _old_db(path, version):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    if version == 2:
        for col, decl in REPO_COLUMNS_V2:
            if col != "analyzed_head":
                conn.execute(f"ALTER TABLE repos ADD COLUMN {col} {decl}")
    conn.execute("INSERT INTO repos(name, state, analyzed_fp, quick_key) VALUES('o/r', 'awaiting_merge', 'fp', 'qk')")
    conn.execute("INSERT INTO proposals(repo, fingerprint, base_sha, mode, state, files, created_at, updated_at) "
                 "VALUES('o/r','fp','abc','review_pr','awaiting_merge','{}','t','t')")
    for state, reserved, actual in (("reconciled", 0.3, 0.125), ("reserved", 0.2, None), ("ambiguous", 0.25, None)):
        conn.execute("INSERT INTO usage(repo, run_id, model, state, reserved_usd, actual_usd, created_at, updated_at) "
                     "VALUES('o/r', 1, 'm', ?, ?, ?, '2026-11-10T01:00:00+00:00', 'x')", (state, reserved, actual))
    conn.execute("INSERT INTO runs(started_at, trigger, outcome) VALUES('2026-11-10T01:00:00+00:00', 'scheduled', 'ok')")
    conn.execute("INSERT INTO settings(key, value) VALUES('paused', '1')")
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()


def _intact(db):
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 3
    row = db.repo("o/r")
    assert (row["state"], row["analyzed_fp"], row["quick_key"]) == ("awaiting_merge", "fp", "qk") and "analyzed_head" in row.keys()
    assert db.latest_proposal("o/r")["state"] == "awaiting_merge"
    assert [r["state"] for r in db.conn.execute("SELECT state FROM usage ORDER BY id")] == ["reconciled", "reserved", "ambiguous"]
    assert db.spent_since("0") == pytest.approx(0.125 + 0.2 + 0.25)
    assert db.last_runs(1)[0]["trigger"] == "scheduled" and db.globally_paused
    assert db.events() == [] and db.add_event(None, None, None, "run", "info", "x") == 1


@pytest.mark.parametrize("version", [1, 2])
def test_schema_1_and_2_migrate_with_everything_intact(tmp_path, version):
    _old_db(tmp_path / "state.db", version)
    _intact(DB(tmp_path / "state.db"))
    backup = next((tmp_path / "backups").glob("pre-schema3-*.db"))
    assert sqlite3.connect(backup).execute("PRAGMA user_version").fetchone()[0] == version


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("stop", range(4))
def test_interrupted_migration_reruns_safely(tmp_path, monkeypatch, version, stop):
    _old_db(tmp_path / "state.db", version)
    real = list(dbm.MIGRATION_STEPS)

    def boom(conn):
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(dbm, "MIGRATION_STEPS", real[:stop] + [boom])
    with pytest.raises(sqlite3.OperationalError):
        DB(tmp_path / "state.db")
    monkeypatch.setattr(dbm, "MIGRATION_STEPS", real)
    _intact(DB(tmp_path / "state.db"))


def test_newer_schema_refuses_and_restore_keeps_schema3(tmp_path):
    db = DB(tmp_path / "state.db")
    db.add_event(1, "manual", "o/r", "run", "done", "kept")
    db.backup_to(tmp_path / "copy.db")
    db.reserve("o/r", 2, "m", 0.4)
    assert db.restore_from(tmp_path / "copy.db") == 1
    assert db.events()[0]["message"] == "kept" and db.spent_since("0") == pytest.approx(0.4)
    db.close()
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute("PRAGMA user_version=4")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="newer ReadmeMonitor"):
        DB(tmp_path / "state.db")


def test_events_are_redacted_bounded_and_paged(tmp_path, monkeypatch):
    db = DB(tmp_path / "s.db")
    eid = db.add_event(1, "manual", "o/r", "inspect", "done", f"commit says {TOKEN} " + "x" * 400,
                       {"commits": [{"message": f"leak {TOKEN}"}], "nested": {"error": f"api_key = '{'s' * 12}'"},
                       "files_changed": [{"path": f"f{i}.py" * 20} for i in range(5000)]})
    raw = db.conn.execute("SELECT message, data FROM events WHERE id=?", (eid,)).fetchone()
    assert TOKEN not in raw["message"] + raw["data"] and "s" * 12 not in raw["data"]
    assert len(raw["message"]) == 300 and len(raw["data"]) <= dbm.MAX_EVENT_DATA
    data = db.events()[0]["data"]
    assert data["truncated"] and 0 < len(data["files_changed"]) < 5000
    monkeypatch.setattr(dbm, "MAX_EVENTS", 5)
    for i in range(10):
        db.add_event(2 if i % 2 else 3, "scheduled", "o/a" if i % 2 else "o/b", "result", "done", f"m{i}")
    assert db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 5
    last = db.last_event_id()
    assert [e["id"] for e in db.events(limit=2)] == [last - 1, last]
    assert [e["message"] for e in db.events(after=last - 2)] == ["m8", "m9"]
    assert {e["repo"] for e in db.events(run_id=2)} == {"o/a"} and {e["run_id"] for e in db.events(repo="o/b")} == {3}


def test_drafts_and_cancellation(tmp_path, monkeypatch):
    db = DB(tmp_path / "s.db")
    monkeypatch.setattr(dbm, "MAX_DRAFTS", 2)
    ids = [db.add_draft(1, "o/r", "dry_run", {"README.md": {"old": "a", "new": f"b {TOKEN}", "diff": "", "changed": [], "conflicts": []}},
                        ["flag"], ["note"], 0.01) for _ in range(3)]
    assert db.draft(ids[0]) is None and db.latest_draft_id("o/r") == ids[2]
    d = db.draft(ids[2])
    assert TOKEN not in json.dumps(d) and d["flags"] == ["flag"] and d["estimate_usd"] == 0.01
    run = db.start_run("manual", "u")
    db.request_cancel()
    assert db.cancel_requested(run)  # the run that was going when Cancel was pressed stops
    assert not db.cancel_requested(db.start_run("manual", "u"))  # a later run, even in the same second, does not
    db.request_cancel("launch-a")  # also the run the interface launched, whenever it records its run
    later = db.start_run("manual", "u")
    assert db.cancel_requested(later, "launch-a") and not db.cancel_requested(later, "launch-b") and not db.cancel_requested(later)


# ---- config ------------------------------------------------------------------------------

FULL = """
[monitor]
poll_interval_minutes = 7
model = "claude-opus-5-5"
effort = "high"
max_output_tokens = 9000
request_timeout_seconds = 90
max_attempts = 3
run_timeout_seconds = 900
max_repos_per_run = 20
check_wait_minutes = 10
bot_branch = "docs/readme-bot"
max_file_bytes = 50000
max_payload_chars = 100000
quiet_minutes = 15
min_generation_interval_hours = 6.5
max_postpone_hours = 48
[installation]
host_lock = "WORKSTATION-1"
[schedule]
mode = "weekly"
interval_minutes = 60
time = "07:30"
weekdays = ["fri", "mon"]
timezone = "local"
[budget]
per_run_usd = 0.25
daily_usd = 1.5
monthly_usd = 9.75
pacing = "none"
[discovery]
owner = "octo-demo"
refresh_hours = 12
publish_mode = "auto_pr"
managed_sections = ["project-structure"]
adoptions_per_day = 1
exclude = ["octo-demo/old"]
[policy]
export_restricted = ["octo-demo/secret"]
[pricing."claude-opus-5-5"]
input = 4.0
output = 20.0
cache_write = 5.0
cache_read = 0.2
[[repo]]
name = "octo-demo/atlas-api"
branch = "main"
readme_paths = ["README.md", "docs/README.md"]
source_include = ["src/**/*.py"]
source_exclude = ["src/vendor/**"]
template_version = 1
managed_sections = ["features", "status"]
disabled_sections = ["status"]
publish_mode = "direct"
api_export = "allowlist"
monthly_usd = 2.0
daily_usd = 0.5
paused = true
[[repo]]
name = "octo-demo/field-notes"
"""


@pytest.mark.parametrize("source", ["example", "full"])
def test_config_writer_round_trip(tmp_path, source):
    path = cli.EXAMPLE_CONFIG if source == "example" else tmp_path / "in.toml"
    if source == "full":
        path.write_text(textwrap.dedent(FULL), encoding="utf-8")
    original = load(path)
    out = tmp_path / "out.toml"
    out.write_text(dumps(load_raw(path)), encoding="utf-8")
    assert replace(load(out), path=path) == original
    assert load_raw(tmp_path / "missing.toml") == {}


def test_save_validates_backs_up_once_and_replaces_atomically(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# hand-written\n[monitor]\nquiet_minutes = 5\n", encoding="utf-8")
    raw = load_raw(path)
    raw["schedule"] = {"mode": "daily", "time": "08:15"}
    cfg = save(path, raw)
    assert cfg.schedule.mode == "daily" and cfg.path == path and load(path).quiet_minutes == 5
    assert len(list(tmp_path.glob("config.toml.pre-ui-*.bak"))) == 1
    raw["monitor"]["quiet_minutes"] = 6
    save(path, raw)  # a file it wrote itself is not backed up again
    assert len(list(tmp_path.glob("config.toml.pre-ui-*.bak"))) == 1
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError):
        save(path, {**raw, "schedule": {"mode": "hourly"}})
    assert path.read_text(encoding="utf-8") == before and not (tmp_path / "config.toml.tmp").exists()


@pytest.mark.parametrize("body,msg", [
    ('[discovery]\nowner = "o"\npublish_mode = "direct"', "discovery.publish_mode"),
    ('[schedule]\nmode = "hourly"', "schedule.mode"),
    ('[schedule]\ninterval_minutes = 0', "between 1 and 1440"),
    ('[schedule]\ninterval_minutes = 1441', "between 1 and 1440"),
    ('[schedule]\ntime = "25:00"', "HH:MM"),
    ('[schedule]\nmode = "weekly"\nweekdays = []', "at least one day"),
    ('[schedule]\nweekdays = ["funday"]', "may only contain"),
    ('[schedule]\ntimezone = "mars"', "local or utc"),
    ('[installation]\nhost_lock = "bad name!"', "host_lock"),
])
def test_new_config_validation(tmp_path, body, msg):
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match=msg):
        load(p)


def test_without_schedule_table_the_old_interval_applies(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[monitor]\npoll_interval_minutes = 7\n[[repo]]\nname = \"o/r\"\npublish_mode = \"direct\"\n", encoding="utf-8")
    cfg = load(p)
    assert (cfg.schedule.mode, cfg.schedule.interval_minutes) == ("interval", 7) and cfg.repos[0].publish_mode == "direct"
