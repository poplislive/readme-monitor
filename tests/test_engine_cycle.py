"""Events of a real cycle against the fakes, direct publication, dry runs, cancellation,
overlapping triggers and the shipped simulation."""

import json

import anthropic
import httpx2
import pytest

from readme_monitor.llm import Generator

from readme_monitor.worker import Worker

from readme_monitor import cli
from readme_monitor.github import GitHubError
from readme_monitor.publish import Publisher
from readme_monitor.simulation import FakeAccount, FakeClaude, FakeGitHub, blob_sha, build_demo
from readme_monitor.worker import file_lock

from .conftest import REPO, initial_readme, run_cycle

TOKEN = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0" * 2


def repo_events(e, run_id=None):
    return [ev for ev in e.db.events(limit=500, run_id=run_id) if ev["repo"] == REPO]


def last_run(e):
    return e.db.last_runs(1)[0]["id"]


def result(e):
    return [ev for ev in repo_events(e, last_run(e)) if ev["stage"] == "result"][-1]["data"]


# ---- events --------------------------------------------------------------------------------


def test_cycle_events_traverse_every_stage_in_order_with_honest_data(env):
    e = env()
    e.run()
    evs = repo_events(e)
    stages = [(ev["stage"], ev["status"]) for ev in evs]
    assert stages == [("inspect", "done"), ("evidence", "done"), ("draft", "started"), ("draft", "done"), ("validate", "done"),
                      ("publish", "started"), ("publish", "done"), ("await", "info"), ("result", "done")]
    run_ev = e.db.events(run_id=last_run(e))
    assert run_ev[0]["stage"] == "run" and run_ev[0]["status"] == "started" and run_ev[-1]["stage"] == "run"
    ins = evs[0]["data"]
    head = e.gh.branches["main"]
    assert ins["first_scan"] and ins["baseline"] is None and [c["sha"] for c in ins["commits"]] == [head]
    assert evs[1]["data"]["files_read"] == ["pyproject.toml", "src/demo/app.py"]  # loaded, allowlisted only
    assert evs[3]["data"]["usage"] == {"input_tokens": 1000, "output_tokens": 200, "cost_usd": pytest.approx(0.004)}
    draft = e.db.draft(evs[4]["data"]["draft_id"])
    assert draft["kind"] == "proposal" and draft["proposal_id"] == e.db.latest_proposal(REPO)["id"]
    assert "features" in draft["files"]["README.md"]["changed"] and draft["files"]["README.md"]["diff"]
    assert evs[6]["data"]["pr_url"].endswith("/pull/1") and evs[7]["data"]["waiting_for"] == "merge"
    assert evs[8]["data"]["outcome"] == "awaiting_merge" and all(ev["trigger"] == "test" for ev in evs)
    assert all(ev["revision"] == head for ev in evs[:8])


def test_next_scan_compares_against_the_last_analyzed_revision(env):
    e = env()
    e.run()
    base = e.gh.branches["main"]
    e.gh.compare_page = 1
    for i in range(3):
        e.gh.push_commit({"src/demo/app.py": f"V = {i}\n"}, f"change {i}")
    e.run()
    ins = repo_events(e, last_run(e))[0]["data"]
    assert ins["baseline"] == base and ins["compare_status"] == "ahead" and ins["commits_total"] == 3
    assert len(ins["commits"]) == 1 and ins["truncated"]
    assert {f["path"] for f in ins["files_changed"]} == {"src/demo/app.py"}


def test_rewritten_history_is_reported_as_diverged(env):
    e = env()
    e.run()
    e.gh.merge(1)
    e.run()
    e.gh.rewrite({"src/demo/app.py": "REWRITTEN = 1\n"})
    e.run()
    ins = repo_events(e, last_run(e))[0]["data"]
    assert ins["compare_status"] == "diverged" and ins["commits_total"] >= 1


def test_compare_failure_never_fails_the_cycle(env, monkeypatch):
    e = env()

    def broken(*a):
        raise GitHubError("HTTP 500", 500)

    monkeypatch.setattr(e.gh, "commit", broken)
    assert e.run() == {"awaiting_merge": 1}
    assert repo_events(e)[0]["data"]["compare_status"] == "unavailable"


def test_unchanged_cycles_make_no_request_no_write_and_no_repository_event(env):
    e = env()
    e.run()
    calls, writes = len(e.claude.calls), list(e.gh.writes)
    e.run()
    assert len(e.claude.calls) == calls and e.gh.writes == writes
    evs = e.db.events(run_id=last_run(e))
    assert [(ev["stage"], ev["status"]) for ev in evs] == [("run", "done")]


