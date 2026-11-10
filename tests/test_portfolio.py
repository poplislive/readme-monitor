"""Account-wide discovery, installation activation guard, fairness, generation policy, shared budget,
adoption and ledger migration. Simulated GitHub and Claude only; no network or credentials."""

import json
import sqlite3
import textwrap
import threading
from datetime import datetime, timedelta, timezone

import pytest

from readme_monitor import cli, discovery, runtime
from readme_monitor.config import ConfigError, load
from readme_monitor.db import DB, SCHEMA
from readme_monitor.github import GitHub, NotFound, RateLimited
from readme_monitor.llm import BudgetBlocked, Generator, pace_limit
from readme_monitor.publish import Publisher
from readme_monitor.readme import parse
from readme_monitor.worker import Worker, file_lock

from .conftest import REPO, FakeClaude, FakeGitHub, run_cycle
from .test_github import Resp, Session

PLAIN = "# {name}\n\nHand-written intro.\n\n## Custom notes\n\nKeep me exactly.\n"


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


class Account:
    """Several simulated repositories behind one credential, plus the account listing."""

    def __init__(self, owner="owner"):
        self.owner = owner
        self.repos: dict[str, FakeGitHub] = {}
        self.listing: dict[int, dict] = {}
        self.quota_low = False
        self.scopes = "repo"
        self.private_count_visible = True
        self.fail_listing: Exception | None = None

    def add(self, name, rid, files=None, **listing) -> FakeGitHub:
        fake = FakeGitHub(files if files is not None else {"README.md": PLAIN.format(name=name.split("/")[1]), "src/app.py": "print(1)\n"})
        fake.id = rid
        self.repos[name.lower()] = fake
        self.listing[rid] = {"id": rid, "full_name": name, "owner": {"login": name.split("/")[0]}, "private": True,
                             "visibility": "private", "size": 10, "default_branch": "main", **listing}
        return fake

    def rename(self, old, new):
        fake = self.repos[old.lower()]
        fake.full_name = new  # GitHub redirects the old name
        self.repos[new.lower()] = fake
        self.listing[fake.id]["full_name"] = new

    def whoami(self):
        mine = [m for m in self.listing.values() if m["owner"]["login"] == self.owner]
        return {"login": self.owner, "public_repos": sum(not m["private"] for m in mine),
                "owned_private_repos": sum(m["private"] for m in mine) if self.private_count_visible else None}

    def owned_repos(self):
        if self.fail_listing:
            raise self.fail_listing
        return list(self.listing.values())

    def repo_by_id(self, rid):
        if rid not in self.listing:
            raise NotFound("gone", 404)
        return self.listing[rid]

    def __getattr__(self, attr):
        def call(full, *a, **k):
            return getattr(self.repos[full.lower()], attr)(full, *a, **k)
        return call


BASE = """
[monitor]
quiet_minutes = 0
min_generation_interval_hours = 0
max_repos_per_run = 10
[budget]
per_run_usd = 1.0
daily_usd = 2.0
monthly_usd = 15.0
[discovery]
owner = "owner"
publish_mode = "{mode}"
adoptions_per_day = {adoptions}
[policy]
export_restricted = ["owner/secret"]
"""


@pytest.fixture
def portfolio(tmp_path):
    def make(mode="review_pr", adoptions=2, extra=""):
        path = tmp_path / "config.toml"
        path.write_text(textwrap.dedent(BASE).format(mode=mode, adoptions=adoptions) + extra, encoding="utf-8")
        cfg, db, acct, claude = load(path), DB(tmp_path / "state.db"), Account(), FakeClaude()

        def cycle(c=None):
            return run_cycle(c or cfg, db, acct, claude, tmp_path)

        class P:
            pass

        p = P()
        p.cfg, p.db, p.acct, p.claude, p.cycle, p.home = cfg, db, acct, claude, cycle, tmp_path
        return p

    return make


# ---- 1. discovery and pagination ----------------------------------------------------------


