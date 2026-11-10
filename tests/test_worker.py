"""End-to-end cycle behavior against the in-memory GitHub and Claude fakes."""

import json
import threading

import anthropic
import httpx2
import pytest

from readme_monitor.readme import parse
from readme_monitor.worker import Locked, file_lock

from .conftest import REPO


def repo_state(e):
    return e.db.repo(REPO)


def blocks(text):
    return {b.id: b for b in parse(text)}


def test_first_run_proposes_pr_and_preserves_unmanaged_text(env):
    e = env()
    summary = e.run()
    assert summary == {"awaiting_merge": 1}
    assert len(e.claude.calls) == 1
    bot = e.gh.files("readme-monitor/update")["README.md"]
    assert "Hand-written intro that must survive." in bot
    assert "## Custom section\n\nKeep me exactly.\n" in bot
    b = blocks(bot)
    assert b["features"].content.startswith("Generated features")
    assert "src/" in b["project-structure"].content
    pr = e.gh.prs[1]
    assert pr["state"] == "open" and "features" in pr["body"]
    # main is untouched until a human merges
    assert "Generated" not in e.gh.files()["README.md"]


def test_unchanged_inputs_make_no_claude_request_and_no_git_write(env):
    e = env()
    e.run()
    calls, writes = len(e.claude.calls), list(e.gh.writes)
    e.run()
    e.run()
    assert len(e.claude.calls) == calls
    assert e.gh.writes == writes


def test_relevant_change_updates_only_returned_sections(env):
    e = env(responder=lambda p: {"sections": [{"id": "features", "content": f"Features at {p['revision'][:7]}.",
                                                "evidence": ["F1"], "uncertain": False, "review_notes": ""}]})
    e.run()
    e.gh.merge(1)
    e.run()  # reconcile: published
    before = blocks(e.gh.files()["README.md"])
    e.gh.push_commit({"src/demo/app.py": "import os\nTOKEN = os.getenv('DEMO_TOKEN')\nNEW = 1\n"})
    e.run()
    after = blocks(e.gh.files("readme-monitor/update")["README.md"])
    assert after["features"].content != before["features"].content
    for sid in ("quick-start", "status", "project-structure"):
        assert after[sid].content == before[sid].content


def test_irrelevant_change_and_bot_merge_do_not_loop(env):
    e = env()
    e.run()
    assert len(e.claude.calls) == 1
    e.gh.merge(1)
    e.run()
    assert repo_state(e)["state"] == "published"
    e.gh.push_commit({"docs/notes.txt": "still irrelevant"})  # outside the allowlist and not a doc path
    e.run()
    e.run()
    assert len(e.claude.calls) == 1
    assert [w for w in e.gh.writes if w[0] == "create_pr"] == [("create_pr", 1)]


def test_downtime_with_many_commits_converges_on_one_proposal(env):
    e = env()
    for i in range(5):
        e.gh.push_commit({"src/demo/app.py": f"VERSION = {i}\n"})
    e.run()
    assert len(e.claude.calls) == 1
    assert e.claude.calls[0]["revision"] == e.gh.branches["main"]
    assert len(e.gh.prs) == 1


def test_new_commit_while_pr_open_updates_the_same_pr(env):
    e = env()
    e.run()
    e.gh.push_commit({"src/demo/app.py": "VERSION = 2\n"})
    e.run()
    assert len(e.gh.prs) == 1 and ("update_pr", 1) in e.gh.writes
    assert len(e.claude.calls) == 2


def test_concurrent_run_is_skipped_by_lock(tmp_path):
    lock = tmp_path / "worker.lock"
    with file_lock(lock):
        result = {}

        def other():
            try:
                with file_lock(lock):
                    result["ran"] = True
            except Locked:
                result["skipped"] = True

        t = threading.Thread(target=other)
        t.start()
        t.join()
    assert result == {"skipped": True}
    with file_lock(lock):  # released afterwards
        pass


def test_timeout_keeps_reservation_and_survives_restart(env):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    e = env(responder=lambda p: anthropic.APITimeoutError(request=req))
    e.run()
    rows = e.db.conn.execute("SELECT state, reserved_usd FROM usage").fetchall()
    assert [r["state"] for r in rows] == ["ambiguous", "ambiguous"]  # two bounded attempts
    spent = e.db.spent_since("0")
    assert spent == pytest.approx(sum(r["reserved_usd"] for r in rows))
    assert repo_state(e)["analyzed_fp"] is None  # pending work preserved
    assert repo_state(e)["state"] == "error"
    # a new process sees the same spend
    from readme_monitor.db import DB

    again = DB(e.home / "state.db")
    assert again.spent_since("0") == pytest.approx(spent)