@pytest.mark.parametrize("case", ["malformed", "person_edit", "moved_branch", "no_permission", "provider_down", "no_budget"])
def test_failure_modes_give_the_right_result_and_state(env, monkeypatch, case):
    responder = None
    if case == "person_edit":  # the same answer for every revision, so only the person's edit differs
        responder = lambda p: {"sections": [{"id": s, "content": f"Generated {s}.", "evidence": [], "uncertain": False,  # noqa: E731
                                             "review_notes": ""} for s in p["requested_sections"]]}
    if case == "malformed":
        responder = lambda p: "not json"  # noqa: E731
    if case == "provider_down":
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        responder = lambda p: anthropic.InternalServerError("down", response=httpx2.Response(500, request=req), body=None)  # noqa: E731
    e = env(responder=responder)
    if case == "person_edit":
        e.run()
        e.gh.merge(1)
        e.run()
        text = e.gh.files()["README.md"].replace("Generated status", "A person rewrote status")
        e.gh.push_commit({"README.md": text})
    if case == "moved_branch":
        real, n = e.gh.head_sha, {"i": 0}

        def moving(full, branch):
            n["i"] += 1
            if n["i"] == 2:
                e.gh.push_commit({"src/demo/app.py": "RACE = 1\n"})
            return real(full, branch)

        monkeypatch.setattr(e.gh, "head_sha", moving)
    if case == "no_permission":
        e.gh.push = False
    if case == "no_budget":
        uid = e.db.reserve("owner/other", None, "claude-sonnet-5-5", 2.0)
        e.db.reconcile(uid, 2.0, {}, None)
    e.run()
    r, state = result(e), e.db.repo(REPO)["state"]
    expected = {"malformed": ("failed", "error"), "person_edit": ("conflict", "conflict"), "moved_branch": ("deferred", "stale"),
                "no_permission": ("blocked", "permission_missing"), "provider_down": ("failed", "error"),
                "no_budget": ("deferred", "budget_paused")}[case]
    assert (r["outcome"], state) == expected
    if case == "malformed":
        assert [ev for ev in repo_events(e, last_run(e)) if ev["stage"] == "validate"][0]["status"] == "failed"
    if case == "provider_down":
        assert any(ev["stage"] == "draft" and ev["status"] == "failed" for ev in repo_events(e, last_run(e)))
    if case == "no_budget":
        msg = [ev for ev in repo_events(e, last_run(e)) if ev["stage"] == "result"][-1]["message"]
        assert "daily limit" in r["detail"] and "daily limit" in msg and e.claude.calls == []
    if case == "person_edit":
        assert "A person rewrote status" in e.gh.files()["README.md"]


def test_a_token_in_a_commit_message_or_error_never_reaches_the_events_table(env, monkeypatch):
    e = env()
    e.run()
    e.gh.push_commit({"src/demo/app.py": "X = 2\n"}, f"oops pasted {TOKEN}")
    e.run()

    def leak(*a):
        raise GitHubError(f"HTTP 502 while using {TOKEN}", 400)

    e.gh.push_commit({"src/demo/app.py": "X = 3\n"})
    monkeypatch.setattr(e.gh, "tree", leak)
    e.run()
    dump = json.dumps([dict(r) for r in e.db.conn.execute("SELECT message, data FROM events")])
    assert TOKEN not in dump and "[REDACTED]" in dump


# ---- direct publication ----------------------------------------------------------------------


def test_direct_mode_commits_once_confirms_and_does_not_loop(env):
    e = env(mode="direct")
    assert e.run() == {"published": 1}
    assert [w[0] for w in e.gh.writes] == ["put_file"] and e.gh.writes[0][2] == "main" and e.gh.prs == {}
    assert "Generated features" in e.gh.files()["README.md"]
    assert "Readme-Monitor-Proposal: 1" in e.gh.commits[e.gh.branches["main"]]["message"]
    p = e.db.latest_proposal(REPO)
    assert p["state"] == "published" and p["published_sha"] == e.gh.branches["main"]
    assert result(e)["outcome"] == "confirmed"
    pub = [ev for ev in repo_events(e) if ev["stage"] == "publish" and ev["status"] == "done"][0]["data"]
    assert pub["commit_sha"] == e.gh.branches["main"] and pub["mode"] == "direct"
    calls, writes = len(e.claude.calls), list(e.gh.writes)
    e.run()  # our own commit moved the head: re-checked, but no model call and no write
    e.run()
    assert len(e.claude.calls) == calls and e.gh.writes == writes
    assert e.db.events(run_id=last_run(e))[-1]["stage"] == "run" and len(e.db.events(run_id=last_run(e))) == 1


