"""Task definitions for every schedule mode, read-back parsing, summaries and next checks across
daylight-saving changes. Task Scheduler itself is replaced by conftest; real registration is
verified separately on a Windows machine."""

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from readme_monitor import cli, scheduler
from readme_monitor.config import ConfigError, Schedule, load
from readme_monitor.scheduler import NS, next_checks, parse_task_xml, summary, task_xml


class NewYork2026(tzinfo):
    """US Eastern rules for 2026 only (DST from 2026-03-08 02:00 to 2026-11-01 02:00 local), used
    when no zone database is installed (Windows without tzdata)."""

    def utcoffset(self, dt):
        summer = datetime(2026, 3, 8, 2) <= dt.replace(tzinfo=None) < datetime(2026, 11, 1, 1 if dt.fold else 2)
        return timedelta(hours=-4 if summer else -5)

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "America/New_York (2026 rules)"


def zone(name):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:  # shortcut: fixed 2026 rules; install tzdata to test the named zone
        return NewYork2026()


def xml_for(s: Schedule, now=datetime(2026, 3, 6, 8, 0)) -> str:
    return task_xml(Path(r"C:\Python\pythonw.exe"), Path(r"C:\src"), Path(r"C:\rt"), Path(r"C:\rt\config.toml"), s, 15,
                    r"WORKSTATION-1\someone", now=now)


