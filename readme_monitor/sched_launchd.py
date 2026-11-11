"""macOS: scheduled checks as a launchd user agent in ~/Library/LaunchAgents.

Behaviour (launchd.plist(5)): a job is never started twice at once; starts missed while the Mac
slept are coalesced into one start on wake; nothing runs while the Mac is off or the user is
logged out. launchd has no execution time limit, so the worker's own run deadline
(run_timeout_seconds) bounds a check. Calendar starts follow this computer's time only, so a UTC
daily schedule is refused rather than registered at the wrong hour.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from pathlib import Path

from .config import WEEKDAYS, Schedule

NAME = "launchd (macOS)"
LABEL = "io.github.poplislive.readme-monitor.sync"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def plist(schedule: Schedule, argv: list[str], workdir: Path) -> dict:
    from .scheduler import Unsupported

    d = {"Label": LABEL, "ProgramArguments": list(argv), "WorkingDirectory": str(workdir), "ProcessType": "Background",
         "RunAtLoad": False, "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
    if schedule.mode == "interval":
        d["StartInterval"] = schedule.interval_minutes * 60
    elif schedule.mode in ("daily", "weekly"):
        if schedule.timezone == "utc":
            raise Unsupported("launchd starts calendar schedules in this computer's time; choose this computer's time "
                              "for the schedule (nothing was registered)")
        h, m = map(int, schedule.time.split(":"))
        if schedule.mode == "daily":
            d["StartCalendarInterval"] = {"Hour": h, "Minute": m}
        else:  # launchd weekdays: 1 = Monday ... 7 = Sunday
            d["StartCalendarInterval"] = [{"Weekday": WEEKDAYS.index(w) + 1, "Hour": h, "Minute": m} for w in schedule.weekdays]
    return d


def parse(d: dict) -> Schedule:
    if "StartInterval" in d:
        return Schedule(mode="interval", interval_minutes=max(1, int(d["StartInterval"]) // 60))
    cal = d.get("StartCalendarInterval")
    if not cal:
        return Schedule(mode="manual")
    items = cal if isinstance(cal, list) else [cal]
    time = f"{int(items[0].get('Hour', 0)):02d}:{int(items[0].get('Minute', 0)):02d}"
    days = {WEEKDAYS[(int(i["Weekday"]) - 1) % 7] for i in items if "Weekday" in i}  # 0 and 7 both mean Sunday
    if not days:
        return Schedule(mode="daily", time=time)
    return Schedule(mode="weekly", time=time, weekdays=tuple(w for w in WEEKDAYS if w in days))


def definition(schedule: Schedule, argv: list[str], workdir: Path, time_limit_minutes: int) -> str:
    return plistlib.dumps(plist(schedule, argv, workdir)).decode("utf-8")


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=60)


def _running() -> bool:
    r = _launchctl("print", _target())
    return r.returncode == 0 and re.search(r"^\s*state = running\s*$", r.stdout, re.M) is not None


def install(schedule: Schedule, argv: list[str], workdir: Path, time_limit_minutes: int) -> str:
    data = plistlib.dumps(plist(schedule, argv, workdir))
    if _running():  # reloading the agent would stop the check mid-way
        raise RuntimeError("a check is running right now; the schedule is saved, apply it again when the check finishes")
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    _launchctl("bootout", _target())  # not loaded yet is fine
    r = _launchctl("bootstrap", f"gui/{os.getuid()}", str(p))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {(r.stderr or r.stdout).strip()}")
    return f"Registered {LABEL} with launchd ({p})."


def uninstall() -> str:
    if _running():
        raise RuntimeError("a check is running right now; remove the schedule when it finishes")
    _launchctl("bootout", _target())
    plist_path().unlink(missing_ok=True)
    return f"Removed {LABEL}."


def query() -> dict | None:
    r = _launchctl("print", _target())
    if r.returncode != 0:
        return None
    state = re.search(r"^\s*state = (\S+)", r.stdout, re.M)
    last = re.search(r"^\s*last exit code = (.+)$", r.stdout, re.M)
    return {"Status": "Running" if state and state.group(1) == "running" else "Ready",
            "Last Result": last.group(1).strip() if last else None}


def query_schedule() -> Schedule | None:
    """What launchd holds: the job must be loaded, and its schedule is read from the loaded plist."""
    if query() is None:
        return None
    try:
        return parse(plistlib.loads(plist_path().read_bytes()))
    except (OSError, plistlib.InvalidFileException, ValueError, KeyError, TypeError):
        return None


def registered_program() -> str | None:
    if query() is None:
        return None
    try:
        return (plistlib.loads(plist_path().read_bytes()).get("ProgramArguments") or [None])[0]
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def run_now() -> str:
    r = _launchctl("kickstart", _target())  # without -k a running check is left alone, never doubled
    if r.returncode != 0:
        raise RuntimeError(f"launchctl kickstart failed: {(r.stderr or r.stdout).strip()}")
    return "Started."
