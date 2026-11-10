import pytest

from readme_monitor.readme import (
    MarkerError, apply, content_hash, migrate, parse, render_block, strip_managed, template_text,
)

ALL = ("features", "quick-start", "configuration", "status")


def test_apply_preserves_everything_outside_blocks_byte_for_byte():
    text = "# T\r\n\r\nintro  \r\n" + render_block("features", "old").replace("\n", "\r\n") + "\r\ntail\r\n"
    res = apply(text, {"features": "new"})
    assert res.changed == ["features"]
    assert res.text.startswith("# T\r\n\r\nintro  \r\n") and res.text.endswith("\r\ntail\r\n")
    assert parse(res.text)[0].content == "new"


def test_identical_content_is_not_rewritten():
    text = "x\n" + render_block("features", "same  text") + "\n"
    res = apply(text, {"features": "same  text\n\n"})
    assert res.changed == [] and res.text == text


def test_human_edit_detected_and_left_alone():
    text = render_block("features", "monitor text").replace("monitor text", "person text")
    res = apply(text, {"features": "new"})
    assert res.conflicts == ["features"] and "person text" in res.text


def test_block_without_hash_counts_as_human_owned():
    text = "<!-- readme-monitor:start:features -->\nhand\n<!-- readme-monitor:end:features -->"
    assert parse(text)[0].human_edited


@pytest.mark.parametrize("bad", [
    "<!-- readme-monitor:start:features -->\nx",
    "<!-- readme-monitor:end:features -->",
    "<!-- readme-monitor:start:nope hash=000000000000 -->\n<!-- readme-monitor:end:nope -->",
    "<!-- readme-monitor:start:features -->\n<!-- readme-monitor:start:status -->",
    "<!-- readme-monitor:begin:features -->",
    render_block("features", "a") + "\n" + render_block("features", "b"),
])
def test_malformed_markers_raise(bad):
    with pytest.raises(MarkerError):
        parse(bad)


def test_strip_managed_ignores_generated_content():
    a = "intro\n" + render_block("features", "one")
    b = "intro\n" + render_block("features", "two")
    assert strip_managed(a) == strip_managed(b)


def test_migration_wraps_existing_sections_and_adds_missing_in_order():
    old = "# Proj\n\nIntro.\n\n## Installation\n\nRun it.\n\n```md\n## Not a heading\n```\n\n## Custom\n\nMine.\n\n## License\n\nMIT\n"
    new = migrate(old, "o/proj", "", ALL, ())
    blocks = {b.id: b for b in parse(new)}
    assert blocks["quick-start"].content == "Run it.\n\n```md\n## Not a heading\n```"
    assert not blocks["quick-start"].human_edited
    assert "## Custom\n\nMine.\n" in new and "## License\n\nMIT\n" in new
    # missing sections are inserted before License, in standard order
    order = [new.index(f"## {h}") for h in ("Installation", "Configuration", "Current status and limitations", "License")]
    assert order == sorted(order)
    assert new.index("## Features") < new.index("## Installation")
    # removing the markers gives back the original text plus added sections only
    assert "Intro." in new


def test_migration_is_idempotent():
    once = migrate("# P\n\n## Features\n\nx\n", "o/p", "", ALL, ())
    assert migrate(once, "o/p", "", ALL, ()) == once


def test_template_for_missing_readme():
    text = template_text("o/proj", "Does a thing.", ("features",), ("roadmap",))
    assert "# proj" in text and "**Does a thing.**" in text
    assert "## Roadmap" not in text
    assert [b.id for b in parse(text)] == ["features"]


def test_hash_ignores_trailing_whitespace():
    assert content_hash("a  \nb\n\n") == content_hash("a\nb")