def test_discovery_reads_every_page_beyond_100_and_ignores_repositories_not_owned(tmp_path):
    def repo(i, owner="owner"):
        return {"id": i, "full_name": f"{owner}/r{i:03}", "owner": {"login": owner}, "private": i % 2 == 0, "size": 5}

    page1 = [repo(i) for i in range(100)]
    page2 = [repo(i) for i in range(100, 199)] + [repo(999, "someone-else")]  # a collaboration, not owned
    page3 = [repo(i) for i in range(199, 205)]
    responses = [
        Resp(200, {"login": "owner", "public_repos": 102, "owned_private_repos": 103}),
        Resp(200, page1, {"Link": '<https://api.github.com/user/repos?page=2>; rel="next"'}),
        Resp(200, page2, {"Link": '<https://api.github.com/user/repos?page=3>; rel="next"'}),
        Resp(200, page3),
    ]
    s = Session(responses)
    gh = GitHub("t", session=s, sleep=lambda s: None)
    owned, coverage = discovery.list_owned(gh, "owner")
    assert len(owned) == 205 and coverage["complete"] and coverage["skipped_not_owned"] == 1
    assert s.sent[1].params["affiliation"] == "owner" and s.sent[1].params["visibility"] == "all"
    cfg = load(_cfg(tmp_path, '[discovery]\nowner = "owner"\n'))
    db = DB(tmp_path / "s.db")
    assert discovery.reconcile(db, cfg, gh, owned, coverage)["new"] == 205
    assert len(discovery.effective_repos(cfg, db)) == 205


def _cfg(tmp_path, body):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_new_renamed_transferred_and_archived_repositories_follow_policy(portfolio):
    p = portfolio(mode="preview")
    p.acct.add("owner/a", 1)
    p.acct.add("owner/b", 2)
    p.acct.add("owner/c", 3)
    discovery.refresh(p.db, p.cfg, p.acct)
    p.db.update_repo("owner/b", state="awaiting_merge", pending_since=ago(hours=3))
    uid = p.db.reserve("owner/b", None, "claude-sonnet-5-5", 0.3)
    p.db.reconcile(uid, 0.2, {}, None)
    # b renamed, c transferred away, a archived, d created
    p.acct.rename("owner/b", "owner/b2")
    p.acct.listing[3]["owner"] = {"login": "elsewhere"}
    p.acct.listing[3]["full_name"] = "elsewhere/c"
    p.acct.listing[1]["archived"] = True
    p.acct.repos["owner/a"].archived = True
    p.acct.add("owner/d", 4)
    summary = discovery.refresh(p.db, p.cfg, p.acct)
    assert summary["new"] == 1 and summary["renamed"] == 1 and summary["transferred"] == 1
    b2 = p.db.repo("owner/b2")
    assert b2["repo_id"] == 2 and b2["state"] == "awaiting_merge" and b2["pending_since"]
    assert p.db.spent_since("0", repo="owner/b2") == pytest.approx(0.2)  # spending moved with the rename
    assert p.db.spent_since("0") == pytest.approx(0.2)
    names = {r.name for r in discovery.effective_repos(p.cfg, p.db)}
    assert names == {"owner/a", "owner/b2", "owner/d"}  # the transferred repository is no longer processed
    assert discovery.disposition(p.cfg, dict(p.db.repo("owner/a")), None)[0] == "archived"
    assert discovery.disposition(p.cfg, dict(p.db.repo("owner/c")), None)[0] == "blocked"
    p.cycle()
    assert p.db.repo("owner/a")["state"] == "archived" and p.acct.repos["owner/a"].writes == []


def test_worker_never_follows_a_transfer_redirect(portfolio):
    p = portfolio(mode="review_pr")
    fake = p.acct.add("owner/a", 1)
    discovery.refresh(p.db, p.cfg, p.acct)
    fake.full_name = "elsewhere/a"  # GitHub redirects; the worker must stop
    p.db.set_setting("discovery", json.dumps({"at": datetime.now(timezone.utc).isoformat()}))  # not due
    assert p.cycle().get("transferred") == 1
    assert fake.writes == [] and p.db.repo("owner/a")["presence"] == "transferred"
    assert p.cycle() == {}  # dropped from processing


# ---- 2. incomplete inventory ---------------------------------------------------------------


