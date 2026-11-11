"""Installation activation: live work runs only in a runtime folder deliberately enabled on this computer.

Anything that can spend money, send repository content to Claude (including free token
counting), write to GitHub or schedule work calls `require_active(home, host_lock)` first. The
check uses the machine's real identity, not environment variables:

- the machine identity the operating system keeps (Windows MachineGuid in HKLM, the macOS
  IOPlatformUUID, the Linux /etc/machine-id; changing any of them needs administrator rights),
  so a folder copied to another computer stays inactive there. It stays in the local activation
  record and is never logged;
- the activation record <home>/activation.json, written by `activate`, which names the folder,
  so a moved or copied folder on the same computer stays inactive too;
- optionally [installation] host_lock, which pins the activation to one computer name.

The host name is informational: renaming the computer keeps the installation active. There is
deliberately no override switch. Offline tests replace `require_active` with a fixture.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

WINDOWS = os.name == "nt"
SUPPORTED = sys.platform == "win32" or sys.platform == "darwin" or sys.platform.startswith("linux")
ACTIVATION_FILE = "activation.json"
LEGACY_FILE = "production-host.json"  # written by bind-host before 0.3.0
POINTER_NAME = ".readme-monitor.json"
ENABLE_HINT = 'use "Enable the worker on this computer" in the setup interface or run `readme-monitor activate`'


class NotActivated(RuntimeError):
    pass


HostNotAuthorized = NotActivated  # earlier name; the CLI maps both to exit code 3


def hostname() -> str:
    return socket.gethostname().split(".")[0].upper()


def machine_guid() -> str | None:
    """This computer's identity as the operating system records it, or None when it cannot be read.
    (The name is historical: the activation.json field predates macOS and Linux support.)"""
    if WINDOWS:
        import winreg

        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                 winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
            with key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0]).lower()
        except OSError:
            return None
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True,
                                 timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{36})"', out)
        return m.group(1).lower() if m else None
    if sys.platform.startswith("linux"):
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                value = Path(p).read_text(encoding="ascii").strip().lower()
            except (OSError, UnicodeDecodeError):
                continue
            if re.fullmatch(r"[0-9a-f]{32}", value):
                return value
    return None


def pointer_path() -> Path:
    """Names the one active runtime folder of this operating-system account."""
    return Path.home() / POINTER_NAME


def _same(a, b) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, TypeError, ValueError):
        return False


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict):
    """Atomic: a crash leaves either the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")  # a leftover from an interrupted write is simply overwritten
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def activation(home: Path) -> dict | None:
    return _read(home / ACTIVATION_FILE)


def activation_problem(home: Path | None, host_lock: str | None = None) -> str | None:
    """None when live work may run with this runtime folder, else one plain sentence saying why not."""
    if home is None:
        return "No runtime folder is selected."
    if not SUPPORTED:
        return "The worker can only be enabled on Windows, macOS or Linux; only offline commands and dry runs work on this system."
    rec = activation(home)
    if rec is None:
        if (home / ACTIVATION_FILE).exists():
            return f"The activation record in {home} cannot be read; {ENABLE_HINT} to enable the worker again."
        if (home / LEGACY_FILE).exists():
            return (f"{home} has an activation record from an earlier version that does not match this computer and folder; "
                    f"it stays inactive until you {ENABLE_HINT} here deliberately.")
        return f"The worker is not enabled for {home} yet; {ENABLE_HINT}."
    guid = machine_guid()
    if not guid:
        return "The operating system's machine identity could not be read, so this computer cannot be identified; the worker stays inactive."
    if rec.get("machine_guid") != guid:
        return ("This runtime folder was activated on another computer or copied from one; it stays inactive until you "
                f"{ENABLE_HINT} here deliberately.")
    if not _same(rec.get("home", ""), home):
        return (f"This runtime folder was activated at {rec.get('home')} and has been moved or copied; it stays inactive until "
                f"you {ENABLE_HINT} here deliberately.")
    if host_lock and host_lock.upper() != hostname().upper():
        return f"config.toml [installation] host_lock allows only the computer {host_lock}; this computer is {hostname()}."
    return None


def require_active(home: Path | None, host_lock: str | None = None) -> None:
    problem = activation_problem(home, host_lock)
    if problem:
        raise NotActivated(f"Live ReadmeMonitor work (Claude requests, spending, GitHub writes, scheduling) is refused: "
                           f"{problem} Offline commands and dry runs still work.")


def _pointed_home() -> Path | None:
    p = _read(pointer_path())
    return Path(p["home"]) if p and isinstance(p.get("home"), str) else None


