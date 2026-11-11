"""Scheduled checks through the operating system's own scheduler: Windows Task Scheduler here,
launchd user agents (sched_launchd) on macOS and systemd user timers (sched_systemd) on Linux.
The system scheduler is the only scheduling authority: the worker never sleeps or loops itself, so
a missed or overlapping start is the scheduler's policy, set below to skip overlaps and to catch up
once, never in a burst. The public functions (register, definition, uninstall, query,
query_schedule, run_now) dispatch to the platform's backend."""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from . import launcher
from .config import WEEKDAYS, Schedule

TASK_NAME = r"\ReadmeMonitor\Sync"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
DAY_NAMES = dict(zip(WEEKDAYS, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")))


class Unsupported(RuntimeError):
    """The system scheduler cannot carry this schedule faithfully; nothing was registered."""


def _backend():
    if sys.platform == "darwin":
        from . import sched_launchd as b
    elif sys.platform.startswith("linux"):
        from . import sched_systemd as b
    else:
        return None
    return b


def name() -> str:
    b = _backend()
    return b.NAME if b else "Windows Task Scheduler" if os.name == "nt" else "no supported scheduler"


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN") if os.name == "nt" else None
    user = getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def scheduled_argv(home: Path, config: Path) -> list[str]:
    """The command every scheduler starts: one scheduled cycle of the installed engine, then exit."""
    return [*launcher.engine(windowless=True), "run-once", "--trigger", "scheduled", "--home", str(home), "--config", str(config)]


def _stable():
    why = launcher.unstable_reason()
    if why:
        raise Unsupported(f"Scheduled checks are not available: {why}.")


def definition(schedule: Schedule, home: Path, config: Path, time_limit_minutes: int, now: datetime | None = None) -> str:
    """The text the scheduler would register (install-task --dry-run)."""
    b = _backend()
    if b:
        return b.definition(schedule, scheduled_argv(home, config), launcher.workdir(), time_limit_minutes)
    return task_xml(launcher.engine(windowless=True), launcher.workdir(), home, config, schedule, time_limit_minutes, current_user(), now)


def registered_program() -> str | None:
    """The program the registered scheduled check starts, or None when nothing is registered."""
    if b := _backend():
        return b.registered_program()
    try:
        r = _schtasks("/Query", "/TN", TASK_NAME, "/XML")
        if r.returncode != 0 or not r.stdout.strip():
            return None
        root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", r.stdout))
    except (RuntimeError, ET.ParseError):
        return None
    cmd = root.findtext("t:Actions/t:Exec/t:Command", None, NS)
    return cmd.strip().strip('"') if cmd else None


def program_is_current(registered: str | None) -> bool | None:
    """Whether a registered check starts this copy's engine (None: nothing registered). A check left
    behind by another copy (an earlier source install, a moved application) is reported, not hidden."""
    if not registered:
        return None
    want = launcher.engine(windowless=True)[0]
    try:
        return os.path.normcase(str(Path(registered).resolve())) == os.path.normcase(str(Path(want).resolve()))
    except (OSError, ValueError):
        return False


def register(schedule: Schedule, home: Path, config: Path, time_limit_minutes: int) -> str:
    """Register (or replace) the scheduled check. Replacing it neither starts nor stops a check."""
    _stable()
    b = _backend()
    if b:
        return b.install(schedule, scheduled_argv(home, config), launcher.workdir(), time_limit_minutes)
    return install(definition(schedule, home, config, time_limit_minutes), home / "task" / "ReadmeMonitor-Sync.xml")


def _start(schedule: Schedule, now: datetime | None) -> str:
    """StartBoundary: wall clock without offset for local (follows daylight saving), Z for UTC."""
    utc = schedule.timezone == "utc"
    now = now or (datetime.now(timezone.utc).replace(tzinfo=None) if utc else datetime.now())
    if schedule.mode in ("daily", "weekly"):
        h, m = map(int, schedule.time.split(":"))
        now = now.replace(hour=h, minute=m)
    return now.replace(second=0, microsecond=0).isoformat() + ("Z" if utc else "")


def _triggers(schedule: Schedule, user: str, now: datetime | None) -> str:
    start = f"<StartBoundary>{_start(schedule, now)}</StartBoundary>"
    if schedule.mode == "manual":
        return "<Triggers />"  # the task exists only to be started on demand
    if schedule.mode == "interval":
        return f"""<Triggers>
    <TimeTrigger>
      {start}
      <Repetition><Interval>PT{schedule.interval_minutes}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
    <LogonTrigger>
      <UserId>{escape(user)}</UserId>
      <Delay>PT2M</Delay>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>"""
    if schedule.mode == "daily":
        when = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    else:
        days = "".join(f"<{DAY_NAMES[d]} />" for d in schedule.weekdays)
        when = f"<ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
    return f"""<Triggers>
    <CalendarTrigger>
      {start}
      {when}
      <Enabled>true</Enabled>
    </CalendarTrigger>
  </Triggers>"""


def task_xml(engine: list[str], workdir: Path, home: Path, config: Path, schedule: Schedule, time_limit_minutes: int, user: str,
             now: datetime | None = None) -> str:
    """The task definition. `engine` is launcher.engine(): the interpreter and `-m readme_monitor`
    in a checkout, or the installed engine executable. Registering it again (a schedule change) only
    replaces the definition: it neither starts a check nor stops one already running; IgnoreNew means
    a start that falls inside a running check is skipped, and StartWhenAvailable gives one catch-up
    start after the computer was off or asleep, never a burst."""
    exe, *pre = engine
    args = subprocess.list2cmdline([*pre, "run-once", "--trigger", "scheduled", "--home", str(home), "--config", str(config)])
    python_exe = exe
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>ReadmeMonitor: one README synchronization cycle, then exit.</Description>
    <URI>{escape(TASK_NAME)}</URI>
  </RegistrationInfo>
  {_triggers(schedule, user, now)}
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT{time_limit_minutes}M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python_exe))}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _minutes(duration: str) -> int | None:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?)?", duration or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi = (int(x or 0) for x in m.groups())
    return d * 1440 + h * 60 + mi


