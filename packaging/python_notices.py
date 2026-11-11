"""Write the licence notices of every Python distribution bundled into the frozen engine.

Run with the build environment's interpreter (the one PyInstaller uses), after `pip install`:

    python packaging/python_notices.py build/notices/PYTHON_THIRD_PARTY.md

Build-only tools (PyInstaller and its helpers, pip, setuptools, pytest) are left out because they
are not shipped; the CPython licence is included because the interpreter is.
"""
import sys
from importlib import metadata
from pathlib import Path

NOT_SHIPPED = {"pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile", "macholib", "pywin32-ctypes", "pip", "setuptools",
               "wheel", "pytest", "pluggy", "iniconfig", "packaging", "pygments", "colorama", "readme-monitor"}
LICENCE_HINTS = ("license", "licence", "copying", "notice", "authors")


def main(out: Path) -> int:
    parts = ["# Python components bundled with the ReadmeMonitor engine\n",
             "Generated at build time from the build environment. ReadmeMonitor itself is licensed under Apache-2.0 (LICENSE, NOTICE).\n"]
    py_licence = Path(sys.base_prefix) / "LICENSE.txt"
    if not py_licence.exists():
        py_licence = next(iter(Path(sys.base_prefix).glob("lib/python3*/LICENSE.txt")), py_licence)
    parts.append(f"\n## CPython {sys.version.split()[0]}\n\n```\n{py_licence.read_text(encoding='utf-8', errors='replace') if py_licence.exists() else 'PSF License Agreement: https://docs.python.org/3/license.html'}\n```\n")
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        if name.lower().replace("_", "-") in NOT_SHIPPED:
            continue
        lic = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
        if len(lic) > 80 or not lic:
            lic = ", ".join(c.split("::")[-1].strip() for c in dist.metadata.get_all("Classifier") or [] if c.startswith("License")) or "see below"
        texts = []
        for f in dist.files or []:
            if any(h in f.name.lower() for h in LICENCE_HINTS) and not f.name.endswith((".py", ".pyc")):
                try:
                    texts.append(f"{f.name}:\n{Path(dist.locate_file(f)).read_text(encoding='utf-8', errors='replace').strip()}")
                except OSError:
                    pass
        parts.append(f"\n## {name} {dist.version}\n\nLicence: {lic}\n\n```\n" + ("\n\n".join(texts) or "(no licence file shipped in the distribution)") + "\n```\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(parts), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
