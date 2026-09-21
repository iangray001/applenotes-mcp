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

import re
import struct
import zlib
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from applenotes_mcp import bridge, server
from tests.conftest import LIVE_FOLDER, assert_in_live_folder

pytestmark = pytest.mark.live


def _png(width: int = 2, height: int = 2) -> bytes:
    """A minimal valid PNG, so Notes stores it as a real image attachment."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([255, 0, 0]) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


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


def test_create_folder_makes_missing_nested_folders(live_folder) -> None:
    # A nested path under the dedicated folder; each missing segment is created in order.
    from applenotes_mcp import notestore

    base = f"{live_folder}/cf-parent/cf-child"
    try:
        result = server.create_folder(base)
        assert "created" in result
        paths = {f.path for f in notestore.folders()}
        assert f"{live_folder}/cf-parent" in paths and base in paths
        # idempotent: a second call is a no-op
        assert "already exists" in server.create_folder(base)
    finally:
        # remove the two folders we made (deepest first), by id
        for path in (base, f"{live_folder}/cf-parent"):
            f = next((x for x in notestore.folders() if x.path == path), None)
            if f:
                server._osascript(
                    f'tell application "Notes" to delete folder id '
                    f"{server._as_str(notestore.folder_id_for_pk(f.pk))}"
                )


def test_a_long_title_is_still_found_and_filed(make_note) -> None:
    # Regression: Notes truncates a note's `name` (its first line) once long enough, so the
    # exact-title lookup used to verify creation returned 0 -- which raised before the folder
    # move and stranded the note in the default folder. A long title must still land here.
    long_title = "Weekly Team Sync Notes and Action Items for the Quarterly Planning Review " + ("x" * 60)
    note_id = make_note(long_title, "body\n")
    assert_in_live_folder(note_id)


# -- search --------------------------------------------------------------------------


def test_title_search_finds_by_title_and_full_text_finds_by_body(make_note) -> None:
    # A distinctive nonsense word in the BODY only, so title search misses it and full-text
    # finds it. Also checks the enriched row carries the note's folder.
    token = "zqxwv"  # unlikely to appear in any real note
    note_id = make_note("search-target", f"A note whose body mentions {token} once.\n")

    by_title = server.search_notes(token)
    assert note_id not in by_title, "full-text token should not match on title"

    by_text = server.search_note_text(token)
    row = next((r for r in by_text.splitlines() if r.startswith(note_id)), None)
    assert row is not None, "full-text search did not find the note by its body"
    assert LIVE_FOLDER in row, "search row did not carry the note's folder"


def test_list_folder_shows_a_note_it_contains(make_note) -> None:
    # The note ID in the listing is rebuilt from the store UUID, not returned by AppleScript,
    # so this also checks that reconstruction produces the same id create_note handed back.
    note_id = make_note("listing-target", "a body\n")
    listing = server.list_folder(LIVE_FOLDER)
    row = next((r.strip() for r in listing.splitlines() if note_id in r), None)
    assert row is not None, "created note not found in its folder's listing"
    assert "listing-target" in row


def test_read_back_recovers_the_features_that_were_written(make_note) -> None:
    md = server.read_note(make_note("features", RICH_BODY))

    assert "## A heading" in md
    assert "- first bullet" in md
    assert "1. first number" in md and "2. second number" in md
    assert "- [ ] unticked task" in md
    # Whether the TICK survives depends on which bridge build is installed; the checkbox
    # itself must survive either way. See test_ticking_follows_the_installed_build.
    assert ("- [x] ticked task" if bridge.installed_is_full() else "- [ ] ticked task") in md
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


def test_ticking_follows_the_installed_build(make_note) -> None:
    """The full build ticks; the basic build cannot, and says so.

    Ticking needs the Notes "Set Checklist Items Checked" action. macOS 27's Shortcuts
    refuses to IMPORT any workflow containing it, so the basic build leaves it out -- but
    the action still RUNS, so the full build (installed via tools/wfimport.m) has it and
    ticks correctly. Either way the checkbox itself survives; only the tick is at stake.
    """
    md = server.read_note(make_note("checklist-state", "- [x] done\n- [ ] todo\n"))
    lines = [ln for ln in md.splitlines() if "done" in ln or "todo" in ln]
    assert ("- [x] done" if bridge.installed_is_full() else "- [ ] done") in lines
    assert "- [ ] todo" in lines


def test_a_mixed_list_keeps_its_order_across_the_block_boundary(make_note) -> None:
    """The case split_blocks is riskiest on: unticked items ride in the prose chunk while
    ticked ones are appended by intent, so this list is emitted as three separate blocks
    (markdown / checklist / markdown). The items must still come back in document order,
    as one continuous list, with the tick on the right one.
    """
    md = server.read_note(make_note("mixed-list", "- [ ] milk\n- [x] eggs\n- [ ] bread\n"))
    items = [ln.strip() for ln in md.splitlines() if "]" in ln and ln.strip().startswith("-")]
    ticked = "- [x] eggs" if bridge.installed_is_full() else "- [ ] eggs"
    assert items == ["- [ ] milk", ticked, "- [ ] bread"], f"got: {md!r}"


def test_a_ticked_item_is_reported_as_a_loss_only_on_the_basic_build(live_folder) -> None:
    out = server.create_note(
        title="tick-warning", markdown="- [x] done\n", folder=live_folder
    )
    if bridge.installed_is_full():
        assert "WARNING" not in out, "the full build writes ticks; nothing to warn about"
    else:
        assert "WARNING" in out and "UNTICKED" in out


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


def test_edit_keeps_a_checklist_item_as_a_checkbox(make_note) -> None:
    # Editing must not turn a checkbox into a plain bullet. The TICK cannot survive on
    # macOS 27 (see test_a_ticked_item_is_written_unticked), but the checkbox must.
    old_id = make_note("edit-checklist", "- [ ] keep me a box\n")
    read_back = server.read_note(old_id)
    new_id = _new_id(server.edit_note(old_id, read_back))
    assert "- [ ] keep me a box" in server.read_note(new_id)


# -- attachments (create) ------------------------------------------------------------


def test_create_note_attaches_a_local_image_inline(make_note, tmp_path) -> None:
    """A local-file image ref in the markdown is attached at its position, byte-perfect.

    This exercises the whole write path that reading alone cannot: the file rides in as an
    extra `-i` input, the shortcut fetches it by index and calls Add File to Note, and it
    lands between the prose either side of it.
    """
    img = tmp_path / "live_probe.png"
    img.write_bytes(_png())
    md = f"Before the image.\n\n![live pic]({img.as_uri()})\n\nAfter the image.\n"

    note_id = make_note("with-attachment", md)
    out = server.read_note(note_id)

    lines = [ln for ln in out.splitlines() if ln.strip()]
    before = next(i for i, ln in enumerate(lines) if ln == "Before the image.")
    image = next(i for i, ln in enumerate(lines) if ln.startswith("![") and "file://" in ln)
    after = next(i for i, ln in enumerate(lines) if ln == "After the image.")
    assert before < image < after, "image was not attached at its inline position"

    stored = Path(unquote(urlparse(re.search(r"\((file://[^)]+)\)", lines[image]).group(1)).path))
    assert stored.read_bytes() == img.read_bytes(), "attachment bytes differ from source"


@pytest.mark.parametrize("size", ["small", "medium", "large"])
def test_a_display_size_follows_the_installed_build(tmp_path, size, live_folder) -> None:
    """The counterpart of the ticking test, for Set Attachment Size.

    On the full build this round-trips, which also proves the read-side value map
    (ZMERGEABLEPREFERREDVIEWSIZE -> size name) matches what the intent writes for each
    case. On the basic build the suffix is inert and must be reported.
    """
    img = tmp_path / f"{size}.png"
    img.write_bytes(_png())
    out = server.create_note(
        title=f"sized-{size}", markdown=f"![pic|{size}]({img.as_uri()})\n", folder=live_folder
    )
    read_back = server.read_note(out.splitlines()[0])
    if bridge.installed_is_full():
        assert "WARNING" not in out
        assert f"|{size}]" in read_back, f"{size} did not round-trip; got: {read_back!r}"
    else:
        assert "WARNING" in out and "display size" in out
        assert f"|{size}]" not in read_back


def test_no_size_reads_back_without_a_pipe(make_note, tmp_path) -> None:
    img = tmp_path / "plain.png"
    img.write_bytes(_png())
    note_id = make_note("sized-default", f"![pic]({img.as_uri()})\n")
    out = server.read_note(note_id)
    image_line = next(ln for ln in out.splitlines() if ln.startswith("![") and "file://" in ln)
    assert "|" not in image_line.split("](", 1)[0], "default size should have no pipe"


def test_create_note_rejects_a_missing_attachment(make_note, tmp_path) -> None:
    # A bad path must fail loudly, not create a half-populated note. (Raised before the
    # shortcut runs, so nothing is created -- hence not routed through make_note.)
    from applenotes_mcp import bridge

    missing = tmp_path / "nope.png"
    with pytest.raises(bridge.BridgeError, match="attachment not found"):
        server.create_note(
            title="bad-attachment", markdown=f"![x]({missing})\n", folder=LIVE_FOLDER
        )


# -- attachments (edit) --------------------------------------------------------------


def _image_bytes_from(markdown: str) -> bytes:
    """Read the file behind the first `![...](file://...)` in some read_note output."""
    url = re.search(r"\((file://[^)]+)\)", markdown).group(1)
    return Path(unquote(urlparse(url).path)).read_bytes()


def test_edit_preserves_an_on_disk_attachment(make_note, tmp_path) -> None:
    """Editing a note that has an image keeps the image, byte-identical.

    This is the payoff of the create-before-delete ordering: read_note gives a file:// path
    to the original's Media file, edit_note re-attaches it into the replacement (Notes copies
    it) while the original still exists, then deletes the original. The copy must survive.
    """
    img = tmp_path / "edit_probe.png"
    img.write_bytes(_png(3, 3))
    old_id = make_note("edit-attachment", f"Intro.\n\n![pic]({img.as_uri()})\n\nOutro.\n")

    read_back = server.read_note(old_id)
    assert "](file://" in read_back, "attachment did not read back as a file path"

    new_id = _new_id(server.edit_note(old_id, read_back.replace("Intro.", "EDITED intro.")))
    out = server.read_note(new_id)

    assert "EDITED intro." in out, "the edit did not take"
    assert "](file://" in out, "the attachment was dropped by the edit"
    # Byte-identical, and read AFTER the original note (and its Media file) was deleted.
    assert _image_bytes_from(out) == img.read_bytes()
