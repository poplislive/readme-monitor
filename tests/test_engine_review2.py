"""Regressions for defects found by the second independent engine review (0.3.0)."""

import json
import sqlite3

import pytest

from readme_monitor import scheduler
from readme_monitor.config import dumps, load
from readme_monitor.db import DB

from .conftest import REPO


def test_oversized_event_data_without_lists_stays_valid_json(tmp_path):
    db = DB(tmp_path / "s.db")
    db.add_event(None, "manual", None, "run", "info", "big", {f"k{i}": i for i in range(10_000)})
    deep: list = []
    for _ in range(3000):
        deep = [deep]
    db.add_event(None, "manual", None, "run", "info", "deep", {"a": deep})  # used to raise RecursionError
    events = db.events()  # used to raise JSONDecodeError for every read covering the first row
    assert [e["message"] for e in events] == ["big", "deep"] and events[0]["data"].get("truncated") is True
    for raw in db.conn.execute("SELECT data FROM events"):
        json.loads(raw[0])


def test_restore_never_lowers_spend_with_identical_reservations_in_one_second(tmp_path):
    db = DB(tmp_path / "state.db")
    row = ("o/r", None, "m", "reserved", 0.5, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
    insert = "INSERT INTO usage(repo, run_id, model, state, reserved_usd, created_at, updated_at) VALUES(?,?,?,?,?,?,?)"
    db.conn.execute(insert, row)
    db.backup_to(tmp_path / "older.db")  # the copy holds one charge
    db.conn.execute(insert, row)  # a second, identical charge in the same second
    assert db.spent_since("0") == pytest.approx(1.0)
    assert db.restore_from(tmp_path / "older.db") == 1
    assert db.spent_since("0") == pytest.approx(1.0)


def test_config_writer_keeps_emoji_and_control_characters(tmp_path):
    raw = {"repo": [{"name": REPO, "branch": "docs-\U0001f600", "readme_paths": ["README.md"]}],
           "pricing": {"model \U0001f680 \x7f": {"input": 1.0, "output": 2.0, "cache_write": 1.25, "cache_read": 0.1}}}
    p = tmp_path / "config.toml"
    p.write_text(dumps(raw), encoding="utf-8")
    cfg = load(p)
    assert cfg.repos[0].branch == "docs-\U0001f600" and "model \U0001f680 \x7f" in cfg.pricing


def test_a_failure_to_store_an_event_does_not_fail_the_cycle(env, monkeypatch):
    e = env()
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(e.db, "add_event", boom)
    summary = e.run()
    assert summary.get("awaiting_merge") == 1 and len(e.claude.calls) == 1  # the work and its ledger write happened
    assert e.db.repo(REPO)["state"] == "awaiting_merge"


def test_a_disabled_trigger_is_not_reported_as_the_schedule():
    s = scheduler.Schedule(mode="daily", time="09:00")
    xml = scheduler.task_xml(scheduler.Path("p.exe"), scheduler.Path("."), scheduler.Path("h"), scheduler.Path("h/c.toml"), s, 15, "user")
    assert scheduler.parse_task_xml(xml).mode == "daily"
    off = xml.replace("<Enabled>true</Enabled>\n    </CalendarTrigger>", "<Enabled>false</Enabled>\n    </CalendarTrigger>")
    assert off != xml and scheduler.parse_task_xml(off).mode == "manual"


def test_a_repository_at_its_own_limit_does_not_hold_the_shared_queue(env):
    from datetime import datetime, timedelta, timezone

    from readme_monitor.llm import Generator
    from readme_monitor.worker import Worker

    e = env()
    hours_ago = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
    # Waiting longer, but only because of its own monthly cap: shared money cannot help it.
    e.db.update_repo("owner/capped", state="budget_paused", pending_since=hours_ago,
                     state_detail="owner/capped monthly limit $0.01 would be exceeded (spent $0.0100, next request reserves $0.0500)")
    w = Worker(e.cfg, e.db, e.gh, Generator(e.cfg, e.db, "sk-test", client=e.claude), e.home, e.db.start_run("t", "u"))
    w.active = {REPO, "owner/capped"}
    assert w.process(e.cfg.repos[0]) == "awaiting_merge" and len(e.claude.calls) == 1
    # A repository waiting on the SHARED budget still holds the queue (unchanged behavior).
    e.db.update_repo("owner/capped", state_detail="monthly limit $10.00 would be exceeded (spent $9.99, next request reserves $0.05)")
    assert "queued behind owner/capped" in (w.generation_wait(REPO, e.db.repo(REPO), datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                              hours_ago, datetime.now(timezone.utc)) or ("", ""))[1]
