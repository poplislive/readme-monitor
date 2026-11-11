"""Linux: scheduled checks as a systemd user service and timer in ~/.config/systemd/user.

Behaviour (systemd.timer(5), systemd.service(5)): the oneshot service is never started twice at
once; calendar timers use Persistent=true, so starts missed while the computer was off or asleep
become one catch-up start; interval timers count monotonic time, which pauses during suspend, so
nothing piles up. User timers run only while the user's systemd instance runs (normally while
signed in); ReadmeMonitor never enables lingering. TimeoutStartSec bounds a check.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import WEEKDAYS, Schedule

NAME = "systemd user timer (Linux)"
UNIT = "readme-monitor-sync"
DAYS = dict(zip(WEEKDAYS, ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")))


def unit_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / "systemd" / "user"


def _escape(s: str) -> str:
    return s.replace("%", "%%")  # unit-file specifiers


def _quote(arg: str) -> str:
    return '"' + _escape(arg).replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'


def service_text(argv: list[str], workdir: Path, time_limit_minutes: int) -> str:
    return (f"[Unit]\nDescription=ReadmeMonitor: one README synchronization cycle, then exit\n\n"
            f"[Service]\nType=oneshot\nExecStart={' '.join(_quote(a) for a in argv)}\n"
            f"WorkingDirectory={_escape(str(workdir))}\nTimeoutStartSec={time_limit_minutes}min\nNice=10\n")


def timer_text(schedule: Schedule) -> str | None:
    if schedule.mode == "manual":
        return None
    if schedule.mode == "interval":
        when = f"OnActiveSec=2min\nOnUnitActiveSec={schedule.interval_minutes}min\n"
    else:
        days = "" if schedule.mode == "daily" else ",".join(DAYS[w] for w in schedule.weekdays) + " "
        tz = " UTC" if schedule.timezone == "utc" else ""
        when = f"OnCalendar={days}*-*-* {schedule.time}:00{tz}\nPersistent=true\n"
    return f"[Unit]\nDescription=ReadmeMonitor scheduled checks\n\n[Timer]\n{when}AccuracySec=1min\n\n[Install]\nWantedBy=timers.target\n"


def parse_timer(text: str) -> Schedule:
    m = re.search(r"^OnUnitActiveSec=(\d+)min\s*$", text, re.M)
    if m:
        return Schedule(mode="interval", interval_minutes=int(m.group(1)))
    m = re.search(r"^OnCalendar=(?:([A-Za-z,]+) )?\*-\*-\* (\d\d:\d\d):00( UTC)?\s*$", text, re.M)
    if not m:
        return Schedule(mode="manual")
    tz = "utc" if m.group(3) else "local"
    if not m.group(1):
        return Schedule(mode="daily", time=m.group(2), timezone=tz)
    names = {v: k for k, v in DAYS.items()}
    days = {names[d] for d in m.group(1).split(",") if d in names}
    return Schedule(mode="weekly", time=m.group(2), weekdays=tuple(w for w in WEEKDAYS if w in days), timezone=tz)


def definition(schedule: Schedule, argv: list[str], workdir: Path, time_limit_minutes: int) -> str:
    timer = timer_text(schedule)
    return (f"# {unit_dir() / (UNIT + '.service')}\n{service_text(argv, workdir, time_limit_minutes)}"
            + (f"\n# {unit_dir() / (UNIT + '.timer')}\n{timer}" if timer else "\n# no timer: manual checks only\n"))


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    if not shutil.which("systemctl"):
        raise RuntimeError("systemd is not available on this computer, so checks cannot be scheduled; use Run now")
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)


def _ok(*args: str) -> str:
    r = _systemctl(*args)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip()
        if "bus" in detail.lower() or "XDG_RUNTIME_DIR" in detail:
            raise RuntimeError("the systemd user session is not reachable from here (sign in to a desktop session); " + detail)
        raise RuntimeError(f"systemctl --user {' '.join(args)} failed: {detail}")
    return r.stdout


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def install(schedule: Schedule, argv: list[str], workdir: Path, time_limit_minutes: int) -> str:
    """Rewrites the units; a running check continues (systemd keeps the active service as it is)."""
    timer = timer_text(schedule)
    _write(unit_dir() / f"{UNIT}.service", service_text(argv, workdir, time_limit_minutes))
    timer_path = unit_dir() / f"{UNIT}.timer"
    if timer is None:
        _systemctl("disable", "--now", f"{UNIT}.timer")  # absent is fine
        timer_path.unlink(missing_ok=True)
        _ok("daemon-reload")
        return f"Registered {UNIT}.service (manual checks only)."
    _write(timer_path, timer)
    _ok("daemon-reload")
    _ok("enable", f"{UNIT}.timer")
    _ok("restart", f"{UNIT}.timer")  # starts the timer only, never the check itself
    return f"Registered {UNIT}.timer with the systemd user manager."


def uninstall() -> str:
    _systemctl("disable", "--now", f"{UNIT}.timer")  # stops the timer; a running check finishes
    for suffix in (".timer", ".service"):
        (unit_dir() / f"{UNIT}{suffix}").unlink(missing_ok=True)
    _systemctl("daemon-reload")
    return f"Removed {UNIT}."


def _show() -> dict[str, dict[str, str]]:
    r = _systemctl("show", f"{UNIT}.service", f"{UNIT}.timer", "-p", "Id,LoadState,ActiveState,ExecMainStatus", "--no-pager")
    units: dict[str, dict[str, str]] = {}
    for block in r.stdout.strip().split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in props:
            units[props["Id"]] = props
    return units


def query() -> dict | None:
    if not (unit_dir() / f"{UNIT}.service").exists():
        return None
    try:
        units = _show()
    except RuntimeError:
        return None
    svc, tmr = units.get(f"{UNIT}.service", {}), units.get(f"{UNIT}.timer", {})
    if svc.get("LoadState") != "loaded":
        return None
    running = svc.get("ActiveState") in ("activating", "active")
    has_timer = (unit_dir() / f"{UNIT}.timer").exists()
    status = "Running" if running else ("Ready" if not has_timer or tmr.get("ActiveState") == "active" else "Disabled")
    return {"Status": status, "Last Result": svc.get("ExecMainStatus"), "Timer": tmr.get("ActiveState")}


def query_schedule() -> Schedule | None:
    """What systemd holds: the service must be loaded and, for a timed schedule, the timer active."""
    t = query()
    if t is None:
        return None
    timer_path = unit_dir() / f"{UNIT}.timer"
    if not timer_path.exists():
        return Schedule(mode="manual")
    if t.get("Timer") != "active":  # unit files alone are not a schedule
        return None
    try:
        return parse_timer(timer_path.read_text(encoding="utf-8"))
    except OSError:
        return None


def registered_program() -> str | None:
    try:
        text = (unit_dir() / f"{UNIT}.service").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'^ExecStart="((?:[^"\\]|\\.)*)"', text, re.M)
    return re.sub(r"\\(.)", r"\1", m.group(1)).replace("%%", "%") if m else None


def run_now() -> str:
    _ok("start", "--no-block", f"{UNIT}.service")  # an active oneshot is not started again
    return "Started."
