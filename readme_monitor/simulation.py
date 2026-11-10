"""In-memory stand-ins for GitHub and Claude, and a scripted demo world for the setup interface.

The real Worker, Generator, Publisher, validation and ledger run against these fakes; nothing
here contacts GitHub or Anthropic, reads a credential or touches Task Scheduler. All sample text
is invented."""

from __future__ import annotations

import hashlib
import json
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from .config import load
from .db import DB
from .github import GitHubError, NotFound
from .llm import Generator
from .readme import apply, migrate, render_project_structure
from .worker import Worker


def blob_sha(text: str) -> str:
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _passthrough(home) -> None:
    """Guard used by the simulation only: the fakes cannot spend or publish anything real."""


class FakeGitHub:
    """One in-memory repository with commits, branches and pull requests."""

    def __init__(self, files: dict[str, str], branch: str = "main", latency: float = 0.0):
        self.default_branch = branch
        self.commits: dict[str, dict] = {}
        self.branches: dict[str, str] = {}
        self.prs: dict[int, dict] = {}
        self.releases_list: list[dict] = []
        self.check_runs: dict[str, list[dict]] = {}
        self.writes: list[tuple] = []
        self.reads = 0
        self.exists = True
        self.push = True
        self.quota_low = False
        self.scopes = "repo"
        self.auto_merge_error: str | None = None
        self.full_name: str | None = None  # set to simulate a rename or transfer seen through GitHub's redirect
        self.id: int | None = None
        self.archived = False
        self.empty = False
        self.description = "Demo project"
        self.latency = latency
        self.protected: set[str] = set()  # branches that refuse direct commits (HTTP 409 like a branch rule)
        self.lose_write_answer = False    # the next put_file lands, then the answer is lost (network error)
        self.compare_page = 250           # commits per compare answer, as GitHub's one page
        self.clock = None                 # the demo sets a real clock; tests keep fixed, ordered dates
        self.branches[branch] = self._commit(files, "initial", None)

    # helpers for tests and the demo script
    def _commit(self, files, message, parent, author="demo-author") -> str:
        sha = hashlib.sha1(json.dumps([files, message, parent, len(self.commits)], sort_keys=True).encode()).hexdigest()
        when = self.clock() if self.clock else datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=len(self.commits))
        date = when.isoformat(timespec="seconds")
        self.commits[sha] = {"files": dict(files), "message": message, "parent": parent, "author": author, "date": date}
        return sha

    def push_commit(self, changes: dict[str, str | None], message="change", branch=None, author="demo-author") -> str:
        branch = branch or self.default_branch
        files = dict(self.commits[self.branches[branch]]["files"])
        for p, t in changes.items():
            if t is None:
                files.pop(p, None)
            else:
                files[p] = t
        self.branches[branch] = self._commit(files, message, self.branches[branch], author)
        return self.branches[branch]

    def rewrite(self, changes: dict[str, str | None], message="rewritten", branch=None) -> str:
        """A force push: the branch gets a new history that does not contain the old head."""
        branch = branch or self.default_branch
        files = dict(self.commits[self.branches[branch]]["files"])
        files.update({p: t for p, t in changes.items() if t is not None})
        root = self.commits[self.branches[branch]]
        while root["parent"]:
            root = self.commits[root["parent"]]
        first = next(s for s, c in self.commits.items() if c is root)
        self.branches[branch] = self._commit(files, message, first)
        return self.branches[branch]

    def files(self, branch=None) -> dict[str, str]:
        return self.commits[self.branches[branch or self.default_branch]]["files"]

    def merge(self, number: int):
        pr = self.prs[number]
        head_files = self.files(pr["head"]["ref"])
        changes = {p: head_files[p] for p in head_files if p.lower().endswith(".md") and head_files[p] != self.files(pr["base"]["ref"]).get(p)}
        self.push_commit(changes, "squash merge", pr["base"]["ref"])
        pr.update(state="closed", merged=True)

    def _chain(self, sha) -> list[str]:
        out = []
        while sha:
            out.append(sha)
            sha = self.commits[sha]["parent"]
        return out

    def _shape(self, sha) -> dict:
        c = self.commits[sha]
        return {"sha": sha, "message": c["message"].split("\n", 1)[0][:200], "author": c["author"], "date": c["date"]}

    # GitHub API surface used by the monitor
    def _check(self):
        self.reads += 1
        if self.latency:
            time.sleep(self.latency)
        if not self.exists:
            raise NotFound("not found", 404)

    def whoami(self):
        return {"login": "owner"}

    def repo(self, full):
        self._check()
        meta = {"default_branch": self.default_branch, "description": self.description, "license": None,
                "permissions": {"push": self.push}, "visibility": "private", "archived": self.archived, "id": self.id}
        if self.full_name:
            meta["full_name"] = self.full_name
        return meta

    def head_sha(self, full, branch):
        self._check()
        if self.empty:
            raise GitHubError("GET commits: HTTP 409: Git Repository is empty.", 409)
        return self.branches[branch]

    def releases(self, full, n=5):
        self._check()
        return self.releases_list

    def tree(self, full, sha):
        self._check()
        return [{"path": p, "type": "blob", "sha": blob_sha(t), "size": len(t)} for p, t in sorted(self.commits[sha]["files"].items())]

    def blob_text(self, full, sha):
        self._check()
        for c in self.commits.values():
            for t in c["files"].values():
                if blob_sha(t) == sha:
                    return t
        raise NotFound("blob", 404)

    def file_at(self, full, path, ref):
        self._check()
        sha = self.branches.get(ref, ref)
        t = self.commits[sha]["files"].get(path)
        return (t, blob_sha(t)) if t is not None else (None, None)

    def checks(self, full, sha):
        self._check()
        return self.check_runs.get(sha, [])

    def compare(self, full, base, head):
        self._check()
        if base not in self.commits:
            return {"status": "unavailable", "total_commits": 0, "commits": [], "files": [], "truncated": False}
        head_chain, base_chain = self._chain(head), set(self._chain(base))
        if head == base:
            status, new = "identical", []
        elif base in head_chain:
            status, new = "ahead", head_chain[: head_chain.index(base)]
        elif head in base_chain:
            status, new = "behind", []
        else:
            status, new = "diverged", [s for s in head_chain if s not in base_chain]
        new = new[::-1]  # oldest first, like GitHub
        a, b = self.commits[base]["files"], self.commits[head]["files"]
        files = []
        for p in sorted(set(a) | set(b)):
            if a.get(p) != b.get(p):
                st = "added" if p not in a else ("removed" if p not in b else "modified")
                files.append({"path": p, "status": st, "additions": len((b.get(p) or "").splitlines()),
                              "deletions": len((a.get(p) or "").splitlines())})
        shown = new[: self.compare_page]
        return {"status": status, "total_commits": len(new), "commits": [self._shape(s) for s in shown], "files": files,
                "truncated": len(new) > len(shown)}

    def commit(self, full, sha):
        self._check()
        if sha not in self.commits:
            raise NotFound("commit", 404)
        return self._shape(sha)

    def ref_sha(self, full, branch):
        return self.branches.get(branch)

    def commit_message(self, full, sha):
        return self.commits[sha]["message"]

    def find_open_pr(self, full, owner, branch, base):
        return next((p for p in self.prs.values() if p["head"]["ref"] == branch and p["base"]["ref"] == base and p["state"] == "open"), None)

    def pr(self, full, number):
        return self.prs[number]

    def create_ref(self, full, branch, sha):
        self.writes.append(("create_ref", branch))
        self.branches[branch] = sha

    def move_ref(self, full, branch, sha):
        self.writes.append(("move_ref", branch))
        self.branches[branch] = sha

    def put_file(self, full, path, branch, text, message, blob):
        if self.latency:
            time.sleep(self.latency)
        if branch in self.protected:
            raise GitHubError(f"PUT contents: HTTP 409: Repository rule violations found for {branch}", 409)
        current = self.files(branch).get(path)
        if (blob_sha(current) if current is not None else None) != blob:
            raise GitHubError("409 sha mismatch", 409)
        self.writes.append(("put_file", path, branch))
        sha = self.push_commit({path: text}, message, branch, author="readme-monitor")
        if self.lose_write_answer:
            self.lose_write_answer = False
            raise GitHubError(f"PUT contents/{path}: network error ConnectionError")
        return sha

    def create_pr(self, full, title, head, base, body):
        n = len(self.prs) + 1
        self.writes.append(("create_pr", n))
        self.prs[n] = {"number": n, "node_id": f"PR_{n}", "html_url": f"https://github.com/{full}/pull/{n}", "title": title,
                       "body": body, "state": "open", "merged": False, "head": {"ref": head}, "base": {"ref": base}}
        return self.prs[n]

    def update_pr(self, full, number, title, body):
        self.writes.append(("update_pr", number))
        self.prs[number].update(title=title, body=body)
        return self.prs[number]

    def enable_auto_merge(self, node_id, method="SQUASH"):
        if self.auto_merge_error:
            raise GitHubError(self.auto_merge_error)
        self.writes.append(("auto_merge", node_id))


