import json
from types import SimpleNamespace

import pytest

from readme_monitor.db import DB
from readme_monitor.github import GitHub, GitHubError, Incomplete, NotFound, RateLimited


class Resp:
    def __init__(self, status=200, data=None, headers=None, text=None):
        self.status_code = status
        self.headers = {"X-RateLimit-Remaining": "4000", "X-RateLimit-Reset": "0", **(headers or {})}
        self._data = data
        self.text = text if text is not None else json.dumps(data)
        self.content = self.text.encode()

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def request(self, method, url, headers, params, json, timeout):
        self.sent.append(SimpleNamespace(method=method, url=url, headers=dict(headers), params=params, body=json))
        return self.responses.pop(0)


def client(responses, db=None):
    s = Session(responses)
    return GitHub("t0k", db=db, session=s, sleep=lambda s: None), s


def test_bot_commits_use_the_accounts_noreply_address_never_its_email():
    me = Resp(200, {"login": "your-account", "id": 42, "email": "person@example.com"})
    gh, s = client([me, Resp(200, {"commit": {"sha": "c1"}}), Resp(200, {"commit": {"sha": "c2"}})])
    assert gh.put_file("o/r", "README.md", "readme-monitor/update", "# x\n", "docs: update", None) == "c1"
    assert gh.put_file("o/r", "README.md", "readme-monitor/update", "# y\n", "docs: update", "b1") == "c2"
    assert [x.method for x in s.sent] == ["GET", "PUT", "PUT"]  # one identity lookup per client
    who = {"name": "your-account", "email": "42+your-account@users.noreply.github.com"}
    assert s.sent[1].body["author"] == s.sent[1].body["committer"] == who and s.sent[2].body["sha"] == "b1"
    assert "person@example.com" not in json.dumps([x.body for x in s.sent])


def test_no_commit_is_made_when_the_account_identity_cannot_be_read():
    gh, s = client([Resp(401, {"message": "Bad credentials"})])
    with pytest.raises(GitHubError):
        gh.put_file("o/r", "README.md", "readme-monitor/update", "# x\n", "docs: update", None)
    assert [x.method for x in s.sent] == ["GET"]  # no write with GitHub's default identity


def test_conditional_request_reuses_cached_body(tmp_path):
    db = DB(tmp_path / "s.db")
    gh, s = client([Resp(200, {"default_branch": "main"}, {"ETag": '"abc"'}), Resp(304, None, {}, text="")], db)
    assert gh.repo("o/r")["default_branch"] == "main"
    assert gh.repo("o/r")["default_branch"] == "main"
    assert s.sent[1].headers["If-None-Match"] == '"abc"'
    assert s.sent[0].headers["Authorization"] == "Bearer t0k"


def test_head_sha_uses_sha_media_type(tmp_path):
    gh, s = client([Resp(200, None, {"ETag": '"e"'}, text="0123456789" * 4)], DB(tmp_path / "s.db"))
    assert gh.head_sha("o/r", "main") == "0123456789" * 4
    assert s.sent[0].headers["Accept"] == "application/vnd.github.sha"


def test_pagination_follows_link_header():
    gh, s = client([
        Resp(200, {"check_runs": [{"name": "a"}]}, {"Link": '<https://api.github.com/x?page=2>; rel="next"'}),
        Resp(200, {"check_runs": [{"name": "b"}]}),
    ])
    assert [r["name"] for r in gh.paginate("/x", key="check_runs")] == ["a", "b"]
    assert s.sent[1].url == "https://api.github.com/x?page=2"


def test_pagination_refuses_partial_lists():
    pages = [Resp(200, [1], {"Link": '<https://api.github.com/x?p>; rel="next"'}) for _ in range(3)]
    gh, _ = client(pages)
    with pytest.raises(Incomplete):
        gh.paginate("/x", max_pages=2)


def test_truncated_tree_is_an_error():
    gh, _ = client([Resp(200, {"truncated": True, "tree": []})])
    with pytest.raises(Incomplete):
        gh.tree("o/r", "sha")


def test_rate_limit_waits_briefly_then_gives_up():
    short = Resp(403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Remaining": "0", "Retry-After": "5"})
    gh, s = client([short, Resp(200, {"ok": 1})])
    assert gh.get("/x") == {"ok": 1}
    long = Resp(429, {"message": "slow down"}, {"Retry-After": "3600"})
    gh, _ = client([long])
    with pytest.raises(RateLimited):
        gh.get("/x")


def test_server_errors_retry_with_bound():
    gh, s = client([Resp(502, {}), Resp(502, {}), Resp(502, {})])
    with pytest.raises(Exception, match="HTTP 502"):
        gh.get("/x")
    assert len(s.sent) == 3


def test_not_found():
    gh, _ = client([Resp(404, {"message": "Not Found"})])
    with pytest.raises(NotFound):
        gh.repo("o/missing")


def test_quota_tracking():
    gh, _ = client([Resp(200, {}, {"X-RateLimit-Remaining": "10"})])
    gh.get("/x")
    assert gh.quota_low
