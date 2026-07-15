"""Shared fixtures, and the safety machinery for the live tier.

The live tests create and delete real notes in a library that syncs to iCloud. A bug in
the *test* could therefore delete real data, so the guard rails here matter more than the
assertions they protect:

  * Two gates. `@pytest.mark.live` is deselected by default (see pyproject `addopts`), AND
    every live test is skipped unless APPLENOTES_MCP_LIVE=1. A bare `pytest` runs nothing
    live; so does `pytest -m live` without the env var.

  * A dedicated folder. Everything is created in LIVE_FOLDER, and teardown deletes notes
    *scoped to that folder* -- an AppleScript that names the folder cannot reach a note
    outside it, whatever a test did wrong.

  * A positive check before any per-note delete. `assert_in_live_folder` reads the note's
    folder back from NoteStore and refuses to delete anything that is not demonstrably in
    LIVE_FOLDER -- a whitelist, not a naming convention.
"""

from __future__ import annotations

import os

import pytest

from applenotes_mcp import bridge, server
from applenotes_mcp.notestore import note_folder

LIVE_FOLDER = "MCP Live Tests"


def pytest_collection_modifyitems(config, items) -> None:
    """Second gate: skip live tests unless the env var is set, even under `-m live`."""
    if os.environ.get("APPLENOTES_MCP_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test; set APPLENOTES_MCP_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def _notes_in_live_folder_count() -> int:
    raw = server._osascript(
        f'tell application "Notes" to return count of notes of folder {server._as_str(LIVE_FOLDER)}'
    )
    return int(raw or "0")


def _delete_all_in_live_folder() -> None:
    # A blanket delete. Scoped to the folder by name -- structurally incapable of touching a
    # note outside LIVE_FOLDER -- but still only ever called once `live_folder` has
    # established the folder started this session empty, so everything in it now is ours.
    server._osascript(f"""
        tell application "Notes"
            delete (every note of folder {server._as_str(LIVE_FOLDER)})
        end tell
    """)


@pytest.fixture(scope="session")
def live_folder() -> str:
    """A dedicated folder that is EMPTY when we find it, and empty again when we leave.

    The blanket delete in teardown is only safe if everything in the folder was created by
    this session, so the precondition is enforced up front: if the folder already holds
    notes, we cannot tell leftover test notes from real ones, and we refuse to run rather
    than risk deleting data we did not create. A note left by a hard-killed previous run
    therefore has to be cleared by hand -- deliberately, so a human confirms none are real.

    The bridge must be installed for any of this to work; if not, live tests are skipped
    rather than failing with a confusing shortcut error.
    """
    if not bridge.is_installed():
        pytest.skip(f"the {bridge.SHORTCUT_NAME!r} shortcut is not installed")

    existing = [f for f in server.folders() if f.name == LIVE_FOLDER]
    if len(existing) > 1:
        pytest.skip(f"{len(existing)} folders named {LIVE_FOLDER!r}; refusing to guess")
    if not existing:
        server._osascript(
            f'tell application "Notes" to make new folder with properties '
            f"{{name:{server._as_str(LIVE_FOLDER)}}}"
        )

    # The check that guards the blanket delete: never delete-all a folder we did not find
    # empty. A freshly created folder is empty; a reused one must be proven so.
    already = _notes_in_live_folder_count()
    if already:
        pytest.skip(
            f"{already} note(s) already in {LIVE_FOLDER!r}. This suite blanket-deletes that "
            "folder on teardown, so it refuses to run against one it did not find empty -- "
            "empty it by hand (confirming none are real) and re-run."
        )

    yield LIVE_FOLDER

    _delete_all_in_live_folder()
    remaining = _notes_in_live_folder_count()
    assert remaining == 0, (
        f"teardown left {remaining} note(s) in {LIVE_FOLDER!r} -- cleanup did not fully work"
    )


@pytest.fixture
def make_note(live_folder: str):
    """Create notes through the real tool, filed into the live folder, tracked for cleanup.

    Returns a factory `make_note(title, markdown) -> note_id`. Whatever a test does with a
    note afterwards (edit_note replaces it, say), the folder-scoped teardown in
    `live_folder` still sweeps up the result.
    """
    created: list[str] = []

    def factory(title: str, markdown: str) -> str:
        note_id = server.create_note(title=title, markdown=markdown, folder=live_folder)
        created.append(note_id)
        return note_id

    yield factory
    # Per-note cleanup is a belt-and-braces pass; the authoritative sweep is folder-scoped.
    for note_id in created:
        try:
            assert_in_live_folder(note_id)
            server._osascript(
                f'tell application "Notes" to delete note id {server._as_str(note_id)}'
            )
        except Exception:
            pass  # the folder-scoped teardown will get it


def assert_in_live_folder(note_id: str) -> None:
    """Refuse to proceed unless the note is demonstrably inside the live folder.

    A positive whitelist read from NoteStore: if the folder cannot be determined, or is
    anything other than LIVE_FOLDER, this raises rather than letting a test delete a note
    it does not own.
    """
    folder = note_folder(note_id)
    assert folder is not None, f"cannot determine folder of {note_id}; refusing to touch it"
    assert folder.name == LIVE_FOLDER, (
        f"{note_id} is in {folder.path!r}, not {LIVE_FOLDER!r}; refusing to touch it"
    )
