"""Installation activation: live work runs only in a runtime folder deliberately enabled on this computer.

Anything that can spend money, send repository content to Claude (including free token
counting), write to GitHub or schedule work calls `require_active(home, host_lock)` first. The
check uses the machine's real identity, not environment variables:

- the Windows MachineGuid in HKLM (changing it needs administrator rights), so a folder copied
  to another computer stays inactive there;
- the activation record <home>/activation.json, written by `activate`, which names the folder,
  so a moved or copied folder on the same computer stays inactive too;
- optionally [installation] host_lock, which pins the activation to one computer name.

The host name is informational: renaming the computer keeps the installation active. There is
deliberately no override switch. Offline tests replace `require_active` with a fixture.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path

WINDOWS = os.name == "nt"
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
    if not WINDOWS:
        return None
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                             winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        with key:
            return str(winreg.QueryValueEx(key, "MachineGuid")[0]).lower()
    except OSError:
        return None


def pointer_path() -> Path:
    """Names the one active runtime folder of this Windows account."""
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
    if not WINDOWS:
        return "The worker can only be enabled on Windows; this computer runs another system, so only offline commands and dry runs work here."
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
        return "The Windows MachineGuid could not be read, so this computer cannot be identified; the worker stays inactive."
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
    if not WINDOWS:
        raise NotActivated("Activation refused: the worker can only be enabled on Windows.")
    guid = machine_guid()
    if not guid:
        raise NotActivated("Activation refused: the Windows MachineGuid could not be read.")
    if host_lock and host_lock.upper() != hostname().upper():
        raise NotActivated(f"Activation refused: [installation] host_lock allows only {host_lock}; this computer is {hostname()}.")
    other = _pointed_home()
    if other is not None and not _same(other, home) and activation_problem(other) is None:
        raise NotActivated(f"Activation refused: this Windows account already has an active runtime folder, {other}. "
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
    if not (WINDOWS and guid and legacy.get("machine_guid") == guid and _same(legacy.get("home", ""), home)
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
