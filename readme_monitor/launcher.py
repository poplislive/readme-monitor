"""How to start the engine again: the one place that knows the command line of `readme_monitor`.

The setup interface's detached runs, the desktop shell's helper and every operating-system
scheduler use `engine()`. In a source checkout that is this interpreter with `-m readme_monitor`;
in an installed desktop build (frozen by PyInstaller as a folder) it is the engine executable in
the installation folder, which an upgrade replaces in place, so a registered schedule keeps working.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))


def engine(windowless: bool = False) -> list[str]:
    """The argv prefix that runs one `readme-monitor <command>`."""
    exe = Path(sys.executable)
    if FROZEN:
        if windowless and os.name == "nt":
            quiet = exe.with_name("rm-engine-bg.exe")  # the build's windowless twin: no console at every check
            if quiet.exists():
                return [str(quiet)]
        return [str(exe)]
    if windowless and os.name == "nt":
        w = exe.with_name("pythonw.exe")
        exe = w if w.exists() else exe
    return [str(exe), "-m", "readme_monitor"]


def workdir() -> Path:
    """A stable working directory for launched processes: the install folder, or the checkout root."""
    return Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent


def unstable_reason() -> str | None:
    """Why the engine's current location must not be registered with a scheduler, or None.
    A scheduled action has to outlive this process: never a temporary extraction folder or a
    mounted AppImage, whose paths disappear when the program exits."""
    if os.environ.get("APPIMAGE"):
        return ("the AppImage runs from a temporary mount, so the system scheduler cannot start it later; "
                "install the .deb package for scheduled checks, or use Run now")
    if FROZEN:
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
        if bundle.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            return "this build unpacks itself into a temporary folder; schedule the installed application instead"
    return None
