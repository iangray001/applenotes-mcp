"""Read from Notes' on-disk store, read-only -- the reader half of the server.

Two things live here. First, decoding a single note's content: `ZICNOTEDATA.ZDATA` holds a
gzipped protobuf of the note's text plus its attribute runs (checklists, list types, inline
formatting, attachment refs), which `decode`/`_paragraphs`/`_render` turn back into markdown.
Second, querying the surrounding Core Data tables (all via `connect()`): the folder tree
(`folders`), a note's folder (`note_folder`), search-result metadata (`note_details`),
title search (`search_titles`), a folder's contents (`folder_contents`), and the store UUID
(`_store_uuid`) that lets a note pk be turned back into its `x-coredata` id.

Verified protobuf schema (field numbers confirmed against real notes):

    top.2.3          Note
      .2             note text (string)
      .5             AttributeRun (repeated)
        .1           length, in UTF-16 code units
        .2           ParagraphStyle
          .1         style_type: 0 title, 1 heading, 2 subheading, 4 monospaced,
                     100 bullet, 101 dashed, 102 numbered, 103 checklist
          .4         indent level
          .5         Checklist { .2 = done }
        .3           Font        (a monospaced face means an inline code span)
        .5           font_weight: 1 bold, 2 italic, 3 both
        .9           link URL (string)
        .12          AttachmentInfo { .1 = identifier (ICAttachment id), .2 = type UTI }

Tables are not in the protobuf: they are separate attachment objects (their content is a
CRDT in ZMERGEABLEDATA), appearing in the text only as a U+FFFC placeholder. Rather than
decode that too, we take tables from the AppleScript HTML -- which renders them faithfully --
and splice them into the placeholders in order. Real file attachments (images, PDFs) share
that placeholder but are resolved to disk separately, in attachments.py.
"""

from __future__ import annotations

import gzip
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

NOTESTORE = (
    Path.home() / "Library" / "Group Containers" / "group.com.apple.notes" / "NoteStore.sqlite"
)

OBJECT_PLACEHOLDER = "￼"

# The UTI Notes gives a table. A table's content is a CRDT in a separate attachment, not a
# file on disk, so it is spliced from the HTML rather than resolved to a path.
TABLE_UTI = "com.apple.notes.table"

STYLE_TITLE = 0
STYLE_HEADING = 1
STYLE_SUBHEADING = 2
STYLE_MONOSPACED = 4
STYLE_BULLET = 100
STYLE_DASHED = 101
STYLE_NUMBERED = 102
STYLE_CHECKLIST = 103

HEADING_PREFIX = {STYLE_TITLE: "# ", STYLE_HEADING: "## ", STYLE_SUBHEADING: "### "}

BOLD, ITALIC = 1, 2


class NoteStoreError(RuntimeError):
    pass


@contextmanager
def connect():
    """A read-only NoteStore connection, closed on exit.

    Any failure to open it or to run a query on it (the usual cause being no Full Disk
    Access) is raised as NoteStoreError. Callers that must keep working when the database
    cannot be read catch NoteStoreError and return a default; callers that cannot proceed
    let it propagate.
    """
    conn = None
    try:
        conn = sqlite3.connect(f"file:{NOTESTORE.as_posix()}?mode=ro", uri=True)
        yield conn
    except sqlite3.Error as exc:
        raise NoteStoreError(f"cannot read NoteStore (Full Disk Access?): {exc}") from exc
    finally:
        if conn is not None:
            conn.close()


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def _fields(buf: bytes):
    """Yield (field_number, wire_type, value) for one protobuf message."""
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 2:
            length, i = _varint(buf, i)
            value = buf[i : i + length]
            i += length
        elif wire == 5:
            value, i = buf[i : i + 4], i + 4
        elif wire == 1:
            value, i = buf[i : i + 8], i + 8
        else:
            return
        yield number, wire, value


def _first(buf: bytes, number: int):
    for num, _wire, value in _fields(buf):
        if num == number:
            return value
    return None


@dataclass
class Run:
    length: int
    style_type: int | None = None
    indent: int = 0
    checked: bool | None = None
    weight: int = 0
    monospaced: bool = False
    link: str | None = None
    # An inline attachment carries a U+FFFC placeholder in the text and an AttachmentInfo
    # (field 12) here: its identifier (the ICAttachment ZIDENTIFIER) and type UTI. Tables
    # and real files (images, PDFs) are told apart by this UTI.
    att_identifier: str | None = None
    att_uti: str | None = None


