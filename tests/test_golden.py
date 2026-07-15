"""Tier 2: the reader, driven from captured protobufs. No Notes.app, no NoteStore.

Each fixture is a real note that Notes itself wrote, captured as the two things the reader
actually consumes: the gzipped protobuf (`.zdata`) and the AppleScript HTML (`.html`, which
is where tables come from). Regenerate with `uv run python tests/capture_fixtures.py`.

These pin the READER. Where a fixture shows a loss, the loss happened on the way IN -- it is
baked into what Notes stored -- and is asserted separately in test_apple_conversions.py, so
that a change in Apple's behaviour reads as news rather than as a regression here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from applenotes_mcp.html_to_markdown import extract_tables
from applenotes_mcp.notestore import _paragraphs, _render, decode

FIXTURES = Path(__file__).parent / "fixtures"
NAMES = sorted(p.stem for p in FIXTURES.glob("*.zdata"))


def read_fixture(name: str) -> str:
    """Exactly what read_note() does, minus the database and AppleScript."""
    text, runs = decode((FIXTURES / f"{name}.zdata").read_bytes())
    tables = extract_tables((FIXTURES / f"{name}.html").read_text())
    return _render(_paragraphs(text, runs), tables)


@pytest.mark.parametrize("name", NAMES)
def test_reader_matches_golden(name: str) -> None:
    assert read_fixture(name) == (FIXTURES / f"{name}.expected").read_text()


def test_fixtures_are_present() -> None:
    # Guards against the parametrisation silently collapsing to zero cases if the fixture
    # directory is empty -- which would make this whole file pass while testing nothing.
    assert len(NAMES) >= 9


@pytest.mark.parametrize("name", NAMES)
def test_every_fixture_has_an_expected_file(name: str) -> None:
    # A captured `.actual` promoted to `.expected` is a deliberate act. A missing one means
    # someone re-captured and did not review.
    assert (FIXTURES / f"{name}.expected").exists()


# -- the properties the fixtures exist to protect ------------------------------------


def test_ticked_state_survives() -> None:
    md = read_fixture("checklists")
    assert "- [x] ticked item" in md
    assert "- [ ] unticked item" in md
    # The whole reason for reading the protobuf rather than the HTML: AppleScript's HTML
    # renders a ticked box and a plain bullet identically, as <li>text</li>.
    assert "- [x]" not in (FIXTURES / "checklists.html").read_text()


def test_checklist_sits_between_the_prose_it_was_written_between() -> None:
    # Checklist intents can only APPEND to a note, so position is not free -- the bridge
    # gets it right only because it builds the whole note by appending in order.
    lines = [ln for ln in read_fixture("checklists").splitlines() if ln.strip()]
    before, after = lines.index("Before the list."), lines.index("After the list.")
    ticked = lines.index("- [x] ticked item")
    assert before < ticked < after


def test_numbered_list_after_bullet_list_stays_a_numbered_list() -> None:
    # AppleScript's HTML merges these into one bullet list. The protobuf does not.
    md = read_fixture("lists_bullet_then_numbered")
    assert "- second bullet" in md
    assert "1. first number" in md
    assert "2. second number" in md


def test_nesting_depth_survives() -> None:
    md = read_fixture("lists_nested")
    assert "- top level" in md
    assert "  - nested once" in md
    assert "    - nested twice" in md


def test_inline_formatting_survives() -> None:
    md = read_fixture("inline_formatting")
    for expected in ["**bold**", "*italic*", "`monospace`", "[link](https://example.com/)"]:
        assert expected in md


def test_tables_are_spliced_in_from_the_html() -> None:
    # The protobuf holds a table only as a U+FFFC placeholder; the content comes from HTML.
    md = read_fixture("table")
    assert "| Header A | Header B |" in md
    assert "| a1 | b1 |" in md
    assert "￼" not in md, "an object placeholder leaked into the output"


def test_list_after_table_is_not_destroyed() -> None:
    # Apple's converter mangles a list that follows a table in the same converted chunk;
    # bridge.split_blocks sends each table as its own block to avoid it. If that breaks,
    # the bullets come back as literal text with tab characters.
    md = read_fixture("table_then_list")
    assert "- bullet after the table" in md
    assert "\t" not in md


def test_emoji_do_not_shift_the_styling_that_follows_them() -> None:
    # Run lengths are UTF-16 code units and an emoji is a surrogate pair, so faulty
    # arithmetic would slide every run after the emoji by one, landing the bold on the
    # wrong characters.
    md = read_fixture("emoji")
    assert "Before 🍺 after, then **bold after emoji** and plain." in md
    assert "- 🎓 bullet with emoji" in md
    assert "- [x] ⚽️ ticked with emoji" in md


def test_no_output_ends_without_a_trailing_newline() -> None:
    for name in NAMES:
        assert read_fixture(name).endswith("\n")
