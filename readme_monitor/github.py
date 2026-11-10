"""Minimal GitHub REST client: authenticated conditional requests, pagination,
rate-limit awareness and bounded retries."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from urllib.parse import quote

import requests

from . import __version__

API = "https://api.github.com"
log = logging.getLogger(__name__)


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NotFound(GitHubError):
    pass


class RateLimited(GitHubError):
    pass


class Incomplete(GitHubError):
    """GitHub returned a truncated or partial answer; acting on it could miss changes."""


class GitHub:
    def __init__(self, token: str, db=None, session=None, timeout=(10, 30), max_attempts: int = 3, sleep=time.sleep, low_quota: int = 50):
        self.token = token
        self.db = db
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.low_quota = low_quota
        self.remaining: int | None = None
        self.reset_at: int | None = None
        self.scopes: str | None = None

    @property
    def quota_low(self) -> bool:
        return self.remaining is not None and self.remaining < self.low_quota

    # ---- transport --------------------------------------------------------
    def _request(self, method: str, path: str, *, params=None, body=None, accept="application/vnd.github+json", conditional=False):
        url = path if path.startswith("http") else API + path
        cache_key = f"{accept} {url}?{json.dumps(params, sort_keys=True) if params else ''}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"ReadmeMonitor/{__version__}",
        }
        cached_etag = cached_body = None
        if conditional and self.db is not None:
            cached_etag, cached_body = self.db.cache_get(cache_key)
            if cached_etag:
                headers["If-None-Match"] = cached_etag
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self.session.request(method, url, headers=headers, params=params, json=body, timeout=self.timeout)
            except requests.RequestException as e:
                last_exc = GitHubError(f"{method} {path}: network error {type(e).__name__}")
                self.sleep(min(2 ** attempt, 10))
                continue
            self._track_quota(resp.headers)
            status = resp.status_code
            if status == 304 and cached_body is not None:
                return 304, json.loads(cached_body), resp.headers
            if status in (403, 429) and self._is_rate_limit(resp):
                wait = self._retry_after(resp)
                if wait is not None and wait <= 60 and attempt < self.max_attempts:
                    self.sleep(wait)
                    continue
                raise RateLimited(f"GitHub rate limit reached on {path}; resets in {wait if wait is not None else '?'}s", status)
            if status >= 500 or status == 408:
                last_exc = GitHubError(f"{method} {path}: HTTP {status}", status)
                self.sleep(min(2 ** attempt, 10))
                continue
            if status == 404:
                raise NotFound(f"{method} {path}: not found or not accessible with this token", status)
            if status >= 400:
                raise GitHubError(f"{method} {path}: HTTP {status}: {_message(resp)}", status)
            data = {"text": resp.text} if accept.endswith(".sha") else _decode(resp)
            if conditional and self.db is not None and resp.headers.get("ETag"):
                self.db.cache_put(cache_key, resp.headers["ETag"], json.dumps(data))
            return status, data, resp.headers
        raise last_exc or GitHubError(f"{method} {path}: failed")

    def _track_quota(self, headers):
        if "X-RateLimit-Remaining" in headers:
            self.remaining = int(headers["X-RateLimit-Remaining"])
            self.reset_at = int(headers.get("X-RateLimit-Reset", "0"))
        if "X-OAuth-Scopes" in headers:
            self.scopes = headers["X-OAuth-Scopes"]

    @staticmethod
    def _is_rate_limit(resp) -> bool:
        if resp.status_code == 429 or resp.headers.get("Retry-After"):
            return True
        return resp.headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in _message(resp).lower()

    @staticmethod
    def _retry_after(resp) -> int | None:
        if resp.headers.get("Retry-After", "").isdigit():
            return int(resp.headers["Retry-After"])
        reset = resp.headers.get("X-RateLimit-Reset")
        return max(0, int(reset) - int(time.time())) + 1 if reset and reset.isdigit() else None

    def get(self, path, params=None, conditional=False, accept="application/vnd.github+json"):
        return self._request("GET", path, params=params, conditional=conditional, accept=accept)[1]

    def paginate(self, path, params=None, max_pages=10, key: str | None = None) -> list:
        params = dict(params or {}, per_page=100)
        out, url, pages = [], path, 0
        while url:
            pages += 1
            if pages > max_pages:
                raise Incomplete(f"{path}: more than {max_pages} pages; refusing to act on a partial list")
            _, data, headers = self._request("GET", url, params=params)
            out.extend(data[key] if key else data)
            url, params = _next_link(headers.get("Link", "")), None
        return out

    # ---- read operations --------------------------------------------------
    def whoami(self) -> dict:
        return self.get("/user")

    def repo(self, full: str) -> dict:
        return self.get(f"/repos/{full}", conditional=True)

    def owned_repos(self) -> list[dict]:
        """Every repository the authenticated account owns (public and private) that this
        credential can see. Raises Incomplete rather than returning a partial list."""
        return self.paginate("/user/repos", params={"affiliation": "owner", "visibility": "all", "sort": "full_name"}, max_pages=100)

    def repo_by_id(self, repo_id: int) -> dict:
        return self.get(f"/repositories/{repo_id}")

    def head_sha(self, full: str, branch: str) -> str:
        # The .sha media type returns only the commit id and supports ETag/304 polling.
        data = self._request("GET", f"/repos/{full}/commits/{quote(branch, safe='')}", accept="application/vnd.github.sha", conditional=True)[1]
        return data["text"].strip()

    def tree(self, full: str, sha: str) -> list[dict]:
        data = self.get(f"/repos/{full}/git/trees/{sha}", params={"recursive": "1"})
        if data.get("truncated"):
            raise Incomplete(f"{full}: file tree at {sha[:12]} is too large for one GitHub response")
        return [e for e in data["tree"] if e["type"] == "blob"]

    def blob_text(self, full: str, blob_sha: str) -> str | None:
        data = self.get(f"/repos/{full}/git/blobs/{blob_sha}")
        raw = base64.b64decode(data["content"]) if data.get("encoding") == "base64" else data["content"].encode()
        if b"\x00" in raw[:8000]:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def file_at(self, full: str, path: str, ref: str) -> tuple[str | None, str | None]:
        """(text, blob sha) of a file at a ref, or (None, None) when it does not exist."""
        try:
            data = self.get(f"/repos/{full}/contents/{quote(path)}", params={"ref": ref})
        except NotFound:
            return None, None
        if isinstance(data, list) or data.get("type") != "file":
            return None, None
        return self.blob_text(full, data["sha"]), data["sha"]

    def releases(self, full: str, n: int = 5) -> list[dict]:
        data = self.get(f"/repos/{full}/releases", params={"per_page": n}, conditional=True)
        return [r for r in data if not r.get("draft")]

    def checks(self, full: str, sha: str) -> list[dict]:
        """Check runs and commit statuses for one revision: [{name, status, conclusion}]."""
        runs = self.paginate(f"/repos/{full}/commits/{sha}/check-runs", key="check_runs", max_pages=5)
        out = [{"name": r["name"], "status": r["status"], "conclusion": r.get("conclusion")} for r in runs]
        combined = self.get(f"/repos/{full}/commits/{sha}/status")
        for s in combined.get("statuses", []):
            done = s["state"] != "pending"
            out.append({"name": s["context"], "status": "completed" if done else "in_progress", "conclusion": s["state"] if done else None})
        return out

    def ref_sha(self, full: str, branch: str) -> str | None:
        try:
            return self.get(f"/repos/{full}/git/ref/heads/{branch}")["object"]["sha"]
        except NotFound:
            return None

    def commit_message(self, full: str, sha: str) -> str:
        return self.get(f"/repos/{full}/git/commits/{sha}")["message"]

    def compare(self, full: str, base: str, head: str) -> dict:
        """What changed between two revisions, one page only (no pagination): {status, total_commits,
        commits: [{sha, message, author, date}], files: [{path, status, additions, deletions}], truncated}.
        A base GitHub no longer knows (rewritten history, deleted branch) gives status "unavailable"."""
        try:
            data = self.get(f"/repos/{full}/compare/{base}...{head}")
        except NotFound:
            return {"status": "unavailable", "total_commits": 0, "commits": [], "files": [], "truncated": False}
        commits = [_commit(c) for c in data.get("commits") or []]
        files = [{"path": f.get("filename"), "status": f.get("status"), "additions": f.get("additions", 0),
                  "deletions": f.get("deletions", 0)} for f in data.get("files") or []]
        total = int(data.get("total_commits") or len(commits))
        # GitHub returns at most 250 commits and 300 files on one page.
        return {"status": data.get("status", "unavailable"), "total_commits": total, "commits": commits, "files": files,
                "truncated": total > len(commits) or len(files) >= 300}

    def commit(self, full: str, sha: str) -> dict:
        return _commit(self.get(f"/repos/{full}/commits/{sha}"))

    def find_open_pr(self, full: str, head_owner: str, branch: str, base: str) -> dict | None:
        prs = self.get(f"/repos/{full}/pulls", params={"head": f"{head_owner}:{branch}", "base": base, "state": "open"})
        return prs[0] if prs else None

    def pr(self, full: str, number: int) -> dict:
        return self.get(f"/repos/{full}/pulls/{number}")

    # ---- write operations -------------------------------------------------
    def create_ref(self, full: str, branch: str, sha: str):
        self._request("POST", f"/repos/{full}/git/refs", body={"ref": f"refs/heads/{branch}", "sha": sha})

    def move_ref(self, full: str, branch: str, sha: str):
        # Only ever called for the monitor-owned bot branch after ownership was verified.
        self._request("PATCH", f"/repos/{full}/git/refs/heads/{branch}", body={"sha": sha, "force": True})

    def put_file(self, full: str, path: str, branch: str, text: str, message: str, blob_sha: str | None) -> str:
        body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode(), "branch": branch}
        if blob_sha:
            body["sha"] = blob_sha
        data = self._request("PUT", f"/repos/{full}/contents/{quote(path)}", body=body)[1]
        return data["commit"]["sha"]

    def create_pr(self, full: str, title: str, head: str, base: str, body: str) -> dict:
        return self._request("POST", f"/repos/{full}/pulls", body={"title": title, "head": head, "base": base, "body": body, "maintainer_can_modify": False})[1]

    def update_pr(self, full: str, number: int, title: str, body: str) -> dict:
        return self._request("PATCH", f"/repos/{full}/pulls/{number}", body={"title": title, "body": body})[1]

    def enable_auto_merge(self, pr_node_id: str, method: str = "SQUASH") -> None:
        query = "mutation($id:ID!,$m:PullRequestMergeMethod!){enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:$m}){clientMutationId}}"
        data = self._request("POST", "/graphql", body={"query": query, "variables": {"id": pr_node_id, "m": method}})[1]
        if data.get("errors"):
            raise GitHubError("auto-merge not enabled: " + "; ".join(e.get("message", "?") for e in data["errors"])[:300])


def _commit(c: dict) -> dict:
    info = c.get("commit") or {}
    author = info.get("author") or {}
    return {"sha": c.get("sha"), "message": (info.get("message") or "").split("\n", 1)[0][:200],
            "author": author.get("name") or (c.get("author") or {}).get("login"), "date": author.get("date")}


def _decode(resp):
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"text": resp.text}


def _message(resp) -> str:
    try:
        return str(resp.json().get("message", ""))[:200]
    except (ValueError, AttributeError):
        return ""


def _next_link(link: str) -> str | None:
    m = re.search(r'<([^>]+)>;\s*rel="next"', link)
    return m[1] if m else None
