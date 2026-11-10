"""Deterministic evidence extraction, source selection, payload limits and fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache

from . import GENERATOR_VERSION
from .config import Config, RepoConfig
from .credentials import is_sensitive_path, redact
from .readme import MarkerError, parse, strip_managed

MAX_FILES = 60
DOC_RE = re.compile(r"^(docs/.+\.md|[^/]+\.md|\.github/workflows/[^/]+\.ya?ml)$", re.I)
ENV_PATTERNS = [
    re.compile(r"os\.(?:environ\.get|getenv)\(\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]"),
    re.compile(r"os\.environ\[\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]\s*\]"),
    re.compile(r"process\.env\.([A-Z][A-Z0-9_]{2,})"),
    re.compile(r"\$env:([A-Za-z][A-Za-z0-9_]{2,})"),
]
SUBCOMMAND_RE = re.compile(r"add_parser\(\s*['\"]([a-z][a-z0-9-]*)['\"]")


@lru_cache(maxsize=256)
def _glob_re(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z", re.I)


def matches(path: str, patterns) -> bool:
    return any(_glob_re(p).match(path) for p in patterns)


@dataclass
class Snapshot:
    repo: str
    branch: str
    head_sha: str
    description: str
    license_spdx: str | None
    tree: list[dict]  # [{path, sha, size}]
    readmes: dict[str, tuple[str | None, str | None]]  # path -> (text, blob sha)
    releases: list[dict]
    checks: list[dict]
    sources: dict[str, str] = field(default_factory=dict)  # filled only when generation is needed

    @property
    def paths(self) -> list[str]:
        return [e["path"] for e in self.tree]

    @property
    def pending_checks(self) -> list[str]:
        return sorted(c["name"] for c in self.checks if c["status"] != "completed")

    @property
    def license_path(self) -> str | None:
        return next((p for p in self.paths if re.fullmatch(r"(?i)licen[cs]e(\.[a-z]+)?", p)), None)


def select_sources(tree: list[dict], repo: RepoConfig, cfg: Config) -> tuple[list[dict], list[str]]:
    """Allowlisted, non-sensitive, size-limited files. Returns (selected, skipped_reasons)."""
    if repo.api_export != "allowlist":
        return [], []
    selected, skipped = [], []
    for e in sorted(tree, key=lambda e: e["path"]):
        p = e["path"]
        if p in repo.readme_paths or not matches(p, repo.source_include) or matches(p, repo.source_exclude):
            continue
        if is_sensitive_path(p):
            skipped.append(f"{p}: sensitive file name")
        elif e.get("size", 0) > cfg.max_file_bytes:
            skipped.append(f"{p}: larger than {cfg.max_file_bytes} bytes")
        elif len(selected) >= MAX_FILES:
            skipped.append(f"{p}: more than {MAX_FILES} files selected")
        else:
            selected.append(e)
    return selected, skipped


def _block_states(text: str):
    # A person editing (or fixing) a managed block must be noticed even though block
    # contents are otherwise excluded from the inputs.
    try:
        return [(b.id, b.human_edited) for b in parse(text)]
    except MarkerError as e:
        return f"malformed: {e}"


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def fingerprints(snap: Snapshot, repo: RepoConfig, cfg: Config) -> tuple[str, str]:
    """(document fingerprint, Claude-input fingerprint). Uses blob SHAs, so commit history,
    squash/rebase merges and timestamps do not matter, only content."""
    selected, _ = select_sources(snap.tree, repo, cfg)
    completed = sorted((c["name"], c["conclusion"]) for c in snap.checks if c["status"] == "completed")
    llm = _h({
        "generator": GENERATOR_VERSION,
        "template": repo.template_version,
        "config": repo.fingerprint_part(),
        "sources": [(e["path"], e["sha"]) for e in selected],
        "docs": sorted(p for p in snap.paths if DOC_RE.match(p) and p not in repo.readme_paths),
        "readmes": {p: [strip_managed(t or ""), _block_states(t or "")] for p, (t, _) in sorted(snap.readmes.items())},
        "checks": completed,
        "license": snap.license_spdx,
    })
    top = sorted({p.split("/")[0] + ("/" if "/" in p else "") for p in snap.paths})
    doc = _h({"llm": llm, "top": top, "releases": [(r["tag_name"], r.get("published_at")) for r in snap.releases[:5]],
              "branch": snap.branch, "license_path": snap.license_path,
              "mode": repo.publish_mode})  # a mode change re-publishes using the stored generation
    return doc, llm


def extract_facts(snap: Snapshot) -> list[dict]:
    """Facts derived by code from the inspected revision. Each gets a stable id."""
    facts: list[tuple[str, str, str]] = []  # (kind, source, value)
    for path, text in sorted(snap.sources.items()):
        name = path.rsplit("/", 1)[-1]
        if name == "pyproject.toml":
            try:
                proj = tomllib.loads(text).get("project", {})
            except tomllib.TOMLDecodeError:
                proj = {}
            if proj.get("requires-python"):
                facts.append(("runtime", path, f"Python {proj['requires-python']}"))
            for dep in proj.get("dependencies", []):
                facts.append(("dependency", path, dep))
            for cmd, target in proj.get("scripts", {}).items():
                facts.append(("command", path, f"{cmd} (entry point {target})"))
        elif re.fullmatch(r"requirements[\w.-]*\.txt", name):
            for ln in text.splitlines():
                ln = ln.split("#")[0].strip()
                if ln and not ln.startswith("-"):
                    facts.append(("dependency", path, ln))
        elif name == "package.json":
            try:
                pkg = json.loads(text)
            except json.JSONDecodeError:
                pkg = {}
            for script in pkg.get("scripts", {}):
                facts.append(("command", path, f"npm run {script}"))
            for eng, ver in pkg.get("engines", {}).items():
                facts.append(("runtime", path, f"{eng} {ver}"))
            for dep in pkg.get("dependencies", {}):
                facts.append(("dependency", path, dep))
        if name.endswith((".ps1", ".sh")):
            facts.append(("command", path, path))
        for sub in SUBCOMMAND_RE.findall(text):
            facts.append(("command", path, f"subcommand {sub}"))
        for pat in ENV_PATTERNS:
            for env in pat.findall(text):
                facts.append(("config_name", path, env))
    for p in snap.paths:
        if DOC_RE.match(p):
            facts.append(("doc_path", p, p))
    for r in snap.releases[:5]:
        facts.append(("release", "github-releases", f"{r['tag_name']} published {(r.get('published_at') or '')[:10]}"))
    for c in snap.checks:
        if c["status"] == "completed":
            facts.append(("verification", f"checks@{snap.head_sha[:12]}", f"{c['name']}: {c['conclusion']}"))
    if snap.license_spdx:
        facts.append(("license", "github-metadata", snap.license_spdx))
    unique = list(dict.fromkeys(facts))
    return [{"id": f"F{i}", "kind": k, "source": s, "value": v} for i, (k, s, v) in enumerate(unique, 1)]


def build_payload(snap: Snapshot, repo: RepoConfig, cfg: Config, readme_path: str, current: dict[str, str], requested: list[str]) -> tuple[dict, list[str]]:
    """Bounded, redacted request data. Returns (payload, review_flags)."""
    flags: list[str] = []
    files = []
    for path, text in sorted(snap.sources.items()):
        clean, n = redact(text)
        if n:
            flags.append(f"{path}: {n} secret-like value(s) redacted before submission")
        files.append({"path": path, "evidence": f"file:{path}", "content": clean})
    facts = extract_facts(snap)
    readme_text, _ = snap.readmes.get(readme_path, (None, None))
    outline, _ = redact(strip_managed(readme_text or ""))
    payload = {
        "repository": snap.repo,
        "branch": snap.branch,
        "revision": snap.head_sha,
        "readme_path": readme_path,
        "requested_sections": requested,
        "current_sections": current,
        "facts": facts,
        "readme_outside_managed_blocks": outline[:20_000],
        "files": files,
        "omitted_files": [],
    }
    # Drop the largest files until the payload fits; tell the model which were omitted.
    while len(json.dumps(payload)) > cfg.max_payload_chars and payload["files"]:
        biggest = max(payload["files"], key=lambda f: len(f["content"]))
        payload["files"].remove(biggest)
        payload["omitted_files"].append(biggest["path"])
    if payload["omitted_files"]:
        flags.append(f"{len(payload['omitted_files'])} file(s) omitted to fit the payload limit")
    if len(json.dumps(payload)) > cfg.max_payload_chars:
        raise ValueError("payload exceeds max_payload_chars even without source files")
    return payload, flags


def evidence_ids(payload: dict) -> set[str]:
    return {f["id"] for f in payload["facts"]} | {f["evidence"] for f in payload["files"]}