@dataclass
class Paragraph:
    style_type: int | None = None
    indent: int = 0
    checked: bool | None = None
    runs: list[tuple[str, Run]] = field(default_factory=list)


def _note_pk(note_id: str) -> int:
    match = re.search(r"/ICNote/p(\d+)", note_id)
    if not match:
        raise NoteStoreError(f"not an Apple Notes note id: {note_id!r}")
    return int(match.group(1))


def _load(note_pk: int) -> tuple[str, list[Run]]:
    with connect() as conn:
        row = conn.execute("SELECT ZDATA FROM ZICNOTEDATA WHERE ZNOTE = ?", (note_pk,)).fetchone()

    if not row or not row[0]:
        raise NoteStoreError(f"no note data for note {note_pk} (a locked note?)")

    return decode(bytes(row[0]))


def decode(zdata: bytes) -> tuple[str, list[Run]]:
    """Decode a raw ZICNOTEDATA.ZDATA blob into its text and attribute runs.

    Split out from the database read so the reader can be tested against captured blobs
    (tests/fixtures/*.zdata) without a NoteStore, or a Notes.app, anywhere in sight.
    """
    raw = gzip.decompress(zdata)
    document = _first(raw, 2)
    note = _first(document, 3) if document else None
    if note is None:
        raise NoteStoreError("unexpected note protobuf layout")

    text_bytes = _first(note, 2)
    text = text_bytes.decode("utf-8") if text_bytes else ""

    runs: list[Run] = []
    for number, _wire, value in _fields(note):
        if number != 5:
            continue
        run = Run(length=0)
        for rnum, rwire, rval in _fields(value):
            if rnum == 1 and rwire == 0:
                run.length = rval
            elif rnum == 2 and rwire == 2:
                for pnum, pwire, pval in _fields(rval):
                    if pnum == 1 and pwire == 0:
                        run.style_type = pval
                    elif pnum == 4 and pwire == 0:
                        run.indent = pval
                    elif pnum == 5 and pwire == 2:
                        done = _first(pval, 2)
                        run.checked = bool(done)
            elif rnum == 3 and rwire == 2:
                name = _first(rval, 1)
                if name and b"Monospaced" in name:
                    run.monospaced = True
            elif rnum == 5 and rwire == 0:
                run.weight = rval
            elif rnum == 9 and rwire == 2:
                run.link = rval.decode("utf-8", "replace")
            elif rnum == 12 and rwire == 2:
                ident = _first(rval, 1)
                uti = _first(rval, 2)
                run.att_identifier = ident.decode("utf-8", "replace") if ident else None
                run.att_uti = uti.decode("utf-8", "replace") if uti else None
        runs.append(run)

    return text, runs


def _paragraphs(text: str, runs: list[Run]) -> list[Paragraph]:
    """Slice the text by its runs, then group runs into paragraphs at newlines.

    Run lengths are in UTF-16 code units, so the text is sliced in UTF-16 space; for
    the BMP characters notes contain in practice this matches Python indices, but
    emoji would otherwise shift everything after them.
    """
    units = text.encode("utf-16-le")
    paragraphs: list[Paragraph] = [Paragraph()]
    offset = 0

    for run in runs:
        chunk = units[offset * 2 : (offset + run.length) * 2].decode("utf-16-le")
        offset += run.length

        current = paragraphs[-1]
        if current.style_type is None and run.style_type is not None:
            current.style_type = run.style_type
        if run.checked is not None and current.checked is None:
            current.checked = run.checked
        current.indent = max(current.indent, run.indent)

        while "\n" in chunk:
            head, chunk = chunk.split("\n", 1)
            if head:
                current.runs.append((head, run))
            # Left unstyled: a paragraph takes its style from the next run to reach the
            # top of the outer loop, not from the run whose tail happens to start it.
            paragraphs.append(Paragraph())
            current = paragraphs[-1]

        if chunk:
            current.runs.append((chunk, run))

    return paragraphs


