"""Acceptance criteria 9, 10 and the static half of 11: what the compiled interface and the API can and cannot do.

Real here: every file under readme_monitor/web/, the HTTP server, the database and redaction. Simulated: GitHub
(hostile repository text) and the credential store. Not covered by any test in this file: real keyboard, touch,
responsive and 4K animation behaviour. Those need a browser and are checked by hand by the lead (docs/verification.md).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from readme_monitor import credentials, server

from .conftest import REPO as DEMO_REPO, initial_readme
from .test_server_api import start, vault  # noqa: F401 (fixtures)

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "readme_monitor" / "web"
TEXT = {".html", ".js", ".css", ".json", ".svg", ".txt"}
SECRET = "ghp_" + "Kq3Lw8Zx1Mn5Vb7Cd9Fg2Hj4" * 2


def web_files():
    files = [p for p in WEB.rglob("*") if p.is_file()]
    assert files, "readme_monitor/web is empty: build the interface (cd ui; npm ci; npm run build)"
    return files


def text_of(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


# ---- 9 ---------------------------------------------------------------------------------------

HOSTILE = ('\n<script>alert(1)</script> [x](javascript:alert(2)) <img src=x onerror="alert(3)"> <iframe src="https://evil.example"></iframe>\n'
           f"Pasted by mistake: {SECRET}\n")


def test_criterion_09_compiled_assets_hold_no_secret_local_path_external_url_inline_script_or_eval():
    """Static scan of every shipped file. Fonts are binary and only checked for source-map references by name."""
    allowed_urls = ("http://www.w3.org/", "https://react.dev/", "https://github.com/")  # XML namespaces and documentation links
    for p in web_files():
        if p.suffix not in TEXT:
            continue
        text = text_of(p)
        where = p.relative_to(WEB).as_posix()
        for pattern in credentials.SECRET_PATTERNS:
            assert not pattern.search(text), f"{where}: looks like a secret"
        assert not re.search(r"(?<!\w)[A-Za-z]:[\\/]{1,2}[A-Za-z0-9_.-]+[\\/]", text), f"{where}: local Windows path"
        assert not re.search(r"(?<![\w.-])/(?:Users|home)/[\w.-]+/", text), f"{where}: local path"
        for url in re.findall(r"https?://[^\s\"'`)<>\\]+", text):
            assert url.startswith(allowed_urls), f"{where}: external URL {url}"
        assert not re.search(r"\beval\s*\(|new\s+Function\s*\(|document\.write\s*\(", text), f"{where}: dynamic code"
        if p.suffix == ".html":
            for tag in re.findall(r"<script\b[^>]*>", text):
                assert re.search(r"\bsrc=", tag), f"{where}: inline script"
            assert not re.search(r"<script\b[^>]*>(?!\s*</script>)", text) and not re.search(r"\son\w+\s*=", text) and "javascript:" not in text
        if p.suffix == ".css":
            assert not re.search(r"url\(\s*[\"']?(?!/assets/|data:|#)", text), f"{where}: stylesheet loads something outside /assets"


def test_criterion_09_hostile_readme_text_and_secrets_come_back_as_inert_json_without_the_secret(env, start, vault):
    """Simulated: GitHub holding a README with script, javascript: link, event handler, iframe and a pasted token (also in a
    commit message). Real: dry run, redaction, drafts, events, state and diagnostics as the API returns them."""
    e = env(files={"README.md": initial_readme() + HOSTILE, "pyproject.toml": '[project]\nname = "demo"\n', "src/demo/app.py": "A = 1\n"})
    e.gh.push_commit({"src/demo/app.py": "A = 2\n"}, f"pasted {SECRET}")
    e.run(trigger="dry_run", dry_run=True)
    e.run()
    app, c = start(e.home)
    state = c.json("GET", "/api/state")[1]
    draft_id = next(r for r in state["repos"] if r["name"] == DEMO_REPO)["latest_draft_id"]
    paths = ["/api/state", "/api/events?limit=500", "/api/runs", "/api/usage", "/api/diagnostics", f"/api/drafts/{draft_id}",
             f"/api/events?repo={DEMO_REPO}&limit=500"]
    for path in paths:
        status, headers, body = c.req("GET", path)
        assert status == 200 and headers["content-type"].startswith("application/json") and headers["x-content-type-options"] == "nosniff", path
        assert b"<script" not in body and b"<img" not in body and b"<iframe" not in body, path  # `<` is escaped inside the JSON
        assert SECRET.encode() not in body and credentials.SECRET_PATTERNS[1].search(body.decode()) is None, path
        json.loads(body)
    text = json.loads(c.req("GET", f"/api/drafts/{draft_id}")[2])["files"]["README.md"]["old"]
    assert "<script>alert(1)</script>" in text and "[REDACTED]" in text  # kept as text, secret removed
    for status_path in ("/", "/settings"):
        headers = c.req("GET", status_path)[1]
        assert "script-src 'self'" in headers["content-security-policy"] and "frame-ancestors 'none'" in headers["content-security-policy"]


# ---- 10 --------------------------------------------------------------------------------------


def test_criterion_10_compiled_page_is_same_origin_reduced_motion_aware_and_ships_no_source_maps(start, tmp_path):
    """Static checks that back the interface claims. Real keyboard, touch, responsive and 4K animation checks are done by the
    lead in a browser (docs/verification.md); nothing here measures them. The server must also serve every file the page names."""
    index = WEB / "index.html"
    assert index.is_file(), "the compiled page is missing: cd ui; npm ci; npm run build"
    html = text_of(index)
    refs = re.findall(r"""(?:src|href)=["']([^"']+)["']""", html)
    assets = [r for r in refs if not r.startswith("data:")]
    assert assets and all(r.startswith("/assets/") for r in assets), refs  # relative to this server, nothing external
    for r in assets:
        assert (WEB / r.lstrip("/")).is_file(), r
    sheets = [p for p in WEB.rglob("*.css")]
    bundles = [p for p in WEB.rglob("*.js")]
    assert sheets and any("prefers-reduced-motion" in text_of(p) for p in sheets + bundles)  # honoured in the stylesheet or the bundle
    assert not [p for p in WEB.rglob("*") if p.suffix == ".map"]
    for p in web_files():
        if p.suffix in TEXT:
            assert "sourceMappingURL" not in text_of(p), p.name
    app, c = start(tmp_path / "home")
    for r in assets:
        status, headers, body = c.req("GET", r)
        assert status == 200 and body == (WEB / r.lstrip("/")).read_bytes() and "immutable" in headers["cache-control"], r
    assert c.req("GET", "/")[2] == index.read_bytes()


# ---- 11 (static half; scripts/verify-clean-install.ps1 does the real work and is run by hand and in CI) --------


def test_criterion_11_the_clean_install_check_exists_and_cannot_use_credentials_or_register_a_task():
    """The script itself installs a copy of the tree into a temporary folder and serves the page; see docs/verification.md for its
    last real output. This test only guards what the script is not allowed to do."""
    script = ROOT / "scripts" / "verify-clean-install.ps1"
    text = script.read_text(encoding="utf-8")
    assert "-RuntimeHome" in text and "--no-browser" in text and "/api/state" in text
    assert not re.search(r"readme_monitor\s+(activate|bind-host|install-task|run-task-now|run-once|set-credential|init|discover|preview)", text)
    for forbidden in ("Register-ScheduledTask", "schtasks", "Set-ExecutionPolicy"):
        assert forbidden not in text, forbidden
    install = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert install.count("install-task") >= 1 and "if ($InstallTask)" in install  # scheduling stays an explicit opt-in
