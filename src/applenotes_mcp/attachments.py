"""Resolve a note's file attachments (photos, PDFs) to their files on disk.

An attachment appears in the note protobuf only as a placeholder carrying its identifier
and type UTI. The identifier joins, in Notes' own database, to a media row (`ZMEDIA`) whose
filename locates the file under `<container>/Media/`. We read that database read-only, never
writing to it -- it is Core Data with CloudKit sync state alongside, and writing to it out
from under a running Notes.app is a reliable way to corrupt a user's notes.

Tables are not files -- their content is a CRDT, spliced from the AppleScript HTML instead --
so they are excluded here by the JOIN on `ZMEDIA`, which they lack.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .notestore import NOTESTORE, NoteStoreError, _first, connect

CONTAINER = NOTESTORE.parent

# An attachment's display size lives in ZMERGEABLEPREFERREDVIEWSIZE, a small protobuf whose
# field 2 -> field 1 is the size value. Absent (NULL) means the default size. The values are
# not sequential -- these were read back from notes written at each size via the Set
# Attachment Size intent (small=2, medium=4, large=0; 0 is a real value, distinct from NULL).
_VIEW_SIZE_BY_VALUE = {2: "small", 4: "medium", 0: "large"}


def _decode_view_size(blob: bytes | None) -> str | None:
    """The display-size name in a ZMERGEABLEPREFERREDVIEWSIZE blob, or None for default."""
    if not blob:
        return None
    inner = _first(bytes(blob), 2)
    return _VIEW_SIZE_BY_VALUE.get(_first(inner, 1)) if inner is not None else None

# For deciding image (`![]`) vs generic file (`[]`) when rendering an attachment. UTI first,
# with a filename-extension fallback for the odd attachment that carries no/again UTI.
IMAGE_UTIS = {
    "public.png", "public.jpeg", "public.jpg", "public.heic", "public.heif",
    "public.tiff", "public.gif", "com.compuserve.gif", "public.image", "public.webp",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".heif", ".tiff", ".tif", ".gif", ".webp"}


# -- resolving an inline attachment to its file on disk ------------------------------


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


def _attachment_markdown(
    uti: str | None, filename: str | None, path: Path | None, size: str | None = None
) -> str:
    # A non-default display size rides in the label after a pipe (`name|small`), which
    # file_ref parses back on a write, so the size round-trips.
    name = f"{filename or 'attachment'}|{size}" if size else (filename or "attachment")
    if path is None:
        # Present in the note, but not on disk (not downloaded from iCloud, say). Emit a
        # visible marker rather than a broken link to a path that does not exist.
        return f"[attachment not downloaded: {name}]"
    uri = path.as_uri()  # percent-encodes spaces etc.; the container path has them
    return f"![{name}]({uri})" if _is_image(uti, path) else f"[{name}]({uri})"


@dataclass(frozen=True)
class FileAttachment:
    identifier: str  # the ICAttachment ZIDENTIFIER, as carried on the note's placeholder run
    uti: str | None
    filename: str | None
    path: Path | None  # the file on disk, or None if it is not present locally
    size: str | None = None  # display size (small/medium/large), None for default


def note_file_attachments(note_pk: int) -> list[FileAttachment]:
    """A note's file attachments (those with a media file), each resolved to disk.

    Tables are excluded by the JOIN on ZMEDIA. Fails soft: an unreadable database yields an
    empty list, and read_note falls back to leaving those placeholders empty rather than
    erroring.
    """
    try:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT a.ZIDENTIFIER, a.ZTYPEUTI, m.ZIDENTIFIER, m.ZFILENAME,
                       a.ZMERGEABLEPREFERREDVIEWSIZE
                FROM ZICCLOUDSYNCINGOBJECT a
                JOIN ZICCLOUDSYNCINGOBJECT m ON m.Z_PK = a.ZMEDIA
                WHERE a.ZNOTE = ?
                """,
                (note_pk,),
            ).fetchall()
    except NoteStoreError:
        return []

    return [
        FileAttachment(att_id, uti, filename, _media_path(media_id, filename), _decode_view_size(vs))
        for att_id, uti, media_id, filename, vs in rows
        if att_id
    ]


def note_media(note_pk: int) -> dict[str, str]:
    """Map each file attachment's identifier to the markdown that renders it.

    Keyed by the identifier the note protobuf carries on the placeholder run, so the reader
    can look each placeholder up by the identifier it already has.
    """
    return {
        a.identifier: _attachment_markdown(a.uti, a.filename, a.path, a.size)
        for a in note_file_attachments(note_pk)
    }


def unresolved_attachments(note_pk: int) -> list[str]:
    """Names of the note's file attachments whose file is NOT on disk.

    `edit_note` recreates a note from the markdown `read_note` produced, and an attachment
    that is not present locally (evicted to iCloud, say) reads back as a marker rather than
    a re-attachable `file://` path -- so recreating would silently drop it. A non-empty list
    here is edit_note's signal to refuse.
    """
    return [
        a.filename or a.identifier
        for a in note_file_attachments(note_pk)
        if a.path is None
    ]
