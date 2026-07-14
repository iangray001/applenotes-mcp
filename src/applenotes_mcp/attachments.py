"""Tell real attachments (photos, PDFs) apart from inline objects (tables).

`count of attachments` in AppleScript counts a table as an attachment, so a naive
"refuse to edit notes with attachments" rule would refuse exactly the notes this
server is best at producing. The distinction only exists in Notes' own database:
a table is UTI `com.apple.notes.table`, whereas a real file is `public.jpeg`,
`com.adobe.pdf` and the like.

We open NoteStore.sqlite read-only (immutable), purely to read those UTIs. We never
write to it -- it is Core Data backed with CloudKit sync state alongside, and
writing to it out from under a running Notes.app is a reliable way to corrupt a
user's notes.

If the database cannot be read (it needs Full Disk Access), we fail closed and
treat every attachment as unsafe.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

NOTESTORE = (
    Path.home() / "Library" / "Group Containers" / "group.com.apple.notes" / "NoteStore.sqlite"
)

# A table is regenerated faithfully from markdown, so recreating a note keeps it.
INLINE_UTIS = {"com.apple.notes.table"}


def _attachment_pks(note_id: str) -> list[int]:
    """Core Data primary keys of a note's attachments, via AppleScript."""
    script = f'''
        tell application "Notes"
            set out to ""
            repeat with a in attachments of note id "{note_id}"
                set out to out & (id of a) & linefeed
            end repeat
            return out
        end tell
    '''
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "AppleScript failed")

    pks = []
    for line in result.stdout.splitlines():
        match = re.search(r"/ICAttachment/p(\d+)", line.strip())
        if match:
            pks.append(int(match.group(1)))
    return pks


def destructible_attachments(note_id: str) -> list[str]:
    """UTIs of attachments that recreating the note would destroy.

    Tables are excluded -- they survive, because we rebuild them from markdown.
    Fails closed: if the UTI cannot be determined, the attachment is reported as
    destructible.
    """
    pks = _attachment_pks(note_id)
    if not pks:
        return []

    try:
        # mode=ro, NOT immutable=1: immutable ignores the write-ahead log, so a table
        # created seconds ago is invisible and would be misreported as a real
        # attachment -- which would block editing the note we just wrote.
        uri = f"file:{NOTESTORE.as_posix()}?mode=ro"
        # closing(), not the connection's own context manager: that one only ends the
        # transaction, and leaks the handle. This server is long-lived.
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            placeholders = ",".join("?" * len(pks))
            rows = conn.execute(
                f"SELECT Z_PK, ZTYPEUTI FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK IN ({placeholders})",
                pks,
            ).fetchall()
        utis = {pk: uti for pk, uti in rows}
    except sqlite3.Error:
        return [f"unknown ({len(pks)} attachment(s); NoteStore unreadable)"]

    return [
        utis.get(pk) or "unknown"
        for pk in pks
        if utis.get(pk) not in INLINE_UTIS
    ]