class FakeAccount:
    """Several FakeGitHub repositories behind one credential, as the worker sees GitHub."""

    def __init__(self, owner: str):
        self.owner = owner
        self.repos: dict[str, FakeGitHub] = {}
        self.quota_low = False
        self.scopes = "repo"

    def add(self, name: str, fake: FakeGitHub) -> FakeGitHub:
        fake.id = fake.id or len(self.repos) + 1000
        self.repos[name.lower()] = fake
        return fake

    def whoami(self):
        return {"login": self.owner}

    def __getattr__(self, attr):
        def call(full, *a, **k):
            return getattr(self.repos[full.lower()], attr)(full, *a, **k)
        return call


class FakeClaude:
    """Scripted Claude client. `responder(payload) -> dict | Exception` builds each reply."""

    def __init__(self, responder=None, latency: float = 0.0):
        self.calls: list[dict] = []
        self.responder = responder or default_responder
        self.latency = latency
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        payload = json.loads(kw["messages"][0]["content"].split("\n", 1)[1])
        self.calls.append(payload)
        if self.latency:
            time.sleep(self.latency)
        reply = self.responder(payload)
        if isinstance(reply, Exception):
            raise reply
        stop = reply.pop("_stop", "end_turn") if isinstance(reply, dict) else "end_turn"
        text = reply if isinstance(reply, str) else json.dumps(reply)
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage, stop_reason=stop, _request_id="req_test")