def _inline(text: str, run: Run, in_heading: bool = False) -> str:
    if not text.strip():
        return text
    if run.monospaced:
        return f"`{text}`"

    lead = len(text) - len(text.lstrip())
    trail = len(text) - len(text.rstrip())
    core = text.strip()

    # Notes stores headings as bold, so re-emitting that bold would yield `## **Heading**`
    # and, worse, accumulate another pair of asterisks on every round trip.
    if run.weight & BOLD and not in_heading:
        core = f"**{core}**"
    if run.weight & ITALIC:
        core = f"*{core}*"
    if run.link:
        core = f"[{core}]({run.link})"
    return text[:lead] + core + text[len(text) - trail :] if trail else text[:lead] + core


def _render(
    paragraphs: list[Paragraph],
    tables: list[str],
    media: dict[str, str] | None = None,
) -> str:
    """Reconstruct markdown. `tables` supplies each table's markdown in document order;
    `media` maps an attachment identifier to the markdown for that file (an image or link).

    Every U+FFFC placeholder is resolved from the attachment run that carries it: a table
    is spliced from `tables`, a file is looked up in `media`. Deciding per run -- rather
    than assuming every placeholder is the next table -- is what stops an image before a
    table from consuming the table's slot.
    """
    media = media or {}
    lines: list[str] = []
    number = 0
    table_index = 0

    def resolve_placeholder(run: Run) -> str:
        nonlocal table_index
        if run.att_uti == TABLE_UTI or run.att_uti is None:
            # A table, or a placeholder with no attachment info (legacy notes): take the
            # next table from the HTML, preserving the old behaviour for the latter.
            table = tables[table_index] if table_index < len(tables) else ""
            table_index += 1
            return table
        return media.get(run.att_identifier or "", f"[attachment: {run.att_uti}]")

    for para in paragraphs:
        in_heading = para.style_type in HEADING_PREFIX

        if any(OBJECT_PLACEHOLDER in text for text, _ in para.runs):
            parts: list[str] = []
            for text, run in para.runs:
                for piece in re.split(f"({OBJECT_PLACEHOLDER})", text):
                    if piece == OBJECT_PLACEHOLDER:
                        parts.append("\n" + resolve_placeholder(run) + "\n")
                    elif piece:
                        parts.append(_inline(piece, run, in_heading))
            lines.extend("".join(parts).strip("\n").splitlines())
            continue

        body = "".join(_inline(text, run, in_heading) for text, run in para.runs)

        style = para.style_type
        if style != STYLE_NUMBERED:
            number = 0

        indent = "  " * para.indent
        if style in HEADING_PREFIX:
            lines.append(HEADING_PREFIX[style] + body)
        elif style == STYLE_CHECKLIST:
            box = "[x]" if para.checked else "[ ]"
            lines.append(f"{indent}- {box} {body}")
        elif style in (STYLE_BULLET, STYLE_DASHED):
            lines.append(f"{indent}- {body}")
        elif style == STYLE_NUMBERED:
            number += 1
            lines.append(f"{indent}{number}. {body}")
        elif style == STYLE_MONOSPACED:
            lines.append(f"    {body}")
        else:
            lines.append(body)

    # Blank line between blocks, but keep a run of same-kind list items (or table rows)
    # together. The KIND matters: a numbered list directly after a bullet list, with no
    # blank line, is parsed back as one bullet list -- which is exactly the corruption
    # this reader exists to avoid.
    def kind(line: str) -> str | None:
        if re.match(r"^\s*-\s\[[ x]\]\s", line):
            return "checklist"
        if re.match(r"^\s*-\s", line):
            return "bullet"
        if re.match(r"^\s*\d+\.\s", line):
            return "numbered"
        if re.match(r"^\s*\|", line):
            return "table"
        return None

    out: list[str] = []
    for line in lines:
        if out and line.strip() and out[-1].strip():
            this, prev = kind(line), kind(out[-1])
            if this is None or this != prev:
                out.append("")
        out.append(line)

    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


@dataclass(frozen=True)
class Folder:
    pk: int
    name: str
    path: str  # "Personal/Projects/Recipes" -- unique, where `name` may not be
    parent: int | None = None  # pk of the containing folder, None at the top level