def activate(home: Path, account: str, host_lock: str | None = None) -> dict:
    """Enable live work for this runtime folder on this computer (deliberate, per folder).
    A mismatched or copied record is archived, never deleted; the ledger is kept as it is."""
    home = home.resolve()
    if not SUPPORTED:
        raise NotActivated("Activation refused: the worker can only be enabled on Windows, macOS or Linux.")
    guid = machine_guid()
    if not guid:
        raise NotActivated("Activation refused: the operating system's machine identity could not be read.")
    if host_lock and host_lock.upper() != hostname().upper():
        raise NotActivated(f"Activation refused: [installation] host_lock allows only {host_lock}; this computer is {hostname()}.")
    other = _pointed_home()
    if other is not None and not _same(other, home) and activation_problem(other) is None:
        raise NotActivated(f"Activation refused: this account already has an active runtime folder, {other}. "
                           f'Deactivate that one first (readme-monitor deactivate --home "{other}").')
    if activation_problem(home, host_lock) is None:
        data = activation(home)
    else:
        if (home / ACTIVATION_FILE).exists():
            os.replace(home / ACTIVATION_FILE, home / f"activation.previous-{_stamp()}.json")
        data = {"version": 2, "installation_id": str(uuid.uuid4()), "host": hostname(), "machine_guid": guid,
                "home": str(home), "account": account, "activated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        _write(home / ACTIVATION_FILE, data)
    _write(pointer_path(), {"home": str(home)})
    return data


def deactivate(home: Path) -> dict:
    """Remove the scheduled task (if any) and revoke the activation. All data stays."""
    from . import scheduler

    removed = False
    if scheduler.query() is not None:
        scheduler.uninstall()
        removed = True
    revoked = None
    if (home / ACTIVATION_FILE).exists():
        revoked = home / f"activation.revoked-{_stamp()}.json"
        os.replace(home / ACTIVATION_FILE, revoked)
    other = _pointed_home()
    if other is not None and _same(other, home):
        pointer_path().unlink(missing_ok=True)
    return {"task_removed": removed, "revoked": str(revoked) if revoked else None}


MAINTENANCE_FILE = "maintenance.json"
MAINTENANCE_HOURS = 6  # a flag left by an update that never finished stops blocking checks after this


def maintenance(home: Path) -> dict | None:
    """The application update in progress for this runtime folder, or None. Every worker entry point
    checks it while holding worker.lock, so a scheduled check never runs on half-installed files. A
    stale flag (the update was abandoned) is ignored, never deleted: `end_maintenance` clears it."""
    rec = _read(home / MAINTENANCE_FILE)
    if not rec:
        return None
    try:
        age = time.time() - float(rec.get("started", 0))
    except (TypeError, ValueError):
        age = MAINTENANCE_HOURS * 3600
    return rec if 0 <= age < MAINTENANCE_HOURS * 3600 else None


def begin_maintenance(home: Path, from_version: str, to_version: str, backup: str | None) -> dict:
    """Callers hold worker.lock, so no check is running when the flag appears."""
    rec = {"started": time.time(), "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "from_version": from_version, "to_version": to_version, "backup": backup}
    _write(home / MAINTENANCE_FILE, rec)
    return rec


def end_maintenance(home: Path) -> dict | None:
    """Clear the flag (kept as maintenance.finished-<stamp>.json for the record). Pause and schedule are not touched."""
    path = home / MAINTENANCE_FILE
    rec = _read(path)
    if path.exists():
        os.replace(path, home / f"maintenance.finished-{_stamp()}.json")
    return rec


def migrate_legacy(home: Path) -> str:
    """Carry an activation from production-host.json (bind-host, before 0.3.0) forward, only on a
    full match of machine, folder and host name. Idempotent and safe to rerun; a mismatch leaves
    the folder inactive (activation_problem explains why) and never raises.
    Returns: present | none | migrated | mismatch | conflict."""
    if (home / ACTIVATION_FILE).exists():
        return "present"
    if any(home.glob("activation.*.json")):
        return "none"  # revoked or archived on purpose since: never bring the old record back
    legacy = _read(home / LEGACY_FILE)
    if legacy is None:
        return "none"
    guid = machine_guid()
    if not (SUPPORTED and guid and legacy.get("machine_guid") == guid and _same(legacy.get("home", ""), home)
            and str(legacy.get("host", "")).upper() == hostname()):
        return "mismatch"
    other = _pointed_home()
    if other is not None and not _same(other, home) and activation_problem(other) is None:
        return "conflict"
    data = {"version": 2, "installation_id": str(uuid.uuid4()), "host": hostname(), "machine_guid": guid,
            "home": str(home.resolve()), "account": legacy.get("account", ""),
            "activated_at": legacy.get("bound_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "migrated_from": LEGACY_FILE}
    _write(home / ACTIVATION_FILE, data)
    _write(pointer_path(), {"home": str(home.resolve())})
    return "migrated"