def default_responder(payload):
    fact = payload["facts"][0]["id"] if payload["facts"] else None
    return {"sections": [
        {"id": s, "content": f"Generated {s} for {payload['revision'][:7]}.", "evidence": [fact] if fact else [],
         "uncertain": False, "review_notes": ""}
        for s in payload["requested_sections"]
    ]}


# ---- demo world -------------------------------------------------------------------------

OWNER = "octo-demo"
DEMO_CONFIG = """
[monitor]
quiet_minutes = 0
min_generation_interval_hours = 0
check_wait_minutes = 0

[schedule]
mode = "interval"
interval_minutes = 15

[budget]
per_run_usd = 0.40
daily_usd = 1.20
monthly_usd = 6.00

[[repo]]
name = "octo-demo/atlas-api"
source_include = ["pyproject.toml", "atlas/**/*.py"]
managed_sections = ["features", "project-structure", "status"]
publish_mode = "review_pr"
api_export = "allowlist"

[[repo]]
name = "octo-demo/field-notes"
managed_sections = ["project-structure"]
publish_mode = "direct"

[[repo]]
name = "octo-demo/pocket-ledger"
managed_sections = ["project-structure"]
publish_mode = "review_pr"

[[repo]]
name = "octo-demo/trail-maps"
managed_sections = ["project-structure"]
publish_mode = "review_pr"

[[repo]]
name = "octo-demo/signal-garden"
source_include = ["garden/**/*.py"]
managed_sections = ["features", "project-structure"]
publish_mode = "review_pr"
api_export = "allowlist"
monthly_usd = 0.01
"""


def _readme(name: str, sections: tuple[str, ...], files: dict[str, str]) -> str:
    base = f"# {name.split('/')[1]}\n\nA small invented project used to demonstrate ReadmeMonitor.\n\n## Notes\n\nWritten by a person.\n"
    text = migrate(base, name, "Invented demo project", sections, ())
    if "project-structure" in sections:  # start current, so only real changes show up
        text = apply(text, {"project-structure": render_project_structure(sorted([*files, "README.md"]))}).text
    return text


