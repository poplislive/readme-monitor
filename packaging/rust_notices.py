"""Write the licence list of every Rust crate compiled into the desktop shell (normal dependencies of
the locked graph for this target), with the crate's licence file text where the crate ships one.

    python packaging/rust_notices.py build/notices/RUST_THIRD_PARTY.md
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "desktop" / "src-tauri" / "Cargo.toml"
HINTS = ("license", "licence", "copying", "notice")


def main(out: Path) -> int:
    meta = json.loads(subprocess.run(["cargo", "metadata", "--format-version", "1", "--locked", "--manifest-path", str(MANIFEST),
                                      "--filter-platform", host()], capture_output=True, text=True, encoding="utf-8",
                                     check=True).stdout)
    pkgs = {p["id"]: p for p in meta["packages"]}
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    root, seen, stack = meta["resolve"]["root"], set(), [meta["resolve"]["root"]]
    while stack:  # follow normal (runtime) dependencies only: build scripts and dev tools are not shipped
        nid = stack.pop()
        for dep in nodes[nid]["deps"]:
            if any(k["kind"] is None for k in dep["dep_kinds"]) and dep["pkg"] not in seen:
                seen.add(dep["pkg"])
                stack.append(dep["pkg"])
    parts = ["# Rust crates compiled into the ReadmeMonitor desktop shell\n\nGenerated at build time from Cargo.lock.\n"]
    for pid in sorted(seen, key=lambda i: (pkgs[i]["name"], pkgs[i]["version"])):
        if pid == root:
            continue
        p = pkgs[pid]
        folder = Path(p["manifest_path"]).parent
        texts = []
        for f in sorted(folder.iterdir()) if folder.is_dir() else []:
            if f.is_file() and any(h in f.name.lower() for h in HINTS):
                texts.append(f"{f.name}:\n{f.read_text(encoding='utf-8', errors='replace').strip()}")
        parts.append(f"\n## {p['name']} {p['version']}\n\nLicence: {p.get('license') or p.get('license_file') or 'unspecified'}\n\n```\n"
                     + ("\n\n".join(texts) or "(no licence file in the crate)") + "\n```\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(parts), encoding="utf-8")
    print(f"{len(seen)} crates -> {out}")
    return 0


def host() -> str:
    out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
    return next(line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("host: "))


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