def test_direct_mode_with_review_flags_opens_a_pull_request(env):
    e = env(mode="direct", responder=lambda p: {"sections": [{"id": "features", "content": "Maybe.", "evidence": ["F1"],
                                                               "uncertain": True, "review_notes": "unsure"}]})
    assert e.run() == {"awaiting_merge": 1}
    assert len(e.gh.prs) == 1 and "Generated" not in e.gh.files()["README.md"]
    assert e.db.latest_proposal(REPO)["detail"] == "direct commit withheld: content has review flags"


def test_direct_mode_on_a_protected_branch_is_blocked_without_retries(env):
    e = env(mode="direct")
    e.gh.protected = {"main"}
    assert e.run() == {"publish_blocked": 1}
    assert result(e)["outcome"] == "blocked" and "branch protection" in e.db.repo(REPO)["state_detail"]
    assert e.db.latest_proposal(REPO)["state"] == "failed" and e.gh.writes == [] and e.gh.prs == {}
    e.run()
    assert e.gh.writes == [] and len(e.claude.calls) == 1  # nothing tried again until the inputs change


def test_direct_mode_refused_after_a_first_path_records_the_partial_commit(env):
    from .conftest import CONFIG

    files = {"README.md": initial_readme(), "docs/README.md": initial_readme(),
             "pyproject.toml": '[project]\nname = "demo"\n', "src/demo/app.py": "x = 1\n"}
    e = env(mode="direct", files=files, config=CONFIG.replace('readme_paths = ["README.md"]', 'readme_paths = ["README.md", "docs/README.md"]'))
    real = e.gh.put_file

    def refuse_second(full, path, branch, text, message, blob):
        if path == "docs/README.md":
            raise GitHubError("PUT contents: HTTP 409: Repository rule violations found", 409)
        return real(full, path, branch, text, message, blob)

    e.gh.put_file = refuse_second
    assert e.run() == {"publish_blocked": 1}
    first = e.gh.branches["main"]
    p = e.db.latest_proposal(REPO)
    assert p["state"] == "failed" and p["published_sha"] == first
    assert "committed README.md" in p["detail"] and "refused docs/README.md" in p["detail"] and "Nothing was written" not in p["detail"]
    r = result(e)
    assert r["outcome"] == "pushed" and r["commit_sha"] == first and "Nothing was written" not in r["detail"]
    writes, calls = list(e.gh.writes), len(e.claude.calls)
    e.run()
    e.run()
    assert e.gh.writes == writes and len(e.claude.calls) == calls  # neither loops nor commits README.md again


def test_direct_mode_refuses_a_moved_branch(env, monkeypatch):
    e = env(mode="direct")
    real, n = e.gh.head_sha, {"i": 0}

    def moving(full, branch):
        n["i"] += 1
        if n["i"] == 3:  # moves between the publisher's freshness check and the commit
            e.gh.push_commit({"src/demo/app.py": "RACE = 1\n"})
        return real(full, branch)

    monkeypatch.setattr(e.gh, "head_sha", moving)
    assert e.run() == {"stale": 1}
    assert not [w for w in e.gh.writes if w[0] == "put_file"]


def test_lost_answer_of_a_direct_commit_is_reconciled_without_a_second_commit(env, monkeypatch):
    e = env(mode="direct")
    e.gh.lose_write_answer = True
    assert e.run() == {"published": 1}  # detected in the same run by reading the branch back
    assert len([w for w in e.gh.writes if w[0] == "put_file"]) == 1
    # Now the read-back itself also fails: the proposal stays "publishing" until the next run looks.
    e2 = env(mode="direct")
    e2.db.conn.execute("DELETE FROM proposals")
    e2.db.conn.execute("DELETE FROM repos")
    e2.gh.lose_write_answer = True
    real = e2.gh.file_at
    monkeypatch.setattr(e2.gh, "file_at", lambda *a: (_ for _ in ()).throw(GitHubError("HTTP 502", 502)))
    assert e2.run() == {"error": 1}
    assert e2.db.latest_proposal(REPO)["state"] == "publishing"
    monkeypatch.setattr(e2.gh, "file_at", real)
    e2.db.update_repo(REPO, next_attempt_at=None)
    e2.run()
    assert e2.db.latest_proposal(REPO)["state"] == "published"
    assert len([w for w in e2.gh.writes if w[0] == "put_file"]) == 1


