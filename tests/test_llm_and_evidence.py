import json
import math

import pytest

from readme_monitor.config import load
from readme_monitor.credentials import is_sensitive_path, redact
from readme_monitor.evidence import Snapshot, build_payload, extract_facts, fingerprints, matches, select_sources
from readme_monitor.llm import InvalidOutput, schema, validate

CFG = """
[[repo]]
name = "o/r"
source_include = ["src/**/*.py", "*.toml", "config/*"]
source_exclude = ["src/vendor/**"]
api_export = "allowlist"
"""


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(CFG)
    return load(p)


def snap(files: dict[str, str], readme="# R\n") -> Snapshot:
    tree = [{"path": p, "sha": f"sha-{hash(t)}", "size": len(t)} for p, t in files.items()]
    s = Snapshot("o/r", "main", "a" * 40, "", None, tree, {"README.md": (readme, "r1")}, [], [])
    s.sources = dict(files)
    return s


def test_glob_matching():
    assert matches("src/a/b.py", ["src/**/*.py"]) and matches("src/b.py", ["src/**/*.py"])
    assert not matches("srcx/b.py", ["src/**/*.py"]) and not matches("a/pyproject.toml", ["*.toml"])


def test_sensitive_and_large_files_never_selected(cfg):
    repo = cfg.repos[0]
    tree = [
        {"path": "src/app.py", "sha": "1", "size": 10},
        {"path": "config/.env", "sha": "2", "size": 10},
        {"path": "config/prod.pem", "sha": "3", "size": 10},
        {"path": "src/vendor/x.py", "sha": "4", "size": 10},
        {"path": "src/huge.py", "sha": "5", "size": 10_000_000},
        {"path": "README.md", "sha": "6", "size": 10},
    ]
    selected, skipped = select_sources(tree, repo, cfg)
    assert [e["path"] for e in selected] == ["src/app.py"]
    assert any(".env" in s for s in skipped) and any("prod.pem" in s for s in skipped) and any("huge" in s for s in skipped)
    assert is_sensitive_path("x/credentials.json") and not is_sensitive_path("src/app.py")


def test_secret_values_are_redacted_from_payload(cfg):
    secret = "sk-ant-api03-" + "x" * 40
    s = snap({"src/app.py": f'API_KEY = "{secret}"\nGH = "ghp_{"a" * 36}"\npassword = "hunter2hunter2"\n'})
    payload, flags = build_payload(s, cfg.repos[0], cfg, "README.md", {"features": ""}, ["features"])
    dumped = json.dumps(payload)
    assert secret not in dumped and "ghp_" not in dumped and "hunter2" not in dumped
    assert any("redacted" in f for f in flags)


def test_payload_limit_drops_largest_files_and_says_so(cfg):
    small = snap({"src/a.py": "a" * 100, "src/b.py": "b" * 190_000})
    payload, flags = build_payload(small, cfg.repos[0], cfg, "README.md", {}, ["features"])
    assert payload["omitted_files"] == ["src/b.py"] and any("omitted" in f for f in flags)


def test_facts_extracted_deterministically():
    s = snap({
        "pyproject.toml": '[project]\nrequires-python = ">=3.11"\ndependencies = ["requests>=2"]\n[project.scripts]\ntool = "t:main"\n',
        "src/cli.py": "sub.add_parser('sync')\nos.getenv('MY_SETTING')\n",
    })
    values = {(f["kind"], f["value"]) for f in extract_facts(s)}
    assert ("runtime", "Python >=3.11") in values
    assert ("command", "tool (entry point t:main)") in values
    assert ("command", "subcommand sync") in values
    assert ("config_name", "MY_SETTING") in values


def test_fingerprint_ignores_managed_content_and_unlisted_files(cfg):
    from readme_monitor.readme import render_block

    repo = cfg.repos[0]
    a = snap({"src/app.py": "x"}, "# R\n" + render_block("features", "one"))
    b = snap({"src/app.py": "x", "notes/todo.txt": "y"}, "# R\n" + render_block("features", "two"))
    assert fingerprints(a, repo, cfg)[1] == fingerprints(b, repo, cfg)[1]
    c = snap({"src/app.py": "changed"}, "# R\n" + render_block("features", "one"))
    assert fingerprints(a, repo, cfg)[1] != fingerprints(c, repo, cfg)[1]


