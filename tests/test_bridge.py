"""Tier 1: the markdown that gets handed to Apple's converter.

split_blocks() decides what Apple's `Make Rich Text from Markdown` ever sees. Two of its
jobs are workarounds for silent converter bugs -- a wrong result here is a quietly mangled
note, never an error -- so they are pinned hard.
"""

from __future__ import annotations

import pytest

from applenotes_mcp.bridge import (
    BridgeError,
    _attachment_paths,
    _widen_table_delimiters,
    file_ref,
    split_blocks,
)


def kinds(markdown: str) -> list[str]:
    return [b["type"] for b in split_blocks(markdown)]


def texts(markdown: str) -> list[str]:
    return [b["text"] for b in split_blocks(markdown)]


# -- checklists ----------------------------------------------------------------------


def test_checklist_items_become_their_own_blocks_carrying_ticked_state() -> None:
    blocks = split_blocks("- [ ] todo\n- [x] done\n")
    assert blocks == [
        {"type": "checklist", "text": "todo", "checked": "no"},
        {"type": "checklist", "text": "done", "checked": "yes"},
    ]


def test_capital_X_ticks_too() -> None:
    assert split_blocks("- [X] done\n")[0]["checked"] == "yes"


def test_asterisk_bullets_are_checklists_too() -> None:
    assert split_blocks("* [x] done\n")[0]["type"] == "checklist"


def test_prose_around_a_checklist_is_split_so_ordering_is_preserved() -> None:
    # Checklist intents can only append, so the bridge must emit prose/checklist/prose as
    # three ordered blocks; merging them would move the checklist to the end of the note.
    assert kinds("before\n\n- [ ] item\n\nafter\n") == ["markdown", "checklist", "markdown"]


def test_a_plain_bullet_is_not_a_checklist() -> None:
    assert kinds("- just a bullet\n") == ["markdown"]


def test_every_block_carries_a_checked_key() -> None:
    # The shortcut reads `checked` unconditionally; a missing key fails the run.
    for block in split_blocks("# Heading\n\n- [x] done\n\n| a |\n| --- |\n| b |\n"):
        assert block["checked"] in ("yes", "no")


# -- tables --------------------------------------------------------------------------


def test_short_delimiter_rows_are_widened_to_three_dashes() -> None:
    # `| - | - |` is silently left as literal text by Apple's converter. GFM allows it, so
    # hand-written markdown hits this constantly.
    assert _widen_table_delimiters("| - | - |") == "| --- | --- |"


def test_widening_preserves_column_alignment_markers() -> None:
    assert _widen_table_delimiters("| :- | -: | :-: |") == "| :--- | ---: | :---: |"


def test_already_wide_delimiters_are_left_alone() -> None:
    assert _widen_table_delimiters("| --- | :---: |") == "| --- | :---: |"


def test_non_delimiter_rows_are_untouched() -> None:
    for line in ["| a | b |", "not a table", "- a bullet"]:
        assert _widen_table_delimiters(line) == line


def test_a_table_is_sent_as_its_own_block() -> None:
    # Apple's converter destroys whatever FOLLOWS a table in the same converted chunk: a
    # bullet list comes back as literal text with a bullet glyph and tabs, which a later
    # round trip then reads as an indented code block, compounding the damage.
    blocks = texts("intro\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n- after the table\n")
    assert blocks[0] == "intro"
    assert blocks[1].startswith("| a | b |")
    assert blocks[2] == "- after the table"


def test_split_blocks_widens_delimiters_on_the_way_through() -> None:
    assert "| --- | --- |" in texts("| a | b |\n| - | - |\n| 1 | 2 |\n")[0]


# -- file attachments ----------------------------------------------------------------


def test_file_ref_recognises_a_file_url() -> None:
    assert file_ref("![pic](file:///tmp/a%20b/x.png)") == ("pic", "/tmp/a b/x.png")


def test_file_ref_recognises_an_absolute_path() -> None:
    assert file_ref("[report.pdf](/Users/me/report.pdf)") == ("report.pdf", "/Users/me/report.pdf")


def test_file_ref_uses_the_basename_when_no_label() -> None:
    assert file_ref("![](/tmp/photo.jpg)") == ("photo.jpg", "/tmp/photo.jpg")


def test_file_ref_ignores_http_links() -> None:
    assert file_ref("[site](https://example.com)") is None


def test_file_ref_ignores_relative_and_scheme_less_targets() -> None:
    # Not addressable as a file to attach: a note must reference a real local path.
    assert file_ref("[x](docs/x.png)") is None
    assert file_ref("plain text") is None


def test_a_file_line_becomes_a_file_block_carrying_its_path() -> None:
    (block,) = split_blocks("![pic](/tmp/x.png)\n")
    assert block["type"] == "file"
    assert block["path"] == "/tmp/x.png"
    assert block["name"] == "pic"


def test_files_are_numbered_from_input_item_2_in_document_order() -> None:
    # Input item 1 is the JSON payload, so the first file is item 2. The `n` is what the
    # shortcut uses to fetch the right `-i` input, so its order must match the files.
    blocks = split_blocks("![a](/tmp/a.png)\n\nprose\n\n[b](/tmp/b.pdf)\n")
    files = [b for b in blocks if b["type"] == "file"]
    assert [b["n"] for b in files] == ["2", "3"]


def test_a_file_keeps_its_inline_position_between_prose() -> None:
    kinds_ = kinds("intro\n\n![a](/tmp/a.png)\n\noutro\n")
    assert kinds_ == ["markdown", "file", "markdown"]


def test_attachment_paths_are_returned_in_n_order(tmp_path) -> None:
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"1"); b.write_bytes(b"2")
    blocks = split_blocks(f"![a]({a})\n\n[b]({b})\n")
    assert _attachment_paths(blocks) == [a, b]


def test_a_missing_attachment_is_a_clean_error_before_the_shortcut_runs(tmp_path) -> None:
    blocks = split_blocks(f"![gone]({tmp_path / 'nope.png'})\n")
    with pytest.raises(BridgeError, match="attachment not found"):
        _attachment_paths(blocks)


# -- general -------------------------------------------------------------------------


def test_blank_input_produces_no_blocks() -> None:
    assert split_blocks("") == []
    assert split_blocks("\n\n  \n") == []


def test_headings_and_prose_stay_in_one_block() -> None:
    # Fewer blocks means fewer conversion round trips, and Apple's converter handles a
    # heading followed by prose correctly. Only checklists and tables need splitting out.
    assert kinds("# Title\n\nsome prose\n\n- a bullet\n") == ["markdown"]


# -- known bugs, not yet fixed -------------------------------------------------------
# xfail(strict=True), so these turn into failures the moment they start passing: the suite
# tells us when the fix has landed rather than letting a stale xfail rot.


@pytest.mark.xfail(strict=True, reason="split_blocks does not understand fenced code blocks")
def test_checklist_syntax_inside_a_code_fence_is_not_a_checklist() -> None:
    markdown = "```\n- [ ] this is example markdown, not a real checklist\n```\n"
    assert kinds(markdown) == ["markdown"]


@pytest.mark.xfail(strict=True, reason="_widen_table_delimiters does not understand fences")
def test_a_delimiter_row_inside_a_code_fence_is_not_rewritten() -> None:
    markdown = "```\n| - | - |\n```\n"
    assert _widen_table_delimiters(markdown) == markdown