def test_interrupted_discovery_changes_nothing_and_cycles_continue(portfolio):
    p = portfolio(mode="preview")
    p.acct.add("owner/a", 1)
    p.acct.add("owner/b", 2)
    discovery.refresh(p.db, p.cfg, p.acct)
    p.db.set_setting("discovery", json.dumps({"at": ago(days=1), "owner": "owner", "listed": 2, "complete": True, "limitations": []}))
    p.acct.fail_listing = RateLimited("rate limit", 403)
    summary = p.cycle()
    assert summary["discovery_failed"] == 1
    assert {r["presence"] for r in p.db.repos()} == {"present"}  # nothing marked missing
    assert "rate limit" in json.loads(p.db.get_setting("discovery"))["last_error"]
    assert sum(v for k, v in summary.items() if k != "discovery_failed") == 2  # both still checked


def test_limited_credential_never_claims_complete_coverage(portfolio):
    p = portfolio()
    p.acct.add("owner/a", 1)
    p.acct.private_count_visible = False  # GitHub hides the count without the `user` scope
    _, coverage = discovery.list_owned(p.acct, "owner")
    assert coverage["complete"]  # classic token with `repo` scope: every owned repository is visible
    p.acct.scopes = None  # fine-grained token: no scope header, possibly limited to selected repositories
    _, coverage = discovery.list_owned(p.acct, "owner")
    assert not coverage["complete"] and "cannot be proven" in coverage["limitations"][0]
    p.acct.private_count_visible = True
    real = p.acct.whoami
    p.acct.whoami = lambda: {**real(), "owned_private_repos": 5}  # some private repositories are invisible
    _, coverage = discovery.list_owned(p.acct, "owner")
    assert not coverage["complete"] and "cannot see the rest" in coverage["limitations"][0]


def test_credential_for_another_account_is_refused(portfolio):
    p = portfolio()
    p.acct.whoami = lambda: {"login": "intruder"}
    with pytest.raises(Exception, match="belongs to 'intruder'"):
        discovery.list_owned(p.acct, "owner")


# ---- 3. export authorization ---------------------------------------------------------------


def test_discovered_repositories_send_nothing_to_claude(portfolio):
    p = portfolio(mode="review_pr", adoptions=5)
    managed = "# x\n\n## Features\n\n<!-- readme-monitor:start:features -->\nold\n<!-- readme-monitor:end:features -->\n"
    p.acct.add("owner/a", 1, files={"README.md": managed, "src/app.py": "x = 1\n"})
    p.acct.add("owner/b", 2)
    p.cycle()
    p.cycle()
    assert p.claude.calls == []
    assert p.db.conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0


def test_export_restriction_survives_defaults_bulk_enrollment_and_rename(portfolio, tmp_path):
    with pytest.raises(ConfigError, match="export-restricted"):
        load(_cfg(tmp_path, '[policy]\nexport_restricted = ["o/secret"]\n[[repo]]\nname = "o/secret"\n'
                            'api_export = "allowlist"\nsource_include = ["**"]\n'))
    p = portfolio()
    p.acct.add("owner/secret", 7)
    discovery.refresh(p.db, p.cfg, p.acct)
    assert p.db.repo("owner/secret")["restricted"] == 1
    p.acct.rename("owner/secret", "owner/renamed")
    discovery.refresh(p.db, p.cfg, p.acct)
    assert p.db.repo("owner/renamed")["restricted"] == 1  # by repository id, not by name
    # Someone enrolls the new name with an allowlist: the stored restriction still wins.
    extra = '\n[[repo]]\nname = "owner/renamed"\napi_export = "allowlist"\nsource_include = ["**"]\npublish_mode = "review_pr"\n'
    cfg = load(_cfg(tmp_path, textwrap.dedent(BASE).format(mode="review_pr", adoptions=2) + extra))
    repo = next(r for r in discovery.effective_repos(cfg, p.db) if r.name == "owner/renamed")
    assert repo.api_export == "none"
    gen = Generator(cfg, p.db, "sk-test", client=p.claude)
    from dataclasses import replace

    with pytest.raises(BudgetBlocked, match="not approved"):
        gen.generate(replace(repo, api_export="allowlist"), None, {"requested_sections": ["features"]}, [])
    assert p.claude.calls == [] and p.db.conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0


