"""Tier 3: live round trips through the real Notes library.

These are the properties nothing below this tier can check: that a note written by the
bridge and read back through the protobuf actually AGREES, and that reading-then-rewriting
reaches a fixed point instead of drifting on every edit.

Doubly gated -- `-m live` AND APPLENOTES_MCP_LIVE=1 -- and everything runs inside the
dedicated folder from conftest, swept clean afterwards. See conftest.py for the guard rails.

    APPLENOTES_MCP_LIVE=1 uv run pytest -m live

Each create_note drives a real shortcut, so this tier is slow (seconds per note). That is
the price of testing the one thing that cannot be faked.
"""

from __future__ import annotations

import pytest

from applenotes_mcp import server
from tests.conftest import LIVE_FOLDER, assert_in_live_folder

pytestmark = pytest.mark.live


# A note that exercises every feature the reader reconstructs. No leading `# title`: the
# note title is supplied separately, and create_note would strip it anyway.
RICH_BODY = (
    "Intro paragraph with **bold**, *italic* and `code`.\n"
    "\n"
    "## A heading\n"
    "\n"
    "- first bullet\n"
    "- second bullet\n"
    "\n"
    "1. first number\n"
    "2. second number\n"
    "\n"
    "- [ ] unticked task\n"
    "- [x] ticked task\n"
    "\n"
    "| Col A | Col B |\n"
    "| --- | --- |\n"
    "| a1 | b1 |\n"
    "\n"
    "Closing paragraph.\n"
)


def test_a_created_note_is_filed_into_the_requested_folder(make_note) -> None:
    note_id = make_note("filing", "just a body\n")
    assert_in_live_folder(note_id)  # reads the folder back from NoteStore, not a guess


def test_read_back_recovers_the_features_that_were_written(make_note) -> None:
    md = server.read_note(make_note("features", RICH_BODY))

    assert "## A heading" in md
    assert "- first bullet" in md
    assert "1. first number" in md and "2. second number" in md
    assert "- [ ] unticked task" in md
    assert "- [x] ticked task" in md  # the ticked STATE, which the HTML path cannot see
    assert "| Col A | Col B |" in md
    assert "**bold**" in md and "*italic*" in md and "`code`" in md
    assert "￼" not in md  # no object placeholder leaked through


def test_reading_then_rewriting_reaches_a_fixed_point(make_note) -> None:
    """f(f(x)) == f(x): the core stability claim.

    The first write/read may transform the input (a note title becomes the first
    paragraph, `###` flattens to `##`, a bare URL gains a slash). What must NOT happen is
    continued drift -- each edit changing the note again -- because edit_note rewrites from
    exactly this read, so drift would corrupt a note a little more on every edit.
    """
    # The title MUST be held constant: a note's title is its first paragraph, so it is part
    # of the read-back, and create_note only strips a leading `# <title>` that matches. Feed
    # the read of one note straight back in under the SAME title, and the fixed point is
    # well-defined; vary the title and the two reads differ trivially in their first line.
    once = server.read_note(make_note("fixed-point", RICH_BODY))
    twice = server.read_note(make_note("fixed-point", once))
    assert once == twice


def test_ticked_state_specifically_survives_a_round_trip(make_note) -> None:
    md = server.read_note(make_note("checklist-state", "- [x] done\n- [ ] todo\n"))
    lines = [ln for ln in md.splitlines() if "task" in ln or "done" in ln or "todo" in ln]
    assert "- [x] done" in lines
    assert "- [ ] todo" in lines


# -- edit_note (destructive) ---------------------------------------------------------


def _new_id(edit_result: str) -> str:
    # edit_note returns "<new_id> (was <old_id>; backup at <path>)".
    return edit_result.split(" (was ", 1)[0]


def test_edit_replaces_the_body_and_mints_a_new_id(make_note) -> None:
    old_id = make_note("to-edit", "original body\n")
    new_id = _new_id(server.edit_note(old_id, "replacement body\n"))

    assert new_id != old_id, "edit_note is delete-and-recreate; the ID must change"
    assert "replacement body" in server.read_note(new_id)
    assert "original body" not in server.read_note(new_id)


def test_edit_keeps_the_note_in_its_original_folder(make_note) -> None:
    # edit_note recreates the note in the default folder and moves it back. If the re-file
    # fails, an edit silently relocates the note -- so this is checked by reading the
    # folder of the NEW id back from NoteStore.
    old_id = make_note("to-edit-folder", "body\n")
    new_id = _new_id(server.edit_note(old_id, "edited body\n"))
    assert_in_live_folder(new_id)


def test_edit_preserves_a_checklists_ticked_state(make_note) -> None:
    # The whole point of refusing to edit from the degraded HTML: an edit must not turn a
    # ticked box into a plain bullet.
    old_id = make_note("edit-checklist", "- [x] keep me ticked\n")
    read_back = server.read_note(old_id)
    new_id = _new_id(server.edit_note(old_id, read_back))
    assert "- [x] keep me ticked" in server.read_note(new_id)
