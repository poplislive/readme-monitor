"""Acceptance criterion 4 (second half): direct publication against a real Git repository.

simulation: local bare Git remote, not GitHub. `BareRemote` below implements the client methods the
worker and publisher call, with the git command line on a bare repository under tmp_path. The real
Worker, Generator (scripted model), Publisher and database run unchanged. It proves the commit
semantics (fast-forward, expected-blob check, no force) in real Git; it says nothing about GitHub's
own API, permissions or branch protection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap

import pytest

from readme_monitor.config import load
from readme_monitor.db import DB
from readme_monitor.github import GitHubError, NotFound
from readme_monitor.simulation import FakeClaude

from .conftest import CONFIG, REPO, initial_readme, run_cycle

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")

PERSON = ("A Person", "person@example.invalid")
MONITOR = ("ReadmeMonitor", "readme-monitor@example.invalid")
FILES = {
    "README.md": initial_readme(),
    "pyproject.toml": '[project]\nname = "demo"\nrequires-python = ">=3.11"\n[project.scripts]\ndemo = "demo:main"\n',
    "src/demo/app.py": "import os\nTOKEN = os.getenv('DEMO_TOKEN')\n",
}


class BareRemote:
    quota_low = False

    def __init__(self, path):
        self.path = path
        self.env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        self.writes: list[tuple] = []
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(path)], check=True, capture_output=True, env=self.env)

    def git(self, *args, data: bytes | None = None, env: dict | None = None, strip: bool = True) -> str:
        r = subprocess.run(["git", "-C", str(self.path), *args], input=data, capture_output=True, env=env or self.env)
        if r.returncode:
            raise GitHubError(f"git {args[0]}: {r.stderr.decode(errors='replace').strip()}", 409)
        out = r.stdout.decode("utf-8")
        return out.rstrip("\n") if strip else out

    def add_commit(self, changes: dict[str, str], message: str, who=PERSON) -> str:
        """A real commit on top of main. `update-ref` with the old value is a compare-and-swap, never a force."""
        parent = self.ref_sha(REPO, "main")
        index = {**self.env, "GIT_INDEX_FILE": str(self.path / "scratch-index"), "GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1],
                 "GIT_COMMITTER_NAME": who[0], "GIT_COMMITTER_EMAIL": who[1]}
        if parent:
            self.git("read-tree", parent, env=index)
        for path, text in changes.items():
            blob = self.git("hash-object", "-w", "--stdin", data=text.encode(), env=index)
            self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=index)
        tree = self.git("write-tree", env=index)
        new = self.git("commit-tree", tree, *(["-p", parent] if parent else []), "-m", message, env=index)
        self.git("update-ref", "refs/heads/main", new, parent or "0" * 40)
        return new

    # ---- the GitHub client surface used by Worker and Publisher
    def whoami(self):
        return {"login": "owner"}

    def repo(self, full):
        return {"default_branch": "main", "description": "Local bare remote", "license": None, "permissions": {"push": True},
                "visibility": "private", "archived": False, "id": 1}

    def ref_sha(self, full, branch):
        try:
            return self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        except GitHubError:
            return None

    def head_sha(self, full, branch):
        return self.ref_sha(full, branch)

    def releases(self, full, n=5):
        return []

    def checks(self, full, sha):
        return []

    def tree(self, full, sha):
        rows = [line.split(None, 4) for line in self.git("ls-tree", "-r", "-l", sha).splitlines()]
        return [{"path": r[4].split("\t")[-1] if "\t" in r[4] else r[4], "type": "blob", "sha": r[2], "size": int(r[3])} for r in rows]

    def blob_text(self, full, sha):
        try:
            return self.git("cat-file", "blob", sha, strip=False)
        except GitHubError:
            raise NotFound("blob", 404) from None

    def file_at(self, full, path, ref):
        try:
            blob = self.git("rev-parse", "--verify", "--quiet", f"{ref}:{path}")
        except GitHubError:
            return None, None
        return self.git("cat-file", "blob", blob, strip=False), blob

    def commit_message(self, full, sha):
        return self.git("log", "-1", "--format=%B", sha)

    def commit(self, full, sha):
        sha, author, date, subject = self.git("log", "-1", "--format=%H%x1f%an%x1f%aI%x1f%s", sha).split("\x1f")
        return {"sha": sha, "message": subject, "author": author, "date": date}

    def compare(self, full, base, head):
        if base == head:
            status = "identical"
        elif self._ancestor(base, head):
            status = "ahead"
        elif self._ancestor(head, base):
            status = "behind"
        else:
            status = "diverged"
        shas = self.git("rev-list", "--reverse", f"{base}..{head}").split()
        files = [{"path": p, "status": "modified", "additions": int(a) if a.isdigit() else 0, "deletions": int(d) if d.isdigit() else 0}
                 for a, d, p in (line.split("\t") for line in self.git("diff", "--numstat", base, head).splitlines())]
        return {"status": status, "total_commits": len(shas), "commits": [self.commit(full, s) for s in shas], "files": files, "truncated": False}

    def _ancestor(self, a, b) -> bool:
        return subprocess.run(["git", "-C", str(self.path), "merge-base", "--is-ancestor", a, b], env=self.env).returncode == 0

    def put_file(self, full, path, branch, text, message, blob):
        """Contents-API semantics: refuse unless the file is still the expected blob; a new commit on the tip, never a force."""
        _, current = self.file_at(full, path, f"refs/heads/{branch}")
        if current != blob:
            raise GitHubError(f"{path} does not match the expected blob", 409)
        self.writes.append(("put_file", path, branch))
        return self.add_commit({path: text}, message, who=MONITOR)

    # ---- assertions helpers
    def history(self) -> list[str]:
        return self.git("log", "--format=%H", "refs/heads/main").split()

    def refs(self) -> list[str]:
        return self.git("for-each-ref", "--format=%(refname)").split()


@pytest.fixture
def world(tmp_path):
    remote = BareRemote(tmp_path / "remote.git")
    first = remote.add_commit(FILES, "initial", who=PERSON)
    cfg_path = tmp_path / "home" / "config.toml"
    cfg_path.parent.mkdir()
    cfg_path.write_text(textwrap.dedent(CONFIG).replace("{mode}", "direct"), encoding="utf-8")
    cfg, db, claude = load(cfg_path), DB(tmp_path / "home" / "state.db"), FakeClaude()
    home = tmp_path / "home"
    return remote, first, lambda: run_cycle(cfg, db, remote, claude, home), db, claude


def test_criterion_04_direct_publication_is_a_fast_forward_commit_with_the_trailer(world):
    """simulation: local bare Git remote, not GitHub."""
    remote, first, run, db, claude = world
    assert run() == {"published": 1}
    history = remote.history()
    assert len(history) == 2 and history[1] == first
    assert remote._ancestor(first, history[0]) and remote.git("rev-list", "--count", f"{first}..{history[0]}") == "1"  # a fast-forward
    assert remote.git("log", "-1", "--format=%an", history[0]) == "ReadmeMonitor"
    assert "Readme-Monitor-Proposal: 1" in remote.commit_message(REPO, history[0])
    assert "Generated features" in remote.file_at(REPO, "README.md", "refs/heads/main")[0]
    assert remote.refs() == ["refs/heads/main"] and [w[0] for w in remote.writes] == ["put_file"]  # no side branch, no pull request
    proposal = db.latest_proposal(REPO)
    assert proposal["state"] == "published" and proposal["published_sha"] == history[0]
    stages = [(e["stage"], e["status"]) for e in db.events(limit=100) if e["repo"] == REPO]
    assert stages[-1] == ("result", "done") and ("publish", "done") in stages
    run()  # our own commit moved the head: re-checked, nothing is written or generated again
    assert remote.history() == history and len(claude.calls) == 1


def test_criterion_04_a_commit_by_another_person_before_the_write_stops_the_publication(world):
    """simulation: local bare Git remote, not GitHub. The head moves between analysis and the direct commit."""
    remote, first, run, db, _ = world
    real, n = remote.head_sha, {"i": 0}

    def moving(full, branch):
        n["i"] += 1
        if n["i"] == 3:  # after the publisher's freshness check, before its commit
            remote.add_commit({"src/demo/app.py": "RACE = 1\n"}, "person pushes first")
        return real(full, branch)

    remote.head_sha = moving
    assert run() == {"stale": 1}
    assert remote.writes == [] and remote.git("log", "-1", "--format=%s") == "person pushes first"
    assert "Generated" not in remote.file_at(REPO, "README.md", "refs/heads/main")[0]
    assert db.repo(REPO)["state"] == "stale"


def test_criterion_04_a_person_editing_the_readme_in_the_race_window_is_never_overwritten(world):
    """simulation: local bare Git remote, not GitHub. The expected-blob check is what protects the person's edit."""
    remote, first, run, db, _ = world
    person_text = FILES["README.md"] + "\nA person wrote this paragraph just now.\n"
    real_put = remote.put_file

    def racing(*args):
        remote.add_commit({"README.md": person_text}, "person edits the README")
        return real_put(*args)

    remote.put_file = racing
    assert run() == {"stale": 1}
    assert remote.file_at(REPO, "README.md", "refs/heads/main")[0] == person_text  # their edit is intact
    assert remote.git("log", "-1", "--format=%s") == "person edits the README"
    assert not any("Readme-Monitor-Proposal" in remote.commit_message(REPO, s) for s in remote.history())
    assert remote.writes == []
