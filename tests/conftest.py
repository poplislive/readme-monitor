"""Test fixtures. The in-memory GitHub and the scripted Claude client live in readme_monitor.simulation."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from readme_monitor import runtime, scheduler
from readme_monitor.config import load
from readme_monitor.db import DB
from readme_monitor.llm import Generator
from readme_monitor.readme import migrate
from readme_monitor.simulation import FakeClaude, FakeGitHub, blob_sha, default_responder  # noqa: F401 (re-exported for tests)
from readme_monitor.worker import Worker

REPO = "owner/demo"


@pytest.fixture(autouse=True)
def offline_host(request, monkeypatch, tmp_path_factory):
    """Offline tests stand in for an activated installation. Tests marked real_host use the real
    guard, which refuses wherever this folder was not activated deliberately. In every test the
    account's active-folder pointer and Task Scheduler are replaced, so nothing real is touched."""
    pointer = tmp_path_factory.mktemp("account") / runtime.POINTER_NAME
    monkeypatch.setattr(runtime, "pointer_path", lambda: pointer)
    calls = []

    def no_task_scheduler(*args):
        calls.append(args)
        return subprocess.CompletedProcess(["schtasks.exe", *args], 1, "", "ERROR: The system cannot find the file specified.")

    monkeypatch.setattr(scheduler, "_schtasks", no_task_scheduler)
    # Every OS exercises the Task Scheduler adapter against the stand-in above, so no test reaches a
    # real launchctl or systemctl; test_desktop_platforms.py covers those backends with their own stand-ins.
    monkeypatch.setattr(scheduler, "_backend", lambda: None)
    if "real_host" not in request.keywords:
        monkeypatch.setattr(runtime, "require_active", lambda home, host_lock=None: None)
    return calls


CONFIG = """
[monitor]
poll_interval_minutes = 5
check_wait_minutes = 30
quiet_minutes = 0
min_generation_interval_hours = 0
[budget]
per_run_usd = 1.0
daily_usd = 2.0
monthly_usd = 10.0
[[repo]]
name = "owner/demo"
readme_paths = ["README.md"]
source_include = ["src/**/*.py", "pyproject.toml"]
managed_sections = ["features", "quick-start", "project-structure", "status"]
publish_mode = "{mode}"
api_export = "allowlist"
"""


def initial_readme() -> str:
    base = "# Demo\n\nHand-written intro that must survive.\n\n## Custom section\n\nKeep me exactly.\n"
    return migrate(base, REPO, "Demo project", ("features", "quick-start", "project-structure", "status"), ())


@pytest.fixture
def env(tmp_path):
    def make(mode="review_pr", files=None, config=CONFIG, responder=None):
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(textwrap.dedent(config).replace("{mode}", mode), encoding="utf-8")
        cfg = load(cfg_path)
        db = DB(tmp_path / "state.db")
        gh = FakeGitHub(files or {
            "README.md": initial_readme(),
            "pyproject.toml": '[project]\nname = "demo"\nrequires-python = ">=3.11"\n[project.scripts]\ndemo = "demo:main"\n',
            "src/demo/app.py": "import os\nTOKEN = os.getenv('DEMO_TOKEN')\n",
            "docs/notes.txt": "irrelevant",
        })
        claude = FakeClaude(responder)
        return SimpleNamespace(cfg=cfg, db=db, gh=gh, claude=claude, home=tmp_path, run=lambda **kw: run_cycle(cfg, db, gh, claude, tmp_path, **kw))

    return make


def run_cycle(cfg, db, gh, claude, home: Path, trigger="test", **kw):
    run_id = db.start_run(trigger, "tester")
    w = Worker(cfg, db, gh, Generator(cfg, db, "sk-test", client=claude, sleep=lambda s: None), home, run_id, trigger=trigger, **kw)
    summary = w.run()
    db.finish_run(run_id, "cancelled" if w.cancelled else "ok", summary)
    return summary