class Demo:
    """A ready simulation: cfg, db, the fake account and a step-by-step script."""

    def __init__(self, home: Path, latency: float = 0.0):
        home.mkdir(parents=True, exist_ok=True)
        self.home = home
        self.config_path = home / "config.toml"
        self.config_path.write_text(textwrap.dedent(DEMO_CONFIG), encoding="utf-8")
        self.cfg = load(self.config_path)
        self.db_path = home / "state.db"
        self.db = DB(self.db_path)
        self.account = FakeAccount(OWNER)
        self.claude = FakeClaude(latency=latency)
        worlds = {
            "atlas-api": ({"pyproject.toml": '[project]\nname = "atlas-api"\nrequires-python = ">=3.11"\n',
                           "atlas/server.py": "def health():\n    return 'ok'\n"}, ("features", "project-structure", "status")),
            "field-notes": ({"notes/2026-01-trail.md": "Invented field notes.\n"}, ("project-structure",)),
            "pocket-ledger": ({"ledger/core.py": "TOTAL = 0\n"}, ("project-structure",)),
            "trail-maps": ({"maps/route.geojson": "{}\n"}, ("project-structure",)),
            "signal-garden": ({"garden/sprout.py": "def grow():\n    return 1\n"}, ("features", "project-structure")),
        }
        for short, (files, sections) in worlds.items():
            name = f"{OWNER}/{short}"
            fake = FakeGitHub({**files, "README.md": _readme(name, sections, files)}, latency=latency)
            # Believable relative times: the seeded history is a few days old, later commits are made now.
            fake.commits[fake.branches[fake.default_branch]]["date"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
            fake.clock = lambda: datetime.now(timezone.utc)
            self.account.add(name, fake)
        self.step = 0
        self.script = [
            ("A source change in atlas-api and a new note in field-notes", self._change_sources),
            ("A person merges the atlas-api pull request", self._merge_atlas),
            ("A person edits a managed section of trail-maps by hand", self._edit_trail_maps),
        ]

    def repo(self, short: str) -> FakeGitHub:
        return self.account.repos[f"{OWNER}/{short}"]

    def _change_sources(self):
        self.repo("atlas-api").push_commit({"atlas/routes.py": "def list_routes():\n    return []\n"}, "Add a route listing")
        self.repo("field-notes").push_commit({"sketches/ridge.txt": "An invented ridge sketch.\n"}, "Add a sketches folder")

    def _merge_atlas(self):
        fake = self.repo("atlas-api")
        for number, pr in fake.prs.items():
            if pr["state"] == "open":
                fake.merge(number)

    def _edit_trail_maps(self):
        fake = self.repo("trail-maps")
        text = fake.files()["README.md"]
        start = text.index("<!-- readme-monitor:start:project-structure")
        line_end = text.index("\n", start) + 1
        edited = text[:line_end] + "A person wrote this map overview by hand.\n" + text[line_end:]
        fake.push_commit({"README.md": edited, "maps/peak.geojson": "{}\n"}, "Describe the maps by hand", author="a-person")

    def advance(self) -> str | None:
        """Apply the next scripted change to the fake world; None when the script is finished."""
        if self.step >= len(self.script):
            return None
        label, change = self.script[self.step]
        change()
        self.step += 1
        return label

    def run_cycle(self, trigger: str = "manual", dry_run: bool = False, repos: list[str] | None = None) -> dict:
        """One real cycle (Worker, Generator, Publisher, ledger) against the fakes."""
        run_id = self.db.start_run("dry_run" if dry_run else trigger, "simulation")
        gen = Generator(self.cfg, self.db, "sk-simulation", client=self.claude, sleep=lambda s: None, guard=_passthrough)
        worker = Worker(self.cfg, self.db, self.account, gen, self.home, run_id, trigger="dry_run" if dry_run else trigger,
                        dry_run=dry_run, only=repos, guard=_passthrough)
        try:
            summary = worker.run()
        except Exception as e:
            self.db.finish_run(run_id, "crashed", {"error": type(e).__name__})
            raise
        self.db.finish_run(run_id, "cancelled" if worker.cancelled else "ok", summary)
        if not dry_run:
            self.db.clear_cancel()
        return summary


def build_demo(home: Path, latency: float = 0.0) -> Demo:
    return Demo(home, latency)
