"""Regenerate the golden fixtures in tests/fixtures/ from a live Notes library.

This is a developer tool, NOT a test. It writes to Notes, so it is never run by pytest.

    uv run python tests/capture_fixtures.py

For each spec below it creates a synthetic note through the bridge, then captures what the
reader has to work from:

    <name>.zdata   raw, still-gzipped ZICNOTEDATA.ZDATA -- the note's protobuf
    <name>.html    the HTML AppleScript hands back (tables are scraped from this)
    <name>.md      the markdown that was written, for reference
    <name>.actual  what the reader currently produces from the two above

`.actual` is NOT the expected output. Review it, and promote it by hand to `<name>.expected`
once you are satisfied it is right -- a captured bug would otherwise be frozen in as correct.
Where the reader is known to be wrong, hand-write the `.expected` and let the test fail:
that failing test is the specification for the fix.

The notes are left in the fixture folder afterwards, so they can be inspected in Notes.app.
Re-running replaces them.

NOTE: the `attachments.*` fixture is NOT regenerated here -- the bridge cannot add images or
PDFs to a note, so it was captured by hand from a note built in Notes.app. Leave it be.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from applenotes_mcp.html_to_markdown import extract_tables  # noqa: E402
from applenotes_mcp.notestore import NOTESTORE, _note_pk, read_note_markdown  # noqa: E402
from applenotes_mcp.server import (  # noqa: E402
    _as_str,
    _create_and_identify,
    _note_field,
    _osascript,
    _resolve_folder,
    _move_note,
)

FOLDER = "MCP Fixtures"
FIXTURES = Path(__file__).parent / "fixtures"

# Each fixture isolates one thing the reader has to get right. Keep them small: a golden
# file that tests six features at once tells you nothing useful when it breaks.
SPECS: dict[str, str] = {
    "headings": (
        "# Title level\n\nBody under the title.\n\n"
        "## Heading level\n\nBody under the heading.\n\n"
        "### Subheading level\n\nBody under the subheading.\n"
    ),
    "lists_bullet_then_numbered": (
        # The trap the protobuf reader exists to avoid: AppleScript's HTML merges a
        # numbered list that directly follows a bullet list into the bullet list.
        "- first bullet\n- second bullet\n\n1. first number\n2. second number\n"
    ),
    "lists_nested": (
        "- top level\n  - nested once\n    - nested twice\n- back to top\n"
    ),
    "checklists": (
        # Ticked state, and position among prose -- checklist intents can only APPEND, so
        # a checklist sandwiched between paragraphs is the interesting case.
        "Before the list.\n\n- [ ] unticked item\n- [x] ticked item\n- [ ] another unticked\n\n"
        "After the list.\n"
    ),
    "inline_formatting": (
        "Plain, **bold**, *italic*, `monospace`, and a [link](https://example.com) inline.\n"
    ),
    "table": (
        "| Header A | Header B |\n| --- | --- |\n| a1 | b1 |\n| a2 | b2 |\n"
    ),
    "table_then_list": (
        # Apple's converter destroys a list that follows a table in the same converted
        # chunk; bridge.split_blocks sends the table separately to avoid it.
        "| Col | Val |\n| --- | --- |\n| x | 1 |\n\n- bullet after the table\n- another\n"
    ),
    "emoji": (
        # Run lengths are UTF-16 code units. An emoji is a surrogate pair, so if the
        # slicing arithmetic is wrong, everything AFTER the emoji shifts and the styling
        # lands on the wrong characters.
        "Before 🍺 after, then **bold after emoji** and plain.\n\n"
        "- 🎓 bullet with emoji\n- [x] ⚽️ ticked with emoji\n"
    ),
    "mixed_prose": (
        "# Notes\n\nA paragraph.\n\n- a bullet\n\nAnother paragraph.\n\n1. a number\n"
    ),
}


def note_zdata(note_id: str) -> bytes:
    uri = f"file:{NOTESTORE.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        row = conn.execute(
            "SELECT ZDATA FROM ZICNOTEDATA WHERE ZNOTE = ?", (_note_pk(note_id),)
        ).fetchone()
    if not row or not row[0]:
        raise SystemExit(f"no protobuf for {note_id}")
    return bytes(row[0])


def ensure_folder() -> None:
    try:
        _resolve_folder(FOLDER)
    except ValueError:
        _osascript(
            f'tell application "Notes" to make new folder with properties '
            f"{{name:{_as_str(FOLDER)}}}"
        )
        print(f"created folder {FOLDER!r}")


def delete_existing(title: str) -> None:
    _osascript(f"""
        tell application "Notes"
            repeat with n in (notes of folder {_as_str(FOLDER)} whose name is {_as_str(title)})
                delete n
            end repeat
        end tell
    """)


def main() -> None:
    FIXTURES.mkdir(exist_ok=True)
    ensure_folder()
    target = _resolve_folder(FOLDER)

    for name, markdown in SPECS.items():
        title = f"fixture-{name}"
        delete_existing(title)

        note_id = _create_and_identify(title, markdown)
        _move_note(note_id, target)

        html = _note_field(note_id, "body")
        actual = read_note_markdown(note_id, tables=extract_tables(html))

        (FIXTURES / f"{name}.zdata").write_bytes(note_zdata(note_id))
        (FIXTURES / f"{name}.html").write_text(html)
        (FIXTURES / f"{name}.md").write_text(markdown)
        (FIXTURES / f"{name}.actual").write_text(actual)
        print(f"captured {name}")

    print(f"\n{len(SPECS)} fixtures in {FIXTURES}")
    print("Review each .actual, then promote to .expected once you believe it.")


if __name__ == "__main__":
    main()
