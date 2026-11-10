"""Account-wide repository discovery, reconciliation by GitHub repository id, and dispositions.

Discovery only lists and records repositories. It never authorizes sending source to Claude:
discovered repositories get api_export = "none" unless a [[repo]] entry says otherwise, and
export-restricted repositories can never be sent at all.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .config import Config, RepoConfig
from .github import GitHub, GitHubError, NotFound

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _meta(r: dict) -> dict:
    return {
        "owner": (r.get("owner") or {}).get("login"),
        "visibility": r.get("visibility") or ("private" if r.get("private") else "public"),
        "archived": bool(r.get("archived")),
        "disabled": bool(r.get("disabled")),
        "fork": bool(r.get("fork")),
        "default_branch": r.get("default_branch"),
        "pushed_at": r.get("pushed_at"),
    }


def list_owned(gh: GitHub, owner: str) -> tuple[list[dict], dict]:
    """(owned repositories, coverage). Raises GitHubError when the listing is not complete."""
    me = gh.whoami()
    if me.get("login", "").lower() != owner.lower():
        raise GitHubError(f"the GitHub credential belongs to {me.get('login')!r}, not {owner!r}; "
                          "it cannot list that account's private repositories")
    listed = gh.owned_repos()
    # Ownership is verified per repository: collaborations and organization repositories are not enrolled.
    owned = [r for r in listed if ((r.get("owner") or {}).get("login") or "").lower() == owner.lower()]
    public = sum(1 for r in owned if not r.get("private"))
    private = len(owned) - public
    limits = []
    if me.get("public_repos") is not None and public != me["public_repos"]:
        limits.append(f"found {public} public repositories; GitHub reports {me['public_repos']}")
    classic_repo_scope = "repo" in {x.strip() for x in (gh.scopes or "").split(",")}
    if me.get("owned_private_repos") is None and classic_repo_scope:
        pass  # a classic token with `repo` scope can see every repository the account owns
    elif me.get("owned_private_repos") is None:
        limits.append("this credential does not report the account's private repository count, so complete "
                      "private coverage cannot be proven (for example a fine-grained token limited to selected "
                      "repositories); grant it 'All repositories' with read-only Metadata to fix")
    elif private != me["owned_private_repos"]:
        limits.append(f"found {private} private repositories; GitHub reports {me['owned_private_repos']} owned "
                      "(the credential cannot see the rest)")
    coverage = {
        "owner": owner, "listed": len(owned), "public": public, "private": private,
        "skipped_not_owned": len(listed) - len(owned),
        "complete": not limits, "limitations": limits,
        "token_kind": "classic" if gh.scopes is not None else "fine-grained or app (no scope header)",
    }
    return owned, coverage


def reconcile(db, cfg: Config, gh: GitHub, owned: list[dict], coverage: dict) -> dict:
    """Record a complete listing. Renames keep their row (history, spending, pending work);
    repositories absent from the listing are checked one by one and never assumed deleted."""
    explicit = {r.name.lower() for r in cfg.repos}
    summary = {"new": 0, "renamed": 0, "present": len(owned), "missing": 0, "transferred": 0}
    seen = set()
    for r in owned:
        rid, name = r["id"], r["full_name"]
        seen.add(rid)
        row = db.repo_by_id(rid)
        if row is not None and row["name"] != name:
            db.rename_repo(row["name"], name)
            summary["renamed"] += 1
        elif row is None:
            by_name = db.conn.execute("SELECT name FROM repos WHERE name=? COLLATE NOCASE", (name,)).fetchone()
            if by_name is None:
                summary["new"] += 1
            else:
                name = by_name["name"]  # e.g. a [[repo]] processed before discovery: attach the id
        prior = db.repo_by_id(rid)
        restricted = cfg.is_restricted(name) or bool(prior and prior["restricted"])
        db.update_repo(name, repo_id=rid, meta=json.dumps(_meta(r)), presence="present", restricted=int(restricted),
                       source="config" if name.lower() in explicit else "discovered")
    for row in db.repos():
        if row["repo_id"] is None or row["repo_id"] in seen or row["presence"] in ("transferred",):
            continue
        try:
            now_at = gh.repo_by_id(row["repo_id"])
        except NotFound:
            db.update_repo(row["name"], presence="inaccessible")  # deleted, or this credential lost access
            summary["missing"] += 1
            continue
        new_owner = ((now_at.get("owner") or {}).get("login") or "")
        if new_owner.lower() != coverage["owner"].lower():
            meta = json.loads(row["meta"] or "{}") | {"transferred_to": now_at.get("full_name")}
            db.update_repo(row["name"], presence="transferred", meta=json.dumps(meta),
                           state="transferred", state_detail=f"transferred to {now_at.get('full_name')}; no longer processed")
            summary["transferred"] += 1
        # Same owner but not listed: a listing race; leave it as it was.
    db.set_setting("discovery", json.dumps({"at": _now().isoformat(timespec="seconds"), **coverage, "summary": summary}))
    return summary


def discovery_due(db, cfg: Config) -> bool:
    if not cfg.discovery.enabled:
        return False
    last = json.loads(db.get_setting("discovery", "{}") or "{}").get("at")
    return last is None or datetime.fromisoformat(last) + timedelta(hours=cfg.discovery.refresh_hours) <= _now()


def refresh(db, cfg: Config, gh: GitHub) -> dict:
    """Discover and reconcile. A failed or partial listing changes nothing but the record of the
    failure, so a rate limit or outage can never look like deleted repositories."""
    try:
        owned, coverage = list_owned(gh, cfg.discovery.owner)
    except GitHubError as e:
        prev = json.loads(db.get_setting("discovery", "{}") or "{}")
        prev.update(last_error=f"{type(e).__name__}: {str(e)[:200]}", last_error_at=_now().isoformat(timespec="seconds"))
        db.set_setting("discovery", json.dumps(prev))
        raise
    return reconcile(db, cfg, gh, owned, coverage)


def restricted(cfg: Config, row) -> bool:
    return bool(row is not None and row["restricted"])


def effective_repos(cfg: Config, db) -> list[RepoConfig]:
    """[[repo]] entries plus discovered repositories, with export restrictions applied."""
    rows = {r["name"].lower(): r for r in db.repos()}
    out = []
    for r in cfg.repos:
        row = rows.get(r.name.lower())
        if cfg.is_restricted(r.name) or restricted(cfg, row):
            r = replace(r, api_export="none")
        out.append(r)
    if cfg.discovery.enabled:
        explicit = {r.name.lower() for r in cfg.repos}
        excluded = {n.lower() for n in cfg.discovery.exclude}
        for key, row in sorted(rows.items()):
            if row["source"] == "discovered" and row["presence"] == "present" and key not in explicit and key not in excluded:
                out.append(cfg.discovery.repo(row["name"]))
    return out


def find_repo(cfg: Config, db, name: str) -> RepoConfig:
    for r in effective_repos(cfg, db):
        if r.name.lower() == name.lower():
            return r
    return cfg.repo(name)  # raises a ConfigError that names the config file


def disposition(cfg: Config, row: dict | None, repo: RepoConfig | None) -> tuple[str, str]:
    """One visible disposition per repository: (code, explanation). `row` is the stored state as a dict."""
    row = row or {}
    meta = json.loads(row.get("meta") or "{}")
    name = row.get("name") or repo.name
    state = row.get("state") or "new"
    detail = row.get("state_detail") or ""
    if row.get("presence") == "transferred":
        return "blocked", f"transferred to {meta.get('transferred_to') or 'another owner'}; not processed"
    if row.get("presence") == "inaccessible" or state == "inaccessible":
        return "inaccessible", "deleted, or no longer visible to the GitHub credential (not assumed deleted)"
    if meta.get("archived") or meta.get("disabled") or state == "archived":
        return "archived", "archived or disabled on GitHub; nothing is written"
    if name.lower() in {n.lower() for n in cfg.discovery.exclude}:
        return "blocked", "excluded by discovery.exclude" + ("; export-restricted" if row.get("restricted") else "")
    if repo is None:
        return "blocked", "not monitored (discovery is off and there is no [[repo]] entry)"
    if repo.paused or row.get("paused"):
        return "paused", "paused"
    if state == "empty":  # only GitHub's "repository is empty" answer; size 0 also occurs for tiny repositories
        return "empty", "no commits yet; a README is proposed after the first push"
    if state in ("needs_init", "adoption_deferred"):
        return "awaiting_adoption", detail or "README not adopted yet"
    if state in ("conflict", "permission_missing", "error", "rate_limited", "credentials_missing", "unsupported_readme",
                 "renamed", "transferred", "stale", "publish_blocked"):
        return "blocked", f"{state}: {detail}" if detail else state
    seen = "never processed yet" if state == "new" else state
    if repo.api_export != "allowlist":
        why = ("export-restricted: needs its destination-specific approval" if row.get("restricted") or cfg.is_restricted(name)
               else "need an owner-approved [[repo]] entry with api_export = \"allowlist\"")
        return "awaiting_export_approval", f"{seen}; structure and release sections only; Claude-written sections {why}"
    return "enabled", seen
