"""Tier 1: the markdown that gets handed to Notes' own markdown parser.

split_blocks() decides what that parser ever sees. It splits out only what the parser
cannot express -- checklist items and file attachments -- and hands it everything else in
one piece. A wrong result here is a quietly mangled note, never an error, so it is pinned
hard.
"""

from __future__ import annotations

import pytest

from applenotes_mcp.bridge import (
    BRIDGE_VERSION,
    BridgeError,
    _attachment_paths,
    _version_from_actions,
    build_workflow,
    file_ref,
    losses,
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


def test_only_checklist_blocks_carry_a_checked_key() -> None:
    # The shortcut no longer reads `checked` -- ticking is unwritable on macOS 27 -- so it
    # survives only on the blocks `losses` inspects.
    for block in split_blocks("# Heading\n\n- [x] done\n\n| a |\n| --- |\n| b |\n"):
        if block["type"] == "checklist":
            assert block["checked"] in ("yes", "no")
        else:
            assert "checked" not in block


# -- tables --------------------------------------------------------------------------
# Notes' own markdown parser handles all of this, so split_blocks no longer rewrites
# delimiter rows or gives a table its own block. Both were workarounds for the Shortcuts
# *Make Rich Text from Markdown* converter, which is no longer in the path. Verified
# against a live note on macOS 27.


def test_a_table_and_the_list_after_it_stay_in_one_block() -> None:
    blocks = texts("intro\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n- after the table\n")
    assert len(blocks) == 1
    assert "- after the table" in blocks[0]


def test_short_delimiter_rows_are_passed_through_untouched() -> None:
    # `| - | - |` is valid GFM and Notes' parser accepts it; rewriting it is no longer
    # needed, and doing so would be a gratuitous edit of the user's text.
    assert "| - | - |" in texts("| a | b |\n| - | - |\n| 1 | 2 |\n")[0]


# -- file attachments ----------------------------------------------------------------


def test_file_ref_recognises_a_file_url() -> None:
    assert file_ref("![pic](file:///tmp/a%20b/x.png)") == ("pic", "/tmp/a b/x.png", None)


def test_file_ref_recognises_an_absolute_path() -> None:
    assert file_ref("[report.pdf](/Users/me/report.pdf)") == ("report.pdf", "/Users/me/report.pdf", None)


def test_file_ref_uses_the_basename_when_no_label() -> None:
    assert file_ref("![](/tmp/photo.jpg)") == ("photo.jpg", "/tmp/photo.jpg", None)


def test_file_ref_parses_a_pipe_display_size() -> None:
    assert file_ref("![photo|small](/tmp/x.png)") == ("photo", "/tmp/x.png", "small")
    assert file_ref("[doc|large](/tmp/x.pdf)") == ("doc", "/tmp/x.pdf", "large")


def test_file_ref_ignores_an_unknown_size_keeping_it_in_the_name() -> None:
    # A pipe that is not a real size is just part of the label, not a size directive.
    assert file_ref("![a|b|huge](/tmp/x.png)") == ("a|b|huge", "/tmp/x.png", None)


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
    assert block["size"] == ""  # no size -> default


def test_a_sized_file_block_carries_its_size() -> None:
    (block,) = split_blocks("![pic|small](/tmp/x.png)\n")
    assert block["name"] == "pic" and block["size"] == "small"


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


# -- version stamping ----------------------------------------------------------------


def test_the_generated_workflow_stamps_the_current_version() -> None:
    # What build_workflow writes must be exactly what installed_version reads back, or drift
    # detection would false-positive on a freshly generated shortcut.
    actions = build_workflow()["WFWorkflowActions"]
    assert actions[0]["WFWorkflowActionIdentifier"] == "is.workflow.actions.comment"
    assert _version_from_actions(actions) == BRIDGE_VERSION


def test_a_workflow_with_no_comment_reads_as_version_zero() -> None:
    # An old, pre-versioning, or hand-made shortcut -- distinct from "cannot read" (None).
    assert _version_from_actions([{"WFWorkflowActionIdentifier": "is.workflow.actions.gettext"}]) == 0


def test_the_version_is_parsed_from_the_comment_text() -> None:
    actions = [
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.comment",
            "WFWorkflowActionParameters": {"WFCommentActionText": "applenotes-mcp bridge v7 (x)"},
        }
    ]
    assert _version_from_actions(actions) == 7


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


# -- macOS 27 write losses -----------------------------------------------------------
# Shortcuts on macOS 27 refuses to IMPORT a workflow containing Set Checklist Items
# Checked or Set Attachment Size, so neither can be written any more. The note is still
# created; `losses` is what tells the caller which parts of it are wrong.


def test_a_ticked_item_is_reported_as_a_loss() -> None:
    assert any("UNTICKED" in m for m in losses(split_blocks("- [x] done\n")))


def test_an_unticked_item_is_not_a_loss() -> None:
    assert losses(split_blocks("- [ ] todo\n")) == []


def test_an_attachment_size_is_reported_as_a_loss() -> None:
    assert any("display size" in m for m in losses(split_blocks("![pic|small](/tmp/x.png)\n")))


def test_a_sizeless_attachment_is_not_a_loss() -> None:
    assert losses(split_blocks("![pic](/tmp/x.png)\n")) == []


def test_plain_prose_has_no_losses() -> None:
    assert losses(split_blocks("just some prose\n")) == []