# ---- 4/5. activation and single runtime ------------------------------------------------


@pytest.mark.real_host
@pytest.mark.parametrize("argv", [
    ["run-once"], ["run-once", "--trigger", "scheduled"], ["preview", "--repo", "o/r", "--api"], ["init", "--repo", "o/r"],
    ["install-task"], ["run-task-now"], ["discover"],
])
def test_live_commands_refuse_an_unactivated_folder_before_credentials_or_state(tmp_path, monkeypatch, capsys, argv):
    asked = []
    monkeypatch.setattr(cli.credentials, "get", lambda kind: asked.append(kind) or ("secret", "test"))
    _cfg(tmp_path, '[discovery]\nowner = "o"\n[[repo]]\nname = "o/r"\n')
    assert cli.main([*argv, "--home", str(tmp_path)]) == 3
    assert "is refused" in capsys.readouterr().out
    assert asked == [] and not (tmp_path / "state.db").exists()


@pytest.mark.real_host
def test_generator_and_publisher_boundaries_refuse_an_unactivated_folder(env, monkeypatch):
    e = env()
    gen = Generator(e.cfg, e.db, "sk-test", client=e.claude)
    payload = {"requested_sections": ["features"], "facts": [], "files": [], "current_sections": {}, "readme_out_of_blocks": ""}
    with pytest.raises(runtime.HostNotAuthorized):
        gen.generate(e.cfg.repos[0], None, payload, [])
    with pytest.raises(runtime.HostNotAuthorized):
        gen.estimate(payload, exact=True)  # token counting sends the payload too
    with pytest.raises(runtime.HostNotAuthorized):
        Publisher(e.cfg, e.db, e.gh).publish(e.cfg.repos[0], "sha", "main", {"README.md": ("x", None)}, 1, {}, {}, [])
    assert e.claude.calls == [] and e.gh.writes == []
    assert e.db.conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0


@pytest.fixture
def windows_pc(tmp_path, monkeypatch):
    """A simulated Windows computer with a fixed MachineGuid; returns a runtime folder on it."""
    monkeypatch.setattr(runtime, "hostname", lambda: "WORKSTATION-1")
    monkeypatch.setattr(runtime, "machine_guid", lambda: "guid-1")
    monkeypatch.setattr(runtime, "WINDOWS", True)
    return (tmp_path / "runtime").resolve()


@pytest.mark.real_host
def test_activation_enables_only_the_activated_folder_on_this_machine(windows_pc, tmp_path, monkeypatch):
    with pytest.raises(runtime.HostNotAuthorized, match="not enabled"):
        runtime.require_active(windows_pc)
    runtime.activate(windows_pc, "WORKSTATION-1\\someone")
    runtime.require_active(windows_pc)  # passes
    with pytest.raises(runtime.NotActivated, match="not enabled"):
        runtime.require_active(tmp_path / "second-ledger")
    monkeypatch.setattr(runtime, "machine_guid", lambda: "guid-2")  # the folder copied to another computer
    with pytest.raises(runtime.NotActivated, match="another computer"):
        runtime.require_active(windows_pc)


@pytest.mark.real_host
def test_conflicting_home_or_config_cannot_open_a_second_ledger(windows_pc, tmp_path, monkeypatch, capsys):
    runtime.activate(windows_pc, "WORKSTATION-1\\someone")
    _cfg(windows_pc, '[[repo]]\nname = "o/r"\n')
    other = tmp_path / "other"
    _cfg(other, '[[repo]]\nname = "o/r"\n')
    monkeypatch.setattr(cli.credentials, "get", lambda kind: (None, "missing"))
    assert cli.main(["run-once", "--home", str(other)]) == 3
    assert cli.main(["run-once", "--home", str(windows_pc), "--config", str(other / "config.toml")]) == 3
    monkeypatch.setenv("READMEMONITOR_HOME", str(other))
    assert cli.main(["preview", "--repo", "o/r", "--api"]) == 3
    assert not (other / "state.db").exists()
    out = capsys.readouterr().out
    assert "not enabled" in out and "--config points elsewhere" in out
    monkeypatch.delenv("READMEMONITOR_HOME")
    with pytest.raises(SystemExit, match="set-credential github"):  # the activated folder passes the guard
        cli.main(["run-once", "--home", str(windows_pc)])


