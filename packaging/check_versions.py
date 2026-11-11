"""One version everywhere: the engine, the interface, the desktop shell and (when given) the tag.

    python packaging/check_versions.py            # all files agree
    python packaging/check_versions.py v0.4.0     # ... and match the release tag

readme_monitor/__init__.py is the source; the others must equal it. tauri.conf.json takes its
version from desktop/package.json, and the updater metadata from tauri.conf.json.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def versions() -> dict[str, str]:
    init = (ROOT / "readme_monitor" / "__init__.py").read_text(encoding="utf-8")
    cargo = (ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    conf = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    return {
        "readme_monitor/__init__.py": re.search(r'__version__\s*=\s*"([^"]+)"', init)[1],
        "ui/package.json": json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))["version"],
        "desktop/package.json": json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))["version"],
        "desktop/src-tauri/Cargo.toml": re.search(r'^version\s*=\s*"([^"]+)"', cargo, re.M)[1],
        "desktop/src-tauri/tauri.conf.json": conf["version"],
    }


def main(tag: str | None) -> int:
    v = versions()
    want = v["readme_monitor/__init__.py"]
    bad = [f"{k}: {x}" for k, x in v.items() if x != want and not (k.endswith("tauri.conf.json") and x == "../package.json")]
    if tag is not None and tag.removeprefix("v") != want:
        bad.append(f"tag {tag} != {want}")
    if not re.fullmatch(r"\d+\.\d+\.\d+", want):
        bad.append(f"{want} is not MAJOR.MINOR.PATCH (the desktop updater compares semantic versions)")
    if bad:
        print(f"Version mismatch (expected {want}):\n  " + "\n  ".join(bad))
        return 1
    print(f"All versions are {want}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