def test_rejected_request_releases_reservation(env):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(400, request=req, json={"error": {"message": "bad"}})
    e = env(responder=lambda p: anthropic.BadRequestError("bad", response=resp, body=None))
    e.run()
    assert [r["state"] for r in e.db.conn.execute("SELECT state FROM usage")] == ["released"]
    assert e.db.spent_since("0") == 0


def test_interrupted_reservation_is_counted_as_ambiguous(env):
    e = env()
    e.db.reserve(REPO, 999, "claude-sonnet-5-5", 0.25)  # left behind by a crashed run
    e.run()
    assert e.db.conn.execute("SELECT state FROM usage WHERE run_id=999").fetchone()[0] == "ambiguous"
    assert e.db.spent_since("0") >= 0.25


def test_budget_exhaustion_pauses_generation_and_keeps_work(env):
    e = env()
    e.db.reserve(REPO, None, "claude-sonnet-5-5", 2.0)
    e.db.set_usage_state(1, "reconciled")
    e.db.conn.execute("UPDATE usage SET actual_usd=2.0")
    e.run()
    assert repo_state(e)["state"] == "budget_paused"
    assert "daily limit" in repo_state(e)["state_detail"]
    assert e.claude.calls == [] and e.gh.writes == []


def test_missing_budget_and_credentials_are_reported(env):
    no_budget = """
[monitor]
quiet_minutes = 0
[[repo]]
name = "owner/demo"
source_include = ["src/**/*.py"]
publish_mode = "review_pr"
api_export = "allowlist"
managed_sections = ["features"]
"""
    e = env(config=no_budget)
    e.run()
    assert repo_state(e)["state"] == "budget_paused"
    assert "no approved budget" in repo_state(e)["state_detail"]
    assert e.claude.calls == []


def test_missing_anthropic_key_state(env):
    from readme_monitor.llm import Generator
    from readme_monitor.worker import Worker

    e = env()
    w = Worker(e.cfg, e.db, e.gh, Generator(e.cfg, e.db, None, client=e.claude), e.home, e.db.start_run("t", "u"))
    w.run()
    assert repo_state(e)["state"] == "credentials_missing"


def test_missing_push_permission_blocks_before_paid_request(env):
    e = env()
    e.gh.push = False
    e.run()
    assert repo_state(e)["state"] == "permission_missing"
    assert e.claude.calls == []


def test_human_edit_in_managed_block_is_not_overwritten(env):
    e = env()
    e.run()
    e.gh.merge(1)
    e.run()
    text = e.gh.files()["README.md"]
    edited = text.replace("Generated status", "A person rewrote status")
    e.gh.push_commit({"README.md": edited, "src/demo/app.py": "CHANGED = True\n"})
    e.run()
    proposal = e.gh.files("readme-monitor/update")["README.md"]
    assert "A person rewrote status" in proposal
    assert "status" in e.gh.prs[2]["body"] and "edited by a person" in e.gh.prs[2]["body"]


def test_stale_source_is_not_published(env, monkeypatch):
    e = env()
    real = e.gh.head_sha
    calls = {"n": 0}

    def moving_head(full, branch):
        calls["n"] += 1
        if calls["n"] == 2:  # the publisher's freshness check sees a new commit
            e.gh.push_commit({"src/demo/app.py": "RACE = 1\n"})
        return real(full, branch)

    monkeypatch.setattr(e.gh, "head_sha", moving_head)
    e.run()
    assert repo_state(e)["state"] == "stale"
    assert not [w for w in e.gh.writes if w[0] == "put_file"]


def test_human_commit_on_bot_branch_is_not_overwritten(env):
    e = env()
    e.run()
    e.gh.push_commit({"README.md": "human edit on the bot branch\n"}, "human", "readme-monitor/update")
    e.gh.push_commit({"src/demo/app.py": "MORE = 1\n"})
    e.run()
    assert repo_state(e)["state"] == "conflict"
    assert e.gh.files("readme-monitor/update")["README.md"] == "human edit on the bot branch\n"


def test_malformed_markers_are_reported(env):
    e = env()
    bad = e.gh.files()["README.md"].replace("<!-- readme-monitor:end:features -->", "")
    e.gh.push_commit({"README.md": bad})
    e.run()
    assert repo_state(e)["state"] == "conflict"
    assert "malformed" in repo_state(e)["state_detail"] or "starts inside" in repo_state(e)["state_detail"]
    assert e.claude.calls == []


def test_invalid_model_output_is_rejected_without_publishing(env):
    e = env(responder=lambda p: {"sections": [{"id": "features", "content": "x", "evidence": ["F999"], "uncertain": False, "review_notes": ""}]})
    e.run()
    assert repo_state(e)["state"] == "error"
    assert "evidence that was not supplied" in repo_state(e)["state_detail"]
    assert e.gh.writes == []