def test_adoption_uses_a_pull_request_also_in_direct_mode(env):
    e = env(mode="direct")
    pid = e.db.create_proposal(REPO, "adoption", e.gh.branches["main"], "direct", "publishing", {}, ["initial adoption"])
    pr = Publisher(e.cfg, e.db, e.gh).publish(e.cfg.repos[0], e.gh.branches["main"], "main",
                                              {"README.md": ("# adopted\n", blob_sha(e.gh.files()["README.md"]))},
                                              pid, {"README.md": []}, {}, [])
    assert pr["number"] == 1 and ("create_pr", 1) in e.gh.writes and e.db.proposal(pid)["detail"] is None


# ---- dry run, cancellation, triggers -----------------------------------------------------------


@pytest.mark.real_host  # the real guard: this folder is not activated
def test_dry_run_changes_no_checkpoint_and_writes_nothing(env):
    e = env()
    e.run(trigger="dry_run", dry_run=True)  # works although the installation is not activated
    row = e.db.repo(REPO)
    assert (row["state"], row["analyzed_fp"], row["quick_key"], row["head_sha"], row["generated_llm_fp"]) == ("new", None, None, None, None)
    assert e.db.latest_proposal(REPO) is None and e.gh.writes == [] and e.claude.calls == []
    assert e.db.conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0
    evs = repo_events(e)
    assert all(ev["trigger"] == "dry_run" for ev in evs)
    draft_done = [ev for ev in evs if ev["stage"] == "draft"]
    assert [d["status"] for d in draft_done] == ["done"] and draft_done[0]["data"]["mode"] == "dry_run"
    assert draft_done[0]["data"]["estimate_usd"] > 0
    r = result(e)
    d = e.db.draft(r["draft_id"])
    assert r["outcome"] == "preview" and d["kind"] == "dry_run" and d["estimate_usd"] > 0
    assert d["files"]["README.md"]["changed"] == ["project-structure"]  # no model request: Claude sections stay as they are


def test_dry_run_shows_the_adoption_on_top_of_an_unadopted_readme(env):
    e = env(files={"README.md": "# Demo\n\nPlain text.\n", "src/demo/app.py": "A = 1\n"})
    e.run(trigger="dry_run", dry_run=True)
    d = e.db.draft(result(e)["draft_id"])
    assert any("not adopted yet" in n for n in d["notes"]) and "readme-monitor:start" in d["files"]["README.md"]["new"]
    assert d["files"]["README.md"]["old"] == "# Demo\n\nPlain text.\n" and e.gh.writes == []


def test_cancellation_stops_at_the_next_boundary_and_keeps_pending_work(env):
    holder = {}

    def responder(payload):
        holder["e"].db.request_cancel()  # a person presses Cancel while the model request is in flight
        from readme_monitor.simulation import default_responder

        return default_responder(payload)

    e = env(responder=responder)
    holder["e"] = e
    assert e.run() == {"cancelled": 1}
    assert e.db.last_runs(1)[0]["outcome"] == "cancelled"
    usage = e.db.conn.execute("SELECT state FROM usage").fetchall()
    assert [u[0] for u in usage] == ["reconciled"]  # the request already sent was completed and recorded
    assert e.gh.writes == [] and e.db.repo(REPO)["analyzed_fp"] is None and result(e)["outcome"] == "cancelled"
    assert "cancelled" in e.db.events(run_id=last_run(e))[-1]["message"]
    e.db.clear_cancel()  # the CLI clears the flag when a run finishes
    assert e.run() == {"awaiting_merge": 1}
    assert len(e.claude.calls) == 1  # the stored answer is reused, nothing is paid twice


def test_cancel_once_a_cycle_is_recorded_stops_it_between_repositories(env):
    e = env()
    run_id = e.db.start_run("manual", "tester")  # the run exists, as when Run now has started it
    e.db.request_cancel()
    w = Worker(e.cfg, e.db, e.gh, Generator(e.cfg, e.db, "sk-test", client=e.claude, sleep=lambda s: None), e.home, run_id)
    assert w.run() == {"cancelled": 1}
    assert e.gh.reads == 0


