"""PopL Repository Standard v1: section contract, managed blocks, rendering and migration.

Managed content lives between named markers:

    <!-- readme-monitor:start:features hash=0123456789ab -->
    ...generated content...
    <!-- readme-monitor:end:features -->

`hash` records what the monitor last wrote. If the content no longer matches it, a person
edited the block and the monitor reports a conflict instead of overwriting it. Everything
outside managed blocks is preserved byte for byte.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass
from importlib import resources

TEMPLATE_NAME = "PopL Repository Standard v1"

# (id, heading, extra heading aliases used when adopting an existing README)
SECTIONS = (
    ("overview", "Overview", ("about", "introduction", "what is it")),
    ("features", "Features", ("what you can do", "highlights", "capabilities")),
    ("quick-start", "Quick start", ("getting started", "quickstart", "installation", "install", "setup", "get started")),
    ("configuration", "Configuration", ("config", "settings")),
    ("architecture", "Architecture", ("how it works", "design")),
    ("project-structure", "Project structure", ("repository layout", "project layout", "structure", "layout")),
    ("development", "Development and testing", ("development", "testing", "tests", "build from source", "for developers")),
    ("status", "Current status and limitations", ("current status", "status", "project status", "limitations")),
    ("recent-changes", "Recent changes", ("changelog", "releases", "release notes", "what's new")),
    ("roadmap", "Roadmap", ("what's coming next", "planned")),
    ("contributing", "Contributing and support", ("contributing", "support", "getting help")),
    ("license", "License", ("license and credits", "licence")),
)
SECTION_IDS = tuple(s[0] for s in SECTIONS)
HEADINGS = {s[0]: s[1] for s in SECTIONS}
HUMAN_OWNED_DEFAULT = frozenset({"overview", "roadmap", "contributing", "license"})
DETERMINISTIC_SECTIONS = frozenset({"project-structure", "recent-changes", "license"})
LLM_SECTIONS = frozenset(SECTION_IDS) - DETERMINISTIC_SECTIONS
PLACEHOLDER = "_This section has not been generated yet._"

START_RE = re.compile(r"^<!-- readme-monitor:start:([a-z0-9-]+)(?: hash=([0-9a-f]{12}))? -->$")
END_RE = re.compile(r"^<!-- readme-monitor:end:([a-z0-9-]+) -->$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


class MarkerError(ValueError):
    pass


@dataclass
class Block:
    id: str
    start: int  # index of the start-marker line
    end: int  # index of the end-marker line
    hash: str | None
    content: str

    @property
    def human_edited(self) -> bool:
        return self.hash is None or content_hash(self.content) != self.hash


def normalize(content: str) -> str:
    lines = [ln.rstrip() for ln in content.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip("\n")


def content_hash(content: str) -> str:
    return hashlib.sha256(normalize(content).encode("utf-8")).hexdigest()[:12]


def render_block(section_id: str, content: str) -> str:
    body = normalize(content)
    return f"<!-- readme-monitor:start:{section_id} hash={content_hash(body)} -->\n{body}\n<!-- readme-monitor:end:{section_id} -->"


def parse(text: str) -> list[Block]:
    """Return managed blocks; raise MarkerError for anything malformed."""
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[Block] = []
    open_id = open_line = open_hash = None
    seen: set[str] = set()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if "readme-monitor:" not in line:
            continue
        start, end = START_RE.match(line), END_RE.match(line)
        if start:
            if open_id is not None:
                raise MarkerError(f"line {i + 1}: block {start[1]!r} starts inside block {open_id!r}")
            if start[1] not in SECTION_IDS:
                raise MarkerError(f"line {i + 1}: unknown section id {start[1]!r}")
            if start[1] in seen:
                raise MarkerError(f"line {i + 1}: duplicate block {start[1]!r}")
            open_id, open_line, open_hash = start[1], i, start[2]
        elif end:
            if open_id != end[1]:
                raise MarkerError(f"line {i + 1}: end marker {end[1]!r} does not close {open_id!r}")
            blocks.append(Block(open_id, open_line, i, open_hash, "\n".join(lines[open_line + 1 : i])))
            seen.add(open_id)
            open_id = None
        else:
            raise MarkerError(f"line {i + 1}: malformed readme-monitor marker")
    if open_id is not None:
        raise MarkerError(f"block {open_id!r} is never closed")
    return blocks


def strip_managed(text: str) -> str:
    """README text with managed content removed, used as a generation input so the monitor's
    own merged output never looks like a new change."""
    try:
        blocks = parse(text)
    except MarkerError:
        return text
    lines = text.replace("\r\n", "\n").split("\n")
    for b in reversed(blocks):
        lines[b.start : b.end + 1] = [f"<!-- managed:{b.id} -->"]
    return "\n".join(lines)


@dataclass
class ApplyResult:
    text: str
    changed: list[str]
    conflicts: list[str]
    missing: list[str]


def apply(text: str, contents: dict[str, str]) -> ApplyResult:
    """Replace managed block contents. Human-edited blocks are left untouched and reported."""
    blocks = {b.id: b for b in parse(text)}
    lines = text.replace("\r\n", "\n").split("\n")
    changed, conflicts = [], []
    for b in sorted(blocks.values(), key=lambda b: b.start, reverse=True):
        if b.id not in contents:
            continue
        if b.human_edited:
            conflicts.append(b.id)
            continue
        if normalize(contents[b.id]) == normalize(b.content):
            continue  # identical meaning: keep exact bytes, no churn
        lines[b.start : b.end + 1] = render_block(b.id, contents[b.id]).split("\n")
        changed.append(b.id)
    missing = [s for s in contents if s not in blocks]
    new = "\n".join(lines)
    if "\r\n" in text:  # keep the file's existing line endings
        new = new.replace("\n", "\r\n")
    return ApplyResult(new, sorted(changed), sorted(conflicts), sorted(missing))


def _heading_key(title: str) -> str:
    return re.sub(r"[^a-z0-9' ]+", "", title.lower()).strip()


def level2_headings(lines: list[str]) -> list[tuple[int, str]]:
    out, fenced = [], False
    for i, ln in enumerate(lines):
        if FENCE_RE.match(ln):
            fenced = not fenced
        elif not fenced and ln.startswith("## "):
            out.append((i, ln[3:].strip()))
    return out


def match_section(title: str) -> str | None:
    key = _heading_key(title)
    for sid, heading, aliases in SECTIONS:
        if key == _heading_key(heading) or key in aliases:
            return sid
    return None


def template_text(repo_name: str, description: str, managed: tuple[str, ...], disabled: tuple[str, ...]) -> str:
    raw = resources.files("readme_monitor").joinpath("templates/popl-standard-v1.md").read_text(encoding="utf-8")
    out = raw.replace("{{name}}", repo_name.split("/")[-1]).replace("{{description}}", description or "_Add a one-sentence purpose._")
    for sid in SECTION_IDS:
        token = "{{section:" + sid + "}}"
        if sid in disabled:
            out = re.sub(rf"\n## [^\n]+\n\n{re.escape(token)}\n", "", out)
        elif sid in managed:
            out = out.replace(token, render_block(sid, PLACEHOLDER))
        else:
            out = out.replace(token, "_To be written by the maintainers._")
    return out


def minimal_text(repo_name: str, description: str, managed: tuple[str, ...], disabled: tuple[str, ...]) -> str:
    """A short starting README for a repository that has none: the title, the GitHub
    description and only the sections the monitor can fill from evidence. Nothing is invented."""
    out = [f"# {repo_name.split('/')[-1]}", "", description or "_Add a one-sentence purpose._", ""]
    for sid in SECTION_IDS:
        if sid in managed and sid not in disabled:
            out += [f"## {HEADINGS[sid]}", "", render_block(sid, PLACEHOLDER), ""]
    return "\n".join(out)


def migrate(text: str | None, repo_name: str, description: str, managed: tuple[str, ...], disabled: tuple[str, ...],
            minimal: bool = False, take_over: bool = True) -> str:
    """Adopt an existing README: wrap recognised managed sections in markers (keeping their
    current text) and add missing managed sections in standard order. Custom sections and
    all other content are preserved. With take_over=False, recognised existing sections are
    left to people entirely and only missing sections are added."""
    if not text or not text.strip():
        return (minimal_text if minimal else template_text)(repo_name, description, managed, disabled)
    existing = {b.id for b in parse(text)}
    lines = text.replace("\r\n", "\n").split("\n")
    heads = level2_headings(lines)
    found: dict[str, int] = {}
    for idx, (line_no, title) in enumerate(heads):
        sid = match_section(title)
        if sid and sid not in found:
            found[sid] = idx
    wanted = [s for s in SECTION_IDS if s in managed and s not in disabled and s not in existing]
    edits: list[tuple[int, int, int, list[str]]] = []  # (start, end_exclusive, order, replacement)
    for order_no, sid in enumerate(wanted):
        if sid in found and not take_over:
            continue
        if sid in found:
            h_line = heads[found[sid]][0]
            nxt = heads[found[sid] + 1][0] if found[sid] + 1 < len(heads) else len(lines)
            body = lines[h_line + 1 : nxt]
            while body and not body[-1].strip():
                body.pop()
            content = "\n".join(body).strip("\n") or PLACEHOLDER
            if "readme-monitor:" in content:
                continue  # already partially managed; leave for a human to resolve
            edits.append((h_line + 1, h_line + 1 + len(body), order_no, ["", render_block(sid, content)]))
        else:
            order = SECTION_IDS.index(sid)
            later = [found[s] for s in SECTION_IDS[order + 1 :] if s in found]
            at = heads[min(later)][0] if later else len(lines)
            new = [f"## {HEADINGS[sid]}", "", render_block(sid, PLACEHOLDER), ""]
            if at == len(lines):
                new = ([""] if lines and lines[-1].strip() else []) + new
            edits.append((at, at, order_no, new))
    # Apply bottom-up; at a shared insertion point the later section goes in first so the
    # earlier one lands above it, keeping standard order.
    for start, end, _, repl in sorted(edits, key=lambda e: (e[0], e[1], e[2]), reverse=True):
        lines[start:end] = repl
    out = "\n".join(lines)
    return out if out.endswith("\n") else out + "\n"


def unified_diff(old: str, new: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}")
    )


# ---- deterministic sections -------------------------------------------------


def render_project_structure(paths: list[str]) -> str:
    top: dict[str, bool] = {}
    for p in paths:
        first, _, rest = p.partition("/")
        if first.startswith(".") and first != ".github":
            continue
        top[first] = top.get(first, False) or bool(rest)
    if not top:
        return "_No files found._"
    names = sorted(top, key=lambda n: (not top[n], n.lower()))[:40]
    rows = [f"{n}/" if top[n] else n for n in names]
    return "Top-level layout of the default branch:\n\n```text\n" + "\n".join(rows) + "\n```"


def render_recent_changes(repo: str, releases: list[dict], branch: str) -> str:
    commits = f"https://github.com/{repo}/commits/{branch}"
    if not releases:
        return f"No releases have been published yet. See the [commit history]({commits}) for changes."
    rows = []
    for r in releases[:5]:
        date = (r.get("published_at") or "")[:10]
        name = r.get("name") or r["tag_name"]
        label = name if name == r["tag_name"] else f"{r['tag_name']}: {name}"
        pre = " (pre-release)" if r.get("prerelease") else ""
        rows.append(f"- [{label}]({r['html_url']}){pre} - {date}")
    return "\n".join(rows) + f"\n\nFull history: [releases](https://github.com/{repo}/releases) and [commits]({commits})."


def render_license(spdx: str | None, license_path: str | None) -> str:
    if license_path and spdx and spdx != "NOASSERTION":
        return f"Licensed under the {spdx} license. See [{license_path}]({license_path})."
    if license_path:
        return f"See [{license_path}]({license_path}) for license terms."
    return "No license has been selected for this project yet. All rights are reserved by the owner until one is chosen."