def payload_for(requested=("features",)):
    return {"requested_sections": list(requested), "facts": [{"id": "F1", "kind": "command", "source": "pyproject.toml", "value": "tool (entry point t:main)"}],
            "files": [{"path": "src/app.py", "evidence": "file:src/app.py", "content": "print('hi')"}],
            "readme_outside_managed_blocks": "", "current_sections": {}}


def reply(content, evidence=("F1",), sid="features", uncertain=False):
    return json.dumps({"sections": [{"id": sid, "content": content, "evidence": list(evidence), "uncertain": uncertain, "review_notes": ""}]})


@pytest.mark.parametrize("text,why", [
    ("not json", "not valid JSON"),
    (json.dumps({"sections": [], "extra": 1}), "shape"),
    (reply("ok", sid="license"), "not requested"),
    (reply("ok", evidence=("F7",)), "not supplied"),
    (reply("<!-- readme-monitor:end:features -->"), "marker"),
    (reply("## Heading\ntext"), "heading"),
    (reply("<script>alert(1)</script>"), "active HTML"),
    (reply("[x](javascript:alert(1))"), "javascript"),
    (reply("x" * 9000), "longer than"),
])
def test_invalid_output_rejected(text, why):
    with pytest.raises(InvalidOutput, match=why):
        validate(text, payload_for(), ["src/app.py"])


def test_unsupported_claims_are_flagged_for_review():
    content = ("Production-ready.\n\n```powershell\ntool --invented-flag\n```\n\nSet `SECRET_THING`. See [guide](docs/missing.md) "
               "and [site](https://example.com/x).")
    sections, flags = validate(reply(content), payload_for(), ["src/app.py"])
    text = " ".join(flags)
    assert "--invented-flag" in text and "SECRET_THING" in text and "docs/missing.md" in text
    assert "example.com" in text and "Production-ready" in text


def test_supported_content_has_no_flags():
    sections, flags = validate(reply("Run `tool`:\n\n```powershell\ntool\n```\n\nCode lives in [src/app.py](src/app.py)."), payload_for(), ["src/app.py"])
    assert sections == {"features": "Run `tool`:\n\n```powershell\ntool\n```\n\nCode lives in [src/app.py](src/app.py)."}
    assert flags == []


def test_generation_fingerprint_ignores_publishing_and_limits(cfg):
    from dataclasses import replace

    repo = cfg.repos[0]
    same = replace(repo, publish_mode="auto_pr", monthly_usd=3.0, daily_usd=1.0, paused=True)
    assert same.fingerprint_part() == repo.fingerprint_part()
    assert replace(repo, source_include=("other/**",)).fingerprint_part() != repo.fingerprint_part()


def test_reservation_uses_exact_count_or_a_strict_fallback(tmp_path):
    from types import SimpleNamespace

    from readme_monitor.config import load
    from readme_monitor.llm import Generator, cost

    p = tmp_path / "c.toml"
    p.write_text('[monitor]\nmax_output_tokens = 1000\n[[repo]]\nname = "o/r"\n')
    cfg = load(p)
    payload = {**payload_for(), "files": [{"path": "a.py", "evidence": "file:a.py", "content": "x" * 100_000}]}
    counter = SimpleNamespace(messages=SimpleNamespace(count_tokens=lambda **kw: SimpleNamespace(input_tokens=40_000)))
    exact = Generator(cfg, None, "k", client=counter).estimate(payload, exact=True)
    assert exact == pytest.approx(cost(cfg.price(), math.ceil(40_000 * 1.05) + math.ceil(len(json.dumps(schema(["features"]))) / 2) + 1000, 1000))
    failing = SimpleNamespace(messages=SimpleNamespace(count_tokens=lambda **kw: 1 / 0))
    fallback = Generator(cfg, None, "k", client=failing).estimate(payload, exact=True)
    # Measured real ratio was ~2.3 characters per token; the fallback must not be below that.
    assert fallback >= cost(cfg.price(), len(json.dumps(payload)) / 2.3, 1000)


def test_template_placeholders_are_not_treated_as_secrets():
    text = 'token = "{{section:x}}"\npassword = "$env:SECRET_VALUE"\n'
    assert redact(text) == (text, 0)


def test_redact_patterns():
    pem = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIE\n-----END RSA " + "PRIVATE KEY-----"  # synthetic, split to avoid scanners
    text, n = redact("token: 'abcdefghijkl'\n" + pem + "\nAKIA" + "ABCDEFGHIJKLMNOP")
    assert n == 3 and "MIIE" not in text and "AKIA" not in text and "abcdefghijkl" not in text
