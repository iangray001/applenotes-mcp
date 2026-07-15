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
CONTAINER = NOTESTORE.parent

# A table is regenerated faithfully from markdown, so recreating a note keeps it.
INLINE_UTIS = {"com.apple.notes.table"}

# For deciding image (`![]`) vs generic file (`[]`) when rendering an attachment. UTI first,
# with a filename-extension fallback for the odd attachment that carries no/again UTI.
IMAGE_UTIS = {
    "public.png", "public.jpeg", "public.jpg", "public.heic", "public.heif",
    "public.tiff", "public.gif", "com.compuserve.gif", "public.image", "public.webp",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".heif", ".tiff", ".tif", ".gif", ".webp"}


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


# -- reading: resolving an inline attachment to its file on disk ---------------------


def _media_bases() -> list[Path]:
    """Directories a note's media might live under.

    For the primary account it is `<container>/Media`; other accounts nest it under
    `<container>/Accounts/<account>/Media`. Both are tried, so resolution does not depend
    on which account a note belongs to.
    """
    bases = [CONTAINER / "Media"]
    accounts = CONTAINER / "Accounts"
    if accounts.is_dir():
        bases += [p / "Media" for p in accounts.iterdir() if p.is_dir()]
    return [b for b in bases if b.is_dir()]


def _media_path(media_identifier: str, filename: str | None) -> Path | None:
    """Find a media file on disk.

    Notes stores it at `Media/<media-id>/<generation-dir>/<filename>`, and the generation
    directory maps to no database column, so the file is located by searching for its name
    under the media directory rather than by reconstructing that path. Returns None if the
    file is not present locally -- e.g. evicted to iCloud and not yet downloaded.
    """
    if not filename:
        return None
    for base in _media_bases():
        media_dir = base / media_identifier
        if media_dir.is_dir():
            for candidate in media_dir.rglob(filename):
                if candidate.is_file():
                    return candidate
    return None


def _is_image(uti: str | None, path: Path) -> bool:
    return (uti in IMAGE_UTIS) or (path.suffix.lower() in IMAGE_EXTS)


def _attachment_markdown(uti: str | None, filename: str | None, path: Path | None) -> str:
    name = filename or "attachment"
    if path is None:
        # Present in the note, but not on disk (not downloaded from iCloud, say). Emit a
        # visible marker rather than a broken link to a path that does not exist.
        return f"[attachment not downloaded: {name}]"
    uri = path.as_uri()  # percent-encodes spaces etc.; the container path has them
    return f"![{name}]({uri})" if _is_image(uti, path) else f"[{name}]({uri})"


def note_media(note_pk: int) -> dict[str, str]:
    """Map each file attachment's identifier to the markdown that renders it.

    Keyed by the ICAttachment ZIDENTIFIER, which is exactly what the note protobuf carries
    on the attachment's placeholder run, so the reader can look each placeholder up by the
    identifier it already has. Tables are excluded -- the JOIN on ZMEDIA drops them, since
    a table has no media file (its content is a CRDT, spliced from the HTML instead).

    Fails soft: an unreadable database yields an empty map, and read_note falls back to
    leaving those placeholders empty rather than erroring.
    """
    try:
        uri = f"file:{NOTESTORE.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            rows = conn.execute(
                """
                SELECT a.ZIDENTIFIER, a.ZTYPEUTI, m.ZIDENTIFIER, m.ZFILENAME
                FROM ZICCLOUDSYNCINGOBJECT a
                JOIN ZICCLOUDSYNCINGOBJECT m ON m.Z_PK = a.ZMEDIA
                WHERE a.ZNOTE = ?
                """,
                (note_pk,),
            ).fetchall()
    except sqlite3.Error:
        return {}

    media: dict[str, str] = {}
    for att_id, uti, media_id, filename in rows:
        if att_id:
            media[att_id] = _attachment_markdown(uti, filename, _media_path(media_id, filename))
    return media