@pytest.mark.parametrize("s", [
    Schedule(mode="interval", interval_minutes=15),
    Schedule(mode="interval", interval_minutes=360, timezone="utc"),
    Schedule(mode="daily", time="09:00"),
    Schedule(mode="daily", time="23:45", timezone="utc"),
    Schedule(mode="weekly", time="07:30", weekdays=("mon", "wed", "fri")),
    Schedule(mode="manual"),
])
def test_task_xml_round_trips_through_the_read_back_parser(s):
    xml = xml_for(s)
    root = ET.fromstring(xml.replace('encoding="UTF-16"', ""))
    assert root.find(".//t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    assert root.find(".//t:StartWhenAvailable", NS).text == "true"
    assert root.find(".//t:ExecutionTimeLimit", NS).text == "PT15M"
    assert "pythonw.exe" in root.find(".//t:Command", NS).text
    starts = [e.text for e in root.iter(f"{{{NS['t']}}}StartBoundary")]
    assert all(x.endswith("Z") == (s.timezone == "utc") for x in starts)
    expected = s if s.mode != "manual" else Schedule(mode="manual")
    got = parse_task_xml(xml)
    if s.mode == "interval":  # interval tasks keep the sign-in trigger
        assert root.find(".//t:LogonTrigger", NS) is not None
        assert (got.mode, got.interval_minutes, got.timezone) == (s.mode, s.interval_minutes, s.timezone)
    elif s.mode == "manual":
        assert root.find(".//t:Triggers", NS) is not None and len(root.find(".//t:Triggers", NS)) == 0 and got == expected
    else:
        assert (got.mode, got.time, got.timezone) == (s.mode, s.time, s.timezone)
        if s.mode == "weekly":
            assert got.weekdays == s.weekdays


def test_parse_accepts_what_windows_writes_back():
    xml = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Triggers>
<TimeTrigger><Repetition><Interval>PT1H</Interval></Repetition><StartBoundary>2026-11-10T08:00:00</StartBoundary></TimeTrigger>
<LogonTrigger><UserId>WORKSTATION-1\\someone</UserId></LogonTrigger></Triggers></Task>"""
    assert parse_task_xml(xml) == Schedule(mode="interval", interval_minutes=60)


def test_query_schedule_reads_back_or_reports_no_task(monkeypatch):
    assert scheduler.query_schedule() is None  # conftest: no task registered
    xml = xml_for(Schedule(mode="daily", time="10:00"))
    monkeypatch.setattr(scheduler, "_schtasks", lambda *a: subprocess.CompletedProcess(a, 0, xml, ""))
    assert scheduler.query_schedule() == Schedule(mode="daily", time="10:00")


@pytest.mark.parametrize("s,text", [
    (Schedule(mode="manual"), "Manual only: checks run when you start them"),
    (Schedule(interval_minutes=15), "Every 15 minutes"),
    (Schedule(interval_minutes=60), "Every hour"),
    (Schedule(interval_minutes=360), "Every 6 hours"),
    (Schedule(mode="daily", time="09:00"), "Every day at 09:00 (this computer's time)"),
    (Schedule(mode="weekly", time="09:00"), "Weekdays at 09:00 (this computer's time)"),
    (Schedule(mode="weekly", time="06:00", weekdays=("sat", "sun"), timezone="utc"),
     "Weekends at 06:00 (UTC; unsupported: Windows counts the weekdays in this computer's time, save the schedule again)"),
    (Schedule(mode="weekly", time="07:30", weekdays=("mon", "wed", "fri")), "Mon, Wed and Fri at 07:30 (this computer's time)"),
])
def test_summary(s, text):
    assert summary(s) == text


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_next_checks_follow_the_wall_clock_across_daylight_saving():
    ny = zone("America/New_York")
    localize = lambda naive: naive.replace(tzinfo=ny)  # noqa: E731
    daily = Schedule(mode="daily", time="09:00")
    # spring forward on 2026-03-08: 09:00 is 14:00 UTC before, 13:00 UTC after
    assert next_checks(daily, utc(2026, 3, 7, 15), localize) == [utc(2026, 3, 8, 13), utc(2026, 3, 9, 13), utc(2026, 3, 10, 13)]
    # fall back on 2026-11-01: 13:00 UTC before, 14:00 UTC after
    assert next_checks(daily, utc(2026, 10, 31, 14), localize) == [utc(2026, 11, 1, 14), utc(2026, 11, 2, 14), utc(2026, 11, 3, 14)]
    weekly = Schedule(mode="weekly", time="09:00", weekdays=("mon", "fri"))
    assert next_checks(weekly, utc(2026, 3, 6, 15), localize) == [utc(2026, 3, 9, 13), utc(2026, 3, 13, 13), utc(2026, 3, 16, 13)]
    assert next_checks(weekly, utc(2026, 10, 30, 12), localize) == [utc(2026, 10, 30, 13), utc(2026, 11, 2, 14), utc(2026, 11, 6, 14)]


def test_next_checks_utc_interval_and_manual():
    eastern = zone("America/New_York")
    localize = lambda naive: naive.replace(tzinfo=eastern)  # noqa: E731
    assert next_checks(Schedule(mode="daily", time="09:00", timezone="utc"), utc(2026, 3, 29, 10), localize)[0] == utc(2026, 3, 30, 9)
    assert next_checks(Schedule(interval_minutes=15), utc(2026, 1, 1, 10, 7, 30), localize) == \
        [utc(2026, 1, 1, 10, 22), utc(2026, 1, 1, 10, 37), utc(2026, 1, 1, 10, 52)]
    assert next_checks(Schedule(mode="manual"), utc(2026, 1, 1), localize) == []
    assert all(t.tzinfo == timezone.utc for t in next_checks(Schedule(mode="daily"), utc(2026, 1, 1)))  # the computer's own zone


def test_install_task_uses_the_configured_schedule_and_interval_override(tmp_path, capsys):
    (tmp_path / "config.toml").write_text('[schedule]\nmode = "daily"\ntime = "06:30"\n', encoding="utf-8")
    assert cli.main(["install-task", "--dry-run", "--home", str(tmp_path)]) == 0
    assert parse_task_xml(capsys.readouterr().out.split("\n", 1)[1]) == Schedule(mode="daily", time="06:30")
    assert cli.main(["install-task", "--dry-run", "--interval", "30", "--home", str(tmp_path)]) == 0
    assert parse_task_xml(capsys.readouterr().out.split("\n", 1)[1]).interval_minutes == 30
    assert cli.main(["schedule", "--json", "--home", str(tmp_path)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"] == "Every day at 06:30 (this computer's time)" and len(data["next_checks"]) == 3
    assert data["effective"] is None and data["in_sync"] is False


def test_read_back_understands_the_offset_form_windows_returns_for_a_utc_boundary():
    # Seen on a real Task Scheduler: a boundary registered as 06:30Z is handed back as 01:30-05:00.
    s = scheduler.Schedule(mode="daily", time="06:30", timezone="utc")
    xml = scheduler.task_xml(Path("p.exe"), Path("."), Path("h"), Path("h/config.toml"), s, 15, "user")
    back = re.sub(r"<StartBoundary>(\d{4}-\d\d-\d\d)T06:30:00Z</StartBoundary>", r"<StartBoundary>\1T01:30:00-05:00</StartBoundary>", xml)
    assert back != xml and scheduler.parse_task_xml(back) == scheduler.parse_task_xml(xml)
    assert scheduler.summary(scheduler.parse_task_xml(back)) == scheduler.summary(s)


def test_weekly_schedule_in_utc_is_refused_because_windows_counts_weekdays_locally(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[schedule]\nmode = "weekly"\nweekdays = ["wed"]\ntime = "02:00"\ntimezone = "utc"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="this computer's time"):
        load(p)


def test_weekly_offset_read_back_is_recognised_and_never_planned():
    # A weekly task registered elsewhere at 06:00Z comes back from Windows in the offset form.
    local = Schedule(mode="weekly", time="06:00", weekdays=("sat", "sun"))
    back = xml_for(local).replace("T06:00:00</StartBoundary>", "T01:00:00-05:00</StartBoundary>")
    assert back != xml_for(local)
    got = parse_task_xml(back)
    assert (got.mode, got.time, got.timezone, got.weekdays) == ("weekly", "06:00", "utc", ("sat", "sun"))
    assert next_checks(got, utc(2026, 3, 6, 12)) == [] and "unsupported" in summary(got)