def _boundary(start: str) -> tuple[str, str]:
    """(HH:MM, timezone) of a StartBoundary. Windows hands a boundary registered with Z back as local
    time with an offset (seen on a real Task Scheduler), so any offset means a fixed UTC instant."""
    try:
        dt = datetime.fromisoformat(start)
    except ValueError:
        return (start[11:16] if len(start) >= 16 else "00:00"), "local"
    if dt.tzinfo is None:
        return dt.strftime("%H:%M"), "local"
    return dt.astimezone(timezone.utc).strftime("%H:%M"), "utc"


def parse_task_xml(xml: str) -> Schedule:
    """The schedule a registered task definition actually carries (schtasks /Query /XML)."""
    root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", xml))
    trig = root.find("t:Triggers", NS)

    def enabled(tag):  # a trigger someone switched off in Task Scheduler fires nothing
        return next((t for t in (trig.findall(tag, NS) if trig is not None else ())
                     if t.findtext("t:Enabled", "true", NS).strip().lower() != "false"), None)

    time_t, cal = enabled("t:TimeTrigger"), enabled("t:CalendarTrigger")
    if time_t is not None:
        start = time_t.findtext("t:StartBoundary", "", NS)
        minutes = _minutes(time_t.findtext("t:Repetition/t:Interval", "", NS))
        if minutes:
            return Schedule(mode="interval", interval_minutes=minutes, timezone=_boundary(start)[1])
    if cal is not None:
        start = cal.findtext("t:StartBoundary", "", NS)
        clock, tz = _boundary(start)
        week = cal.find("t:ScheduleByWeek/t:DaysOfWeek", NS)
        if week is not None:
            names = {v: k for k, v in DAY_NAMES.items()}
            days = {names[c.tag.split("}")[-1]] for c in week if c.tag.split("}")[-1] in names}
            return Schedule(mode="weekly", time=clock, weekdays=tuple(d for d in WEEKDAYS if d in days), timezone=tz)
        return Schedule(mode="daily", time=clock, timezone=tz)
    return Schedule(mode="manual")