def test_publication_requires_content_on_target_branch(env):
    e = env()
    e.run()
    pr = e.gh.prs[1]
    pr.update(state="closed", merged=True)  # reported merged, but main never received the README
    e.run()
    assert e.db.latest_proposal(REPO)["state"] == "awaiting_merge"
    assert repo_state(e)["state"] == "merged_unconfirmed"
    assert len(e.gh.prs) == 1  # no duplicate proposal while waiting
    e.gh.prs[1].update(state="open", merged=False)
    e.gh.merge(1)
    e.run()
    p = e.db.latest_proposal(REPO)
    assert p["state"] == "published" and p["published_sha"] == e.gh.branches["main"]


def test_closed_pr_is_not_reproposed_for_same_inputs(env):
    e = env()
    e.run()
    e.gh.prs[1].update(state="closed")
    e.run()
    e.run()
    assert repo_state(e)["state"] == "rejected"
    assert len(e.gh.prs) == 1 and len(e.claude.calls) == 1


def test_waits_for_pending_checks_then_generates_once(env):
    e = env()
    head = e.gh.branches["main"]
    e.gh.check_runs[head] = [{"name": "tests", "status": "in_progress", "conclusion": None}]
    e.run()
    assert repo_state(e)["state"] == "waiting_for_checks" and e.claude.calls == []
    e.gh.check_runs[head] = [{"name": "tests", "status": "completed", "conclusion": "success"}]
    e.run()
    assert len(e.claude.calls) == 1
    assert any(f["kind"] == "verification" and "tests: success" in f["value"] for f in e.claude.calls[0]["facts"])


def test_auto_pr_enables_auto_merge_without_review_flags(env):
    e = env(mode="auto_pr")
    e.run()
    assert ("auto_merge", "PR_1") in e.gh.writes


def test_auto_pr_withholds_auto_merge_when_content_needs_review(env):
    e = env(mode="auto_pr", responder=lambda p: {"sections": [{"id": "features", "content": "Maybe works.", "evidence": ["F1"], "uncertain": True, "review_notes": "unsure"}]})
    e.run()
    assert not [w for w in e.gh.writes if w[0] == "auto_merge"]
    assert "withheld" in e.db.latest_proposal(REPO)["detail"]
    assert "unsure" in e.gh.prs[1]["body"]


def test_review_flags_are_listed_once(env):
    e = env()
    e.gh.push_commit({"src/demo/app.py": 'API_KEY = "' + "sk-ant-" + "q" * 30 + '"\n'})
    e.run()
    body = e.gh.prs[1]["body"]
    assert body.count("secret-like value(s) redacted") == 1
    assert "sk-ant-qqq" not in json.dumps(e.claude.calls)


def test_auto_merge_unavailable_is_reported(env):
    e = env(mode="auto_pr")
    e.gh.auto_merge_error = "Auto merge is not allowed for this repository"
    e.run()
    assert "human merge required" in e.db.latest_proposal(REPO)["detail"]


def test_switching_mode_reuses_stored_generation(env):
    """Changing publish_mode (or limits) must publish the stored proposal, not pay again."""
    from dataclasses import replace

    from .conftest import run_cycle

    e = env(mode="preview")
    assert e.run() == {"proposal_created": 1}
    repo = replace(e.cfg.repos[0], publish_mode="review_pr", monthly_usd=5.0)
    cfg = replace(e.cfg, repos=(repo,))
    assert run_cycle(cfg, e.db, e.gh, e.claude, e.home) == {"awaiting_merge": 1}
    assert len(e.claude.calls) == 1 and len(e.gh.prs) == 1


def test_preview_mode_writes_local_files_only(env):
    e = env(mode="preview")
    assert e.run() == {"proposal_created": 1}
    assert e.gh.writes == []
    out = e.home / "previews" / "owner__demo"
    assert (out / "README.md").read_text(encoding="utf-8").count("Generated features") == 1
    assert (out / "README.md.diff").exists()


def test_deleted_repository_backs_off(env):
    e = env()
    e.gh.exists = False
    e.run()
    row = repo_state(e)
    assert row["state"] == "inaccessible" and row["next_attempt_at"]
    assert e.run() == {"backoff": 1}


def test_paused_repo_and_global_pause(env):
    e = env()
    e.db.set_setting("paused", "1")
    assert e.run() == {"paused": 1}
    e.db.set_setting("paused", "0")
    e.db.update_repo(REPO, paused=1)
    assert e.run() == {"paused": 1}
    assert e.gh.reads == 0