def folders() -> list[Folder]:
    """Every folder a note can actually be filed into, with its full path.

    Folder NAMES are not unique -- nesting means a library can hold both
    "Personal/Recipes" and "Personal/Projects/Brewing/Recipes". AppleScript's
    `folder "Recipes"` silently picks one of them, so callers must address a folder by
    its path (or, to move a note, by its ID), never by a bare name.

    Excluded, to match what Notes.app itself shows:

      * ZFOLDERTYPE != 0 -- Recently Deleted and Quick Notes are system folders, not
        filing destinations.
      * ZMARKEDFORDELETION -- pending deletion.
      * ZNEEDSINITIALFETCHFROMCLOUD -- a CloudKit stub whose contents were never fetched.
        Notes.app does not display these and they hold no notes, but they persist in the
        database indefinitely (stale ones can be years older than the live records). They
        are the main source of apparently-duplicate folder names, so leaving them in makes
        real folders look ambiguous when they are not.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT Z_PK, ZTITLE2, ZPARENT
            FROM ZICCLOUDSYNCINGOBJECT
            WHERE ZTITLE2 IS NOT NULL
              AND ZFOLDERTYPE = 0
              AND ZMARKEDFORDELETION = 0
              AND ZNEEDSINITIALFETCHFROMCLOUD = 0
            """
        ).fetchall()

    names = {pk: title for pk, title, _ in rows}
    parents = {pk: parent for pk, _, parent in rows}

    def path(pk: int) -> str:
        parts: list[str] = []
        seen: set[int] = set()
        # `seen` guards against a parent cycle, which would otherwise hang the server.
        # The walk also stops at a parent the filter excluded, giving a partial path.
        while pk in names and pk not in seen:
            seen.add(pk)
            parts.append(names[pk])
            pk = parents.get(pk)  # type: ignore[assignment]
        return "/".join(reversed(parts))

    return sorted(
        (Folder(pk=pk, name=names[pk], path=path(pk), parent=parents.get(pk)) for pk in names),
        key=lambda f: f.path,
    )


_STORE_UUID: str | None = None


def _store_uuid() -> str:
    """The Core Data store UUID that note/folder x-coredata IDs are built from.

    It lives in Z_METADATA and is constant for the library, so it is read once and cached.
    This is what lets a note pk found by a NoteStore query be turned back into the
    `x-coredata://<uuid>/ICNote/p<pk>` id that read_note and edit_note need, without a round
    trip through AppleScript.
    """
    global _STORE_UUID
    if _STORE_UUID is None:
        with connect() as conn:
            row = conn.execute("SELECT Z_UUID FROM Z_METADATA").fetchone()
        if not row or not row[0]:
            raise NoteStoreError("no store UUID in Z_METADATA")
        _STORE_UUID = row[0]
    return _STORE_UUID


def note_id_for_pk(pk: int) -> str:
    return f"x-coredata://{_store_uuid()}/ICNote/p{pk}"


def folder_id_for_pk(pk: int) -> str:
    """The x-coredata id of a folder, for addressing it exactly in AppleScript (create a
    subfolder `at folder id ...`, move a note `to folder id ...`)."""
    return f"x-coredata://{_store_uuid()}/ICFolder/p{pk}"


@dataclass(frozen=True)
class FolderListing:
    subfolders: list[Folder]  # direct children only
    notes: list[tuple[str, str, str, str]]  # (id, title, modified, snippet), newest first


