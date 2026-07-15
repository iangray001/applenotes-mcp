"""What Apple's markdown converter did to our input -- claims about Apple, not about us.

These read the fixtures: what we WROTE (`.md`) against what Notes actually STORED (`.zdata`,
via `.expected`). A failure here is not a regression in this codebase. It means Apple's
`Make Rich Text from Markdown` has changed behaviour -- which is worth knowing, because most
of these are losses we work around, and a fix upstream means code here can be deleted.

Hermetic: it runs off the captured fixtures, not a live Notes.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def stored(name: str) -> str:
    return (FIXTURES / f"{name}.expected").read_text()


def written(name: str) -> str:
    return (FIXTURES / f"{name}.md").read_text()


def test_h1_and_h2_survive_but_h3_is_flattened_to_h2() -> None:
    """Heading depth is preserved to two levels, then collapses.

    `#` becomes Notes' Title style and `##` its Heading style, both of which round trip.
    `###` ALSO becomes Heading -- Notes' Subheading style exists (style_type 2) but the
    markdown converter never emits it -- so `### Foo` reads back as `## Foo`.

    The loss is one level deep and it does NOT compound: reading back `## Foo` and writing
    it again yields `## Foo`.
    """
    md = stored("headings")
    assert "# Title level" in md  # `#`   -> Title      -> `#`
    assert "## Heading level" in md  # `##`  -> Heading    -> `##`
    assert "## Subheading level" in md  # `###` -> Heading    -> `##`  (flattened)
    assert "### " not in md
    assert "### Subheading level" in written("headings")  # ... which is what we asked for


def test_the_note_title_is_stored_as_the_first_paragraph() -> None:
    # Notes keeps no separate title field: the title IS the first paragraph, styled Title.
    # So a read always leads with `# <title>`, and create_note strips a leading `# <title>`
    # from the body to avoid writing it twice.
    assert stored("headings").startswith("# fixture-headings\n")


def test_links_gain_a_trailing_slash() -> None:
    # Notes normalises a bare-host URL: https://example.com -> https://example.com/
    # Harmless, but it means the first round trip is not byte-identical. It is stable
    # afterwards, since the slash is already there the second time.
    assert "https://example.com" in written("inline_formatting")
    assert "https://example.com/" in stored("inline_formatting")


def test_bold_italic_monospace_and_links_all_survive() -> None:
    md = stored("inline_formatting")
    for expected in ["**bold**", "*italic*", "`monospace`", "[link]("]:
        assert expected in md


def test_a_numbered_list_after_a_bullet_list_stays_separate() -> None:
    # Apple's converter gets this right; it is AppleScript's HTML export that merges them.
    md = stored("lists_bullet_then_numbered")
    assert "1. first number" in md


def test_nesting_survives_to_at_least_three_levels() -> None:
    assert "    - nested twice" in stored("lists_nested")


def test_checklists_keep_their_ticked_state() -> None:
    assert "- [x] ticked item" in stored("checklists")


def test_a_list_following_a_table_survives_when_the_table_is_its_own_block() -> None:
    # Only true because bridge.split_blocks sends each table separately. Converted in one
    # chunk, the bullets come back as literal text with a bullet glyph and tab characters.
    md = stored("table_then_list")
    assert "- bullet after the table" in md
    assert "\t" not in md