def test_cancel_pressed_while_idle_does_not_stop_the_next_cycle(env):
    e = env()
    e.db.request_cancel()  # nothing was running
    assert e.run() != {"cancelled": 1} and e.gh.reads > 0


def test_overlapping_manual_and_scheduled_triggers_never_duplicate_work(tmp_path, monkeypatch, capsys):
    acct = FakeAccount("owner")
    for n in ("owner/a", "owner/b", "owner/c"):
        acct.add(n, FakeGitHub({"README.md": initial_readme().replace("Demo", n), "src/x.py": "X = 1\n"}))
    cfgtxt = "[monitor]\nquiet_minutes = 0\nmin_generation_interval_hours = 0\n[budget]\nper_run_usd = 1.0\ndaily_usd = 2.0\nmonthly_usd = 10.0\n" + \
        "".join(f'[[repo]]\nname = "{n}"\nsource_include = ["src/*.py"]\nmanaged_sections = ["features", "project-structure"]\n'
                f'publish_mode = "review_pr"\napi_export = "allowlist"\n' for n in ("owner/a", "owner/b", "owner/c"))
    (tmp_path / "config.toml").write_text(cfgtxt, encoding="utf-8")
    from readme_monitor.config import load
    from readme_monitor.db import DB

    cfg, db, claude = load(tmp_path / "config.toml"), DB(tmp_path / "state.db"), FakeClaude()
    for trigger in ("manual", "scheduled", "manual", "scheduled"):
        run_cycle(cfg, db, acct, claude, tmp_path, trigger=trigger)
    assert len(claude.calls) == 3 and db.conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 3
    assert all(len(f.prs) == 1 and len([w for w in f.writes if w[0] == "put_file"]) == 1 for f in acct.repos.values())
    # A start that overlaps a running cycle is skipped by the worker lock, before anything is read.
    monkeypatch.setattr(cli.credentials, "get", lambda kind: ("token", "test"))
    monkeypatch.setattr(cli, "GitHub", lambda token, db=None: acct)
    reads = sum(f.reads for f in acct.repos.values())
    with file_lock(tmp_path / "worker.lock"):
        assert cli.main(["run-once", "--trigger", "scheduled", "--home", str(tmp_path)]) == 0
    assert "Skipped" in capsys.readouterr().out and sum(f.reads for f in acct.repos.values()) == reads


def test_run_once_repo_limits_the_cycle(env):
    e = env()
    assert e.run(only=["OWNER/DEMO"]) == {"awaiting_merge": 1}
    assert e.run(only=["owner/other"]) == {}


# ---- simulation --------------------------------------------------------------------------------


def test_demo_world_runs_the_real_pipeline_through_its_script(tmp_path):
    demo = build_demo(tmp_path / "demo")
    s1 = demo.run_cycle()
    atlas, notes, ledger, trail, garden = (demo.repo(n) for n in ("atlas-api", "field-notes", "pocket-ledger", "trail-maps", "signal-garden"))
    state = lambda n: demo.db.repo(f"octo-demo/{n}")["state"]  # noqa: E731
    assert state("atlas-api") == "awaiting_merge" and len(atlas.prs) == 1
    assert state("pocket-ledger") == "no_changes" and ledger.writes == []
    assert state("signal-garden") == "budget_paused" and "monthly limit" in demo.db.repo("octo-demo/signal-garden")["state_detail"]
    assert s1["no_changes"] >= 2
    assert demo.advance().startswith("A source change")
    demo.run_cycle(trigger="scheduled")
    assert state("field-notes") == "published" and "sketches/" in notes.files()["README.md"]
    assert notes.prs == {} and len(atlas.prs) == 1 and ("update_pr", 1) in atlas.writes
    demo.advance()
    demo.run_cycle()
    assert state("atlas-api") == "published"
    demo.advance()
    demo.run_cycle()
    assert state("trail-maps") == "conflict" and "by hand" in trail.files()["README.md"] and trail.writes == []
    assert demo.advance() is None
    outcomes = {ev["data"]["outcome"] for ev in demo.db.events(limit=500) if ev["stage"] == "result"}
    assert {"awaiting_merge", "confirmed", "deferred", "conflict"} <= outcomes
    before = len(demo.claude.calls)
    demo.run_cycle(dry_run=True)
    assert len(demo.claude.calls) == before and demo.db.conn.execute("SELECT COUNT(*) FROM drafts WHERE kind='dry_run'").fetchone()[0] == 5