@pytest.mark.real_host
def test_offline_commands_still_work_on_an_unactivated_folder(tmp_path, monkeypatch, capsys):
    acct = Account()
    acct.add("owner/a", 1)
    monkeypatch.setattr(cli, "GitHub", lambda token: acct)
    monkeypatch.setattr(cli.credentials, "get", lambda kind: ("token", "test"))
    _cfg(tmp_path, '[discovery]\nowner = "owner"\n')
    assert cli.main(["discover", "--dry-run", "--home", str(tmp_path)]) == 0
    assert cli.main(["status", "--home", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "owner/a" in out and "Installation: not active (" in out
    assert list((tmp_path / "inventory").glob("inventory-*.json"))


# ---- 6. fairness -------------------------------------------------------------------------


def test_rotation_covers_every_repository_across_cycles_and_restarts(portfolio):
    p = portfolio(mode="preview")
    for i in range(25):
        p.acct.add(f"owner/r{i:02}", i + 1)
    seen = []
    for _ in range(3):
        p.cycle()
        seen.append({r["name"] for r in p.db.repos() if r["last_checked_at"]})
    assert len(seen[0]) == 10 and len(seen[1]) == 20 and len(seen[2]) == 25
    first = {r["name"]: r["last_checked_at"] for r in p.db.repos()}
    # restart: a new process opens the same database and continues the rotation
    p.db.close()
    db = DB(p.home / "state.db")
    for r in db.repos():  # make the order unambiguous within the same second
        db.update_repo(r["name"], last_checked_at=ago(minutes=60 - int(r["name"][-2:])))
    oldest = sorted(first, key=lambda n: (db.repo(n)["last_checked_at"]))[:10]
    run_cycle(p.cfg, db, p.acct, p.claude, p.home)
    assert all(db.repo(n)["last_checked_at"] > ago(minutes=1) for n in oldest)


def test_failing_repositories_cannot_monopolize_slots(portfolio):
    p = portfolio(mode="preview")
    for i in range(20):
        fake = p.acct.add(f"owner/r{i:02}", i + 1)
        fake.exists = i >= 12  # twelve repositories fail
    discovery.refresh(p.db, p.cfg, p.acct)
    p.cycle()
    p.cycle()
    summary = p.cycle()
    assert summary["backoff"] >= 10  # failures wait outside the slots
    healthy = [r for r in p.db.repos() if r["state"] != "inaccessible"]
    assert len(healthy) == 8 and all(r["last_checked_at"] for r in healthy)


# ---- generation policy -------------------------------------------------------------------


POLICY = """
[monitor]
check_wait_minutes = 30
quiet_minutes = 30
min_generation_interval_hours = 12
max_postpone_hours = 24
[budget]
per_run_usd = 1.0
daily_usd = 2.0
monthly_usd = 10.0
[[repo]]
name = "owner/demo"
source_include = ["src/**/*.py", "pyproject.toml"]
managed_sections = ["features", "quick-start", "project-structure", "status"]
publish_mode = "review_pr"
api_export = "allowlist"
"""


def test_quiet_period_coalesces_changes_then_generates_once(env):
    e = env(config=POLICY)
    assert e.run() == {"generation_deferred": 1} and e.claude.calls == []
    row = e.db.repo(REPO)
    assert "quiet minutes" in row["state_detail"] and row["pending_since"] and row["next_attempt_at"]
    e.db.update_repo(REPO, head_changed_at=ago(minutes=31), next_attempt_at=None)
    assert e.run() == {"awaiting_merge": 1} and len(e.claude.calls) == 1
    row = e.db.repo(REPO)
    assert row["pending_since"] is None and row["last_generated_at"]


def test_constant_commits_cannot_postpone_generation_forever(env):
    e = env(config=POLICY)
    e.run()
    e.db.update_repo(REPO, pending_since=ago(hours=25), next_attempt_at=None)
    e.gh.push_commit({"src/demo/app.py": "BUSY = 1\n"})  # the branch just moved again
    assert e.run() == {"awaiting_merge": 1} and len(e.claude.calls) == 1


def test_minimum_interval_between_paid_generations(env):
    e = env(config=POLICY)
    e.db.update_repo(REPO, last_generated_at=ago(hours=1), head_sha=e.gh.branches["main"], head_changed_at=ago(hours=2))
    e.run()
    row = e.db.repo(REPO)
    assert row["state"] == "generation_deferred" and "per repository" in row["state_detail"] and e.claude.calls == []


def test_oldest_pending_repository_is_funded_first(env):
    e = env()
    e.db.update_repo("owner/older", state="budget_paused", pending_since=ago(hours=5))
    w = Worker(e.cfg, e.db, e.gh, Generator(e.cfg, e.db, "sk-test", client=e.claude), e.home, e.db.start_run("t", "u"))
    w.active = {REPO, "owner/older"}  # both are being processed
    assert w.process(e.cfg.repos[0]) == "budget_paused"
    row = e.db.repo(REPO)
    assert "queued behind owner/older" in row["state_detail"] and row["pending_since"] and e.claude.calls == []
    # A repository that is no longer processed (excluded, removed, transferred) cannot hold the queue.
    assert e.run() == {"awaiting_merge": 1} and len(e.claude.calls) == 1


# ---- 7. shared budget --------------------------------------------------------------------


def test_reservation_check_is_atomic_across_processes(tmp_path):
    DB(tmp_path / "s.db").close()
    limits = [("monthly", "0", None, None, 1.0)]
    results = []
    barrier = threading.Barrier(2)

    def worker(repo):
        db = DB(tmp_path / "s.db")
        barrier.wait()
        results.append(db.try_reserve(repo, None, "m", 0.6, limits))
        db.close()

    threads = [threading.Thread(target=worker, args=(f"o/{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r[0] is None for r in results) == [False, True]  # exactly one fits
    assert DB(tmp_path / "s.db").spent_since("0") == pytest.approx(0.6)


def test_limits_are_shared_across_repositories(env):
    e = env()
    uid = e.db.reserve("owner/another-repo", None, "claude-sonnet-5-5", 1.99)
    e.db.reconcile(uid, 1.99, {}, None)
    e.run()
    row = e.db.repo(REPO)
    assert row["state"] == "budget_paused" and "daily limit" in row["state_detail"]
    assert e.claude.calls == [] and row["pending_since"]


def test_every_readme_and_attempt_counts_against_one_run(env):
    two = """
[monitor]
quiet_minutes = 0
[budget]
per_run_usd = 1.0
daily_usd = 2.0
monthly_usd = 10.0
[[repo]]
name = "owner/demo"
readme_paths = ["README.md", "docs/README.md"]
source_include = ["src/**/*.py"]
managed_sections = ["features"]
publish_mode = "review_pr"
api_export = "allowlist"
"""
    from .conftest import initial_readme

    e = env(config=two, files={"README.md": initial_readme(), "docs/README.md": initial_readme(), "src/a.py": "A = 1\n"})
    e.run()
    rows = e.db.conn.execute("SELECT run_id FROM usage").fetchall()
    assert len(rows) == 2 and len({r[0] for r in rows}) == 1


def test_even_pacing_spreads_the_monthly_allowance(tmp_path):
    cfg = load(_cfg(tmp_path, "[budget]\nper_run_usd = 0.5\ndaily_usd = 2.0\nmonthly_usd = 15.0\n"))
    assert pace_limit(cfg, datetime(2026, 11, 1, tzinfo=timezone.utc)) == pytest.approx(1.0)
    assert pace_limit(cfg, datetime(2026, 11, 15, tzinfo=timezone.utc)) == pytest.approx(8.0)
    assert pace_limit(cfg, datetime(2026, 11, 30, tzinfo=timezone.utc)) == pytest.approx(15.0)


def test_paid_preview_takes_the_worker_lock(tmp_path, capsys):
    _cfg(tmp_path, '[[repo]]\nname = "o/r"\n')
    with file_lock(tmp_path / "worker.lock"):
        assert cli.main(["preview", "--repo", "o/r", "--api", "--home", str(tmp_path)]) == 1
    assert "another ReadmeMonitor run holds" in capsys.readouterr().out


# ---- 9/10. adoption and publication ------------------------------------------------------


def test_adoption_opens_one_bounded_batch_of_review_prs(portfolio):
    p = portfolio(mode="review_pr", adoptions=2)
    for i, name in enumerate(("owner/a", "owner/b", "owner/c")):
        p.acct.add(name, i + 1)
    summary = p.cycle()
    assert summary["awaiting_merge"] == 2 and summary["adoption_deferred"] == 1
    a = p.acct.repos["owner/a"]
    text = a.files("readme-monitor/update")["README.md"]
    assert "Hand-written intro." in text and "## Custom notes\n\nKeep me exactly.\n" in text
    blocks = {b.id: b for b in parse(text)}
    assert "src/" in blocks["project-structure"].content  # real content, not a placeholder
    assert "features" not in blocks  # nothing Claude would have to write
    p.cycle()
    assert len(a.prs) == 1
    a.push_commit({"src/app.py": "print(2)\n"})
    p.cycle()
    assert len(a.prs) == 1 and ("update_pr", 1) in a.writes  # the same PR is refreshed, not a competitor
    a.merge(1)
    p.cycle()
    assert p.db.repo("owner/a")["state"] == "published" and p.db.repo("owner/a")["last_published_at"]


def test_adoption_of_discovered_repository_leaves_hand_written_sections_alone(portfolio):
    p = portfolio(mode="review_pr")
    readme = "# x\n\nIntro.\n\n## Changelog\n\n- v1: hand-written notes\n\n## License\n\nMIT\n"
    fake = p.acct.add("owner/a", 1, files={"README.md": readme, "a.py": "1\n"})
    full = "# y\n\n## Project structure\n\nmine\n\n## Releases\n\nmine too\n"
    other = p.acct.add("owner/b", 2, files={"README.md": full, "a.py": "1\n"})
    p.cycle()
    text = fake.files("readme-monitor/update")["README.md"]
    assert "## Changelog\n\n- v1: hand-written notes\n" in text  # not wrapped, so never replaced
    assert [b.id for b in parse(text)] == ["project-structure"]
    assert text.index("## Project structure") < text.index("## Changelog")  # standard order
    # Nothing left to adopt: settled, not a permanent "awaiting adoption".
    assert p.db.repo("owner/b")["state"] == "no_changes" and other.writes == []


def test_missing_readme_gets_a_minimal_evidence_based_proposal(portfolio):
    p = portfolio(mode="review_pr")
    fake = p.acct.add("owner/bare", 1, files={"main.go": "package main\n", "go.mod": "module x\n"})
    p.cycle()
    text = fake.files("readme-monitor/update")["README.md"]
    assert text.startswith("# bare\n\nDemo project\n")
    assert "## Project structure" in text and "go.mod" in text
    assert "Quick start" not in text and "Features" not in text  # nothing invented


def test_actual_readme_filename_case_is_used(portfolio):
    p = portfolio(mode="review_pr")
    lower = p.acct.add("owner/lower", 1, files={"readme.md": "# lower\n\nText.\n", "x.py": "1\n"})
    rst = p.acct.add("owner/rst", 2, files={"README.rst": "lower\n=====\n", "x.py": "1\n"})
    p.cycle()
    assert "readme.md" in lower.files("readme-monitor/update") and "README.md" not in lower.files("readme-monitor/update")
    assert p.db.repo("owner/rst")["state"] == "unsupported_readme" and rst.writes == []


def test_empty_and_archived_repositories_wait_without_writes(portfolio):
    p = portfolio(mode="review_pr")
    empty = p.acct.add("owner/empty", 1)
    empty.empty = True
    old = p.acct.add("owner/old", 2)
    old.archived = True
    p.cycle()
    assert p.db.repo("owner/empty")["state"] == "empty" and p.db.repo("owner/old")["state"] == "archived"
    assert empty.writes == [] and old.writes == []


def test_closed_adoption_pr_is_not_reopened_automatically(portfolio):
    p = portfolio(mode="review_pr")
    fake = p.acct.add("owner/a", 1)
    p.cycle()
    fake.prs[1].update(state="closed")
    p.cycle()
    fake.push_commit({"src/app.py": "print(3)\n"})
    p.cycle()
    row = p.db.repo("owner/a")
    assert len(fake.prs) == 1 and row["state"] == "needs_init" and "closed" in row["state_detail"]


def test_malformed_markers_in_a_discovered_readme_fail_safely(portfolio):
    p = portfolio(mode="review_pr")
    bad = "# x\n\n<!-- readme-monitor:start:project-structure -->\nunclosed\n"
    fake = p.acct.add("owner/a", 1, files={"README.md": bad, "a.py": "1\n"})
    p.cycle()
    assert p.db.repo("owner/a")["state"] == "conflict" and fake.writes == []


# ---- 11. migration of the ledger ---------------------------------------------------------


def test_schema1_database_is_backed_up_and_migrated_with_spend_intact(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)  # the schema-1 tables
    conn.execute("INSERT INTO usage(repo, model, state, reserved_usd, actual_usd, created_at, updated_at) "
                 "VALUES('o/r','m','reconciled',0.3,0.125,'2026-11-10T01:10:21+00:00','x')")
    conn.execute("INSERT INTO repos(name, state) VALUES('o/r', 'awaiting_merge')")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()
    db = DB(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert db.spent_since("0") == pytest.approx(0.125) and db.repo("o/r")["state"] == "awaiting_merge"
    assert "pending_since" in db.repo("o/r").keys()
    backup = next((tmp_path / "backups").glob("pre-schema3-*.db"))
    assert sqlite3.connect(backup).execute("PRAGMA user_version").fetchone()[0] == 1


def test_restoring_an_older_copy_never_erases_newer_spend(tmp_path):
    db = DB(tmp_path / "state.db")
    a = db.reserve("o/r", 1, "m", 0.3)
    db.reconcile(a, 0.2, {}, None)
    old = tmp_path / "old.db"
    db.backup_to(old)
    db.reserve("o/r", 2, "m", 0.25)  # newer, still in flight when the old copy is restored
    before = db.spent_since("0")
    assert db.restore_from(old) == 1
    assert db.spent_since("0") == pytest.approx(before)
    assert [r["state"] for r in db.conn.execute("SELECT state FROM usage ORDER BY id")] == ["reconciled", "ambiguous"]
    assert sqlite3.connect(old).execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 1  # source untouched


def test_restore_db_command_reports_carried_spend(tmp_path, capsys):
    _cfg(tmp_path, '[[repo]]\nname = "o/r"\n')
    db = DB(tmp_path / "state.db")
    old = tmp_path / "old.db"
    db.backup_to(old)
    db.reserve("o/r", 1, "m", 0.4)
    db.close()
    assert cli.main(["restore-db", str(old), "--home", str(tmp_path)]) == 0
    assert "Kept 1 spending record" in capsys.readouterr().out


# ---- status ------------------------------------------------------------------------------


def test_status_reports_every_repository_and_deferred_work(portfolio, capsys):
    p = portfolio(mode="review_pr", adoptions=1)
    p.acct.add("owner/a", 1)
    p.acct.add("owner/b", 2)
    p.acct.add("owner/secret", 3)
    p.cycle()
    p.db.update_repo("owner/a", pending_since=ago(hours=26))
    assert cli.main(["status", "--json", "--home", str(p.home)]) == 0
    data = json.loads(capsys.readouterr().out)
    names = {r["name"]: r for r in data["repos"]}
    assert set(names) == {"owner/a", "owner/b", "owner/secret"}
    assert data["discovery"]["complete"] and data["installation"]["active"] is False and data["installation"]["problem"]
    assert data["deferred"][0]["name"] == "owner/a" and data["deferred"][0]["age"].startswith("1d")
    assert p.db.repo("owner/secret")["restricted"] == 1 and names["owner/secret"]["api_export"] == "none"
    assert sum(data["counts"].values()) == 3
    assert data["budget"]["scope"].startswith("calls made by this monitor")
