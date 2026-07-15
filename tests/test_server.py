"""Tier 1: the invariants that protect user data.

edit_note deletes real notes from a library that syncs to iCloud, so its refusals are not
politeness -- they are the only thing standing between a bug and data loss. They are cheap
to test with fakes, and worth far more per line than any other test here.
"""

from __future__ import annotations

import pytest

from applenotes_mcp import server
from applenotes_mcp.notestore import Folder, NoteStoreError

NOTE_ID = "x-coredata://32CD64CC-0000-0000-0000-000000000000/ICNote/p123"

FOLDERS = [
    Folder(pk=461, name="Notes", path="Notes"),
    Folder(pk=465, name="Recipes", path="Personal/Recipes"),
    Folder(pk=434, name="Recipes", path="Personal/Projects/Brewing/Recipes"),
    Folder(pk=444, name="Work", path="Work"),
]


@pytest.fixture(autouse=True)
def no_real_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this file may reach Notes.app or NoteStore. Any attempt is a test bug."""

    def forbidden(*_a, **_k):
        raise AssertionError("a unit test tried to talk to the real Notes library")

    monkeypatch.setattr(server, "_osascript", forbidden)
    monkeypatch.setattr(server.bridge, "run", forbidden)
    monkeypatch.setattr(server, "folders", lambda: FOLDERS)


# -- folder resolution ---------------------------------------------------------------


def test_resolves_an_unambiguous_name() -> None:
    assert server._resolve_folder("Work").pk == 444


def test_resolves_a_full_path() -> None:
    assert server._resolve_folder("Personal/Projects/Brewing/Recipes").pk == 434


def test_an_ambiguous_bare_name_is_refused_not_guessed() -> None:
    # AppleScript's `folder "Recipes"` would silently pick one of the two. Filing a note
    # into the wrong folder is invisible -- the user may simply never find it again.
    with pytest.raises(ValueError, match="ambiguous"):
        server._resolve_folder("Recipes")


def test_the_refusal_names_both_candidates() -> None:
    with pytest.raises(ValueError) as exc:
        server._resolve_folder("Recipes")
    assert "Personal/Recipes" in str(exc.value)
    assert "Personal/Projects/Brewing/Recipes" in str(exc.value)


def test_an_unknown_folder_is_refused() -> None:
    with pytest.raises(ValueError, match="no folder named"):
        server._resolve_folder("Nonexistent")


def test_the_path_wins_over_a_bare_name_collision() -> None:
    # An exact path match must not be defeated by some other folder sharing its last
    # component, or the exact address would be unusable.
    assert server._resolve_folder("Personal/Recipes").pk == 465


# -- addressing ----------------------------------------------------------------------


def test_folder_id_is_derived_from_the_notes_own_store_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(server, "_osascript", lambda script: seen.append(script) or "")

    server._move_note(NOTE_ID, FOLDERS[2])  # pk 434

    assert "/ICFolder/p434" in seen[0]
    assert "32CD64CC-0000-0000-0000-000000000000" in seen[0]
    assert "folder id" in seen[0], "moved by name, which is ambiguous"


def test_applescript_strings_are_escaped() -> None:
    assert server._as_str('say "hi"\\') == '"say \\"hi\\"\\\\"'


# -- title handling ------------------------------------------------------------------


def test_a_leading_title_heading_is_dropped() -> None:
    # Notes sets the title itself from the title argument; leaving the `# Title` in the
    # body would repeat it. (The remainder is lstrip()ed, so the blank line goes too.)
    assert server._strip_leading_title("Shopping", "# Shopping\n\nmilk\n") == "milk"


def test_a_different_leading_heading_is_kept() -> None:
    assert server._strip_leading_title("Shopping", "# Other\n\nmilk\n").startswith("# Other")


def test_a_deeper_heading_matching_the_title_is_kept() -> None:
    assert server._strip_leading_title("Shopping", "## Shopping\n\nmilk\n").startswith("## ")


# -- edit_note refusals --------------------------------------------------------------


def _stub_read(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    """Wire up edit_note's dependencies with safe defaults, overridable per test."""
    monkeypatch.setattr(server, "_note_field", lambda _id, field: {"body": "<div>x</div>", "name": "Old"}[field])
    monkeypatch.setattr(server, "note_folder", lambda _id: None)
    monkeypatch.setattr(server, "read_note_markdown", lambda _id, tables=None: "true markdown\n")
    monkeypatch.setattr(server, "destructible_attachments", lambda _id: [])
    for name, value in overrides.items():
        monkeypatch.setattr(server, name, value)


def test_edit_refuses_when_the_protobuf_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # The HTML fallback cannot see checklists, so rewriting from it would silently turn
    # every checkbox into a plain bullet. Reading may degrade; editing may not.
    def unreadable(_id, tables=None):
        raise NoteStoreError("no Full Disk Access")

    _stub_read(monkeypatch, read_note_markdown=unreadable)

    with pytest.raises(RuntimeError, match="refusing to edit"):
        server.edit_note(NOTE_ID, "new body")


def test_edit_refuses_a_note_with_real_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_read(monkeypatch, destructible_attachments=lambda _id: ["public.jpeg"])

    with pytest.raises(ValueError, match="refusing to edit"):
        server.edit_note(NOTE_ID, "new body")


def test_edit_backs_up_the_true_markdown_not_the_degraded_html(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # The backup is the only safety net on a destructive operation. Deriving it from the
    # HTML would record a note's checklists as plain bullets -- exactly the degraded read
    # that edit_note refuses to rewrite from.
    import json

    _stub_read(monkeypatch, read_note_markdown=lambda _id, tables=None: "- [x] ticked\n")
    monkeypatch.setattr(server, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(server, "_create_and_identify", lambda *_: NOTE_ID.replace("p123", "p999"))
    monkeypatch.setattr(server, "_osascript", lambda _script: "")

    server.edit_note(NOTE_ID, "new body")

    backup = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert backup["markdown"] == "- [x] ticked\n"


def test_edit_does_not_delete_the_original_if_the_replacement_was_not_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    def cannot_identify(*_a):
        raise RuntimeError("expected exactly one new note, found 2")

    _stub_read(monkeypatch, _create_and_identify=cannot_identify)
    monkeypatch.setattr(server, "BACKUP_DIR", tmp_path)

    deleted: list[str] = []
    monkeypatch.setattr(server, "_osascript", lambda s: deleted.append(s) or "")

    with pytest.raises(RuntimeError, match="original is untouched"):
        server.edit_note(NOTE_ID, "new body")

    assert not any("delete" in s for s in deleted)


def test_edit_never_deletes_the_note_it_just_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # If the new note somehow resolved to the old ID, deleting the "old" one would destroy
    # the replacement too, and the note would be gone entirely.
    _stub_read(monkeypatch)
    monkeypatch.setattr(server, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(server, "_create_and_identify", lambda *_: NOTE_ID)
    monkeypatch.setattr(server, "_osascript", lambda _s: "")

    with pytest.raises(RuntimeError, match="internal error"):
        server.edit_note(NOTE_ID, "new body")