def folder_contents(folder_pk: int) -> FolderListing:
    """The notes and immediate subfolders directly inside a folder.

    Notes come from a NoteStore query (id built from the store UUID), newest first;
    subfolders are the direct children from `folders()`. Deleted notes are excluded.
    """
    subfolders = [f for f in folders() if f.parent == folder_pk]

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT Z_PK, ZTITLE1, ZMODIFICATIONDATE1, ZSNIPPET
            FROM ZICCLOUDSYNCINGOBJECT
            WHERE ZFOLDER = ? AND ZTITLE1 IS NOT NULL AND ZMARKEDFORDELETION = 0
            ORDER BY ZMODIFICATIONDATE1 DESC
            """,
            (folder_pk,),
        ).fetchall()

    notes = [
        (
            note_id_for_pk(pk),
            title,
            datetime.fromtimestamp(modified + 978307200).strftime("%Y-%m-%d %H:%M") if modified else "",
            re.sub(r"\s+", " ", snippet or "").strip()[:80],
        )
        for pk, title, modified, snippet in rows
    ]
    return FolderListing(subfolders=subfolders, notes=notes)


def note_folder(note_id: str) -> Folder | None:
    """The folder a note lives in, or None if it cannot be determined.

    AppleScript cannot do this: `container of note id X` fails with -1728 in current
    Notes, however the note is addressed. Reading it from NoteStore is exact, and matters
    because edit_note recreates the note and must re-file it where it was -- otherwise
    editing a note quietly moves it to the default folder.
    """
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT ZFOLDER FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = ?",
                (_note_pk(note_id),),
            ).fetchone()
        if not row or not row[0]:
            return None
        return next((f for f in folders() if f.pk == row[0]), None)
    except NoteStoreError:
        return None


@dataclass(frozen=True)
class NoteDetails:
    folder: str  # the note's folder path, "" if it cannot be determined
    modified: str  # "YYYY-MM-DD HH:MM" local time, "" if unknown
    snippet: str  # a one-line preview of the body


def note_details(pks: list[int]) -> dict[int, NoteDetails]:
    """Folder path, modification date and a body snippet for each note pk.

    Used to enrich search results so notes that share a title (two "Recipes", say) can be
    told apart. Fails soft: an unreadable database yields an empty map and the caller falls
    back to bare id/title.
    """
    if not pks:
        return {}
    placeholders = ",".join("?" * len(pks))
    try:
        with connect() as conn:
            rows = conn.execute(
                f"""
                SELECT Z_PK, ZFOLDER, ZMODIFICATIONDATE1, ZSNIPPET
                FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK IN ({placeholders})
                """,
                pks,
            ).fetchall()
    except NoteStoreError:
        return {}

    folder_path = {f.pk: f.path for f in folders()}
    out: dict[int, NoteDetails] = {}
    for pk, folder_pk, modified, snippet in rows:
        # Core Data timestamps count seconds from 2001-01-01; shift to the Unix epoch.
        when = (
            datetime.fromtimestamp(modified + 978307200).strftime("%Y-%m-%d %H:%M")
            if modified
            else ""
        )
        out[pk] = NoteDetails(
            folder=folder_path.get(folder_pk, ""),
            modified=when,
            snippet=re.sub(r"\s+", " ", snippet or "").strip()[:80],
        )
    return out


def search_titles(query: str) -> list[tuple[int, str]]:
    """(pk, title) for notes whose title contains `query`, case-insensitively.

    A pure NoteStore title search -- the AppleScript equivalent (`notes whose name
    contains`) needs Notes.app to be running and answering Apple Events, whereas this keeps
    working when the app is busy or hung. Recently-Deleted notes are excluded via the folder
    join: a trashed note keeps its title and often is not itself marked deleted, so filtering
    on ZMARKEDFORDELETION alone would still surface it.
    """
    # Escape LIKE's own wildcards so a query containing % or _ still matches literally.
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with connect() as conn:
        rows = conn.execute(
            r"""
            SELECT n.Z_PK, n.ZTITLE1
            FROM ZICCLOUDSYNCINGOBJECT n
            JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER
            WHERE n.ZTITLE1 IS NOT NULL
              AND n.ZMARKEDFORDELETION = 0
              AND f.ZFOLDERTYPE = 0
              AND n.ZTITLE1 LIKE ? ESCAPE '\'
            """,
            (f"%{escaped}%",),
        ).fetchall()
    return [(pk, title) for pk, title in rows]


def read_note_markdown(
    note_id: str,
    tables: list[str] | None = None,
    media: dict[str, str] | None = None,
) -> str:
    """Reconstruct a note's markdown from Notes' protobuf.

    `tables` supplies the markdown for each table in the note, in order (from the
    AppleScript HTML, which renders tables faithfully). `media` maps a file attachment's
    identifier to its rendered markdown; when omitted it is resolved from the on-disk
    media store. Pass `media={}` to render without touching disk -- as the tests do.
    """
    pk = _note_pk(note_id)
    text, runs = _load(pk)
    if media is None:
        from .attachments import note_media

        media = note_media(pk)
    return _render(_paragraphs(text, runs), tables or [], media)
