# PyInstaller spec for the ReadmeMonitor engine, built on each target OS (PyInstaller does not
# cross-compile). A folder build, not one file: no unpacking to a temporary folder at every
# scheduled check, a stable path the system scheduler can start, and plain files to sign.
#   pyinstaller packaging/engine.spec --distpath build/engine-dist --workpath build/pyi --noconfirm
# Output: build/engine-dist/engine/{rm-engine[.exe], rm-engine-bg.exe (Windows only), _internal/}
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
notices = ROOT / "build" / "notices" / "PYTHON_THIRD_PARTY.md"
if not notices.exists():
    raise SystemExit(f"{notices} is missing: run packaging/python_notices.py first (see docs/desktop.md)")

datas = collect_data_files("readme_monitor", includes=["templates/*", "web/*", "web/**/*"])
datas += [(str(ROOT / n), "licenses") for n in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md")]
datas += [(str(notices), "licenses")]
hidden = collect_submodules("readme_monitor") + collect_submodules("keyring.backends")

a = Analysis([str(ROOT / "packaging" / "engine_entry.py")], pathex=[str(ROOT)], datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "pytest", "PyInstaller"], noarchive=False)
pyz = PYZ(a.pure)
exes = [EXE(pyz, a.scripts, [], exclude_binaries=True, name="rm-engine", console=True, upx=False,
            codesign_identity=None, entitlements_file=None)]
if sys.platform == "win32":
    # Same program without a console window, for Task Scheduler (launcher.engine(windowless=True)).
    exes.append(EXE(pyz, a.scripts, [], exclude_binaries=True, name="rm-engine-bg", console=False, upx=False))
coll = COLLECT(*exes, a.binaries, a.datas, upx=False, name="engine")