def summary(schedule: Schedule) -> str:
    if schedule.mode == "manual":
        return "Manual only: checks run when you start them"
    if schedule.mode == "interval":
        n = schedule.interval_minutes
        if n == 1:
            return "Every minute"
        if n % 60 == 0:
            return "Every hour" if n == 60 else f"Every {n // 60} hours"
        return f"Every {n} minutes"
    where = "UTC" if schedule.timezone == "utc" else "this computer's time"
    if schedule.mode == "weekly" and schedule.timezone == "utc":
        # Only a task registered outside ReadmeMonitor can carry this (config refuses it).
        where = "UTC; unsupported: the scheduler counts the weekdays in this computer's time, save the schedule again"
    days = set(schedule.weekdays) if schedule.mode == "weekly" else set(WEEKDAYS)
    if days == set(WEEKDAYS):
        label = "Every day"
    elif days == set(WEEKDAYS[:5]):
        label = "Weekdays"
    elif days == set(WEEKDAYS[5:]):
        label = "Weekends"
    else:
        names = [d.capitalize() for d in WEEKDAYS if d in days]
        label = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"{label} at {schedule.time} ({where})"


def local_localize(naive: datetime) -> datetime:
    """A naive wall-clock time on this computer, made aware with the operating system's zone rules."""
    return naive.astimezone()


def next_checks(schedule: Schedule, now: datetime, localize=local_localize, count: int = 3) -> list[datetime]:
    """The next `count` scheduled starts as aware UTC datetimes. `localize` turns a naive local
    wall-clock datetime into an aware one (tests pass zoneinfo zones). Interval starts are
    estimated from `now`; Task Scheduler's own next run time is more exact when it reports one."""
    now = now.astimezone(timezone.utc)
    if schedule.mode == "manual":
        return []
    if schedule.mode == "weekly" and schedule.timezone == "utc":
        return []  # config refuses it: Windows counts weekdays locally, so any planned day could be wrong
    if schedule.mode == "interval":
        step = timedelta(minutes=schedule.interval_minutes)
        first = now.replace(second=0, microsecond=0) + step
        return [first + step * i for i in range(count)]
    h, m = map(int, schedule.time.split(":"))
    days = set(schedule.weekdays) if schedule.mode == "weekly" else set(WEEKDAYS)
    out: list[datetime] = []
    day = now.date() - timedelta(days=1)  # local dates can lag UTC by up to a day
    while len(out) < count:
        if WEEKDAYS[day.weekday()] in days:
            wall = datetime(day.year, day.month, day.day, h, m)
            at = wall.replace(tzinfo=timezone.utc) if schedule.timezone == "utc" else localize(wall).astimezone(timezone.utc)
            if at > now:
                out.append(at)
        day += timedelta(days=1)
    return out


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    if os.name != "nt":
        raise RuntimeError("Task Scheduler is only available on Windows")
    return subprocess.run(["schtasks.exe", *args], capture_output=True, text=True, timeout=60)


def install(xml: str, xml_path: Path) -> str:
    """Registers (or replaces, /F) the task definition. Replacing it does not start or stop a run."""
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(xml, encoding="utf-16")
    r = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()


def uninstall() -> str:
    if b := _backend():
        return b.uninstall()
    r = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()


def query() -> dict | None:
    """Selected fields from `schtasks /Query /V` (the same keys from the other backends), or None
    when no scheduled check is registered."""
    if b := _backend():
        return b.query()
    if os.name != "nt":
        return None
    r = _schtasks("/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST")
    if r.returncode != 0:
        return None
    wanted = {"Status", "Next Run Time", "Last Run Time", "Last Result", "Run As User", "Task To Run", "Logon Mode"}
    out = {}
    for line in r.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in wanted and key.strip() not in out:
            out[key.strip()] = value.strip()
    return out


def query_schedule() -> Schedule | None:
    """The schedule the system scheduler actually holds, or None when nothing is registered."""
    if b := _backend():
        return b.query_schedule()
    try:
        r = _schtasks("/Query", "/TN", TASK_NAME, "/XML")
    except RuntimeError:  # not Windows
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return parse_task_xml(r.stdout)
    except ET.ParseError:
        return None


def run_now() -> str:
    if b := _backend():
        return b.run_now()
    r = _schtasks("/Run", "/TN", TASK_NAME)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()
