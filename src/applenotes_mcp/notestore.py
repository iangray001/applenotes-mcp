"""Read a note's true structure from Notes' own protobuf.

Both of the obvious read paths are lossy, and lose *different* things:

  * AppleScript's HTML cannot see checklists at all -- a ticked box and a plain bullet
    both come back as <li>text</li> -- and it merges a numbered list into a preceding
    bullet list.
  * Apple's "Make Markdown from Rich Text" action destroys tables and flattens headings.

The truth lives in ZICNOTEDATA.ZDATA: a gzipped protobuf holding the note's plain text
plus a list of attribute runs, each carrying a paragraph style (bullet / numbered /
checklist, with its `done` flag) and inline formatting. We read it -- read-only, never
written -- and reconstruct markdown from it.

Verified schema (field numbers confirmed against real notes, not guessed):

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
        .12          AttachmentInfo { .2 = type UTI }

Tables are NOT in here: they are separate attachment objects (their content is a CRDT
in ZMERGEABLEDATA), and appear in the text only as a U+FFFC placeholder. Rather than
decode that too, we take the tables from the AppleScript HTML -- which renders them
faithfully -- and splice them into the placeholders in order.
"""

from __future__ import annotations

import gzip
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

NOTESTORE = (
    Path.home() / "Library" / "Group Containers" / "group.com.apple.notes" / "NoteStore.sqlite"
)

OBJECT_PLACEHOLDER = "￼"

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
    try:
        uri = f"file:{NOTESTORE.as_posix()}?mode=ro"
        # closing(), not the connection's own context manager: that one only ends the
        # transaction, and leaks the handle. This server is long-lived.
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute(
                "SELECT ZDATA FROM ZICNOTEDATA WHERE ZNOTE = ?", (note_pk,)
            ).fetchone()
    except sqlite3.Error as exc:
        raise NoteStoreError(f"cannot read NoteStore (Full Disk Access?): {exc}") from exc

    if not row or not row[0]:
        raise NoteStoreError(f"no note data for note {note_pk} (a locked note?)")

    raw = gzip.decompress(bytes(row[0]))
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


def _render(paragraphs: list[Paragraph], tables: list[str]) -> str:
    lines: list[str] = []
    number = 0
    table_index = 0

    for para in paragraphs:
        in_heading = para.style_type in HEADING_PREFIX
        body = "".join(_inline(text, run, in_heading) for text, run in para.runs)

        if OBJECT_PLACEHOLDER in body:
            for _ in range(body.count(OBJECT_PLACEHOLDER)):
                replacement = tables[table_index] if table_index < len(tables) else ""
                table_index += 1
                body = body.replace(OBJECT_PLACEHOLDER, "\n" + replacement + "\n", 1)
            lines.extend(body.strip("\n").splitlines())
            continue

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
    try:
        uri = f"file:{NOTESTORE.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
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
    except sqlite3.Error as exc:
        raise NoteStoreError(f"cannot read NoteStore (Full Disk Access?): {exc}") from exc

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
        (Folder(pk=pk, name=names[pk], path=path(pk)) for pk in names),
        key=lambda f: f.path,
    )


def note_folder(note_id: str) -> Folder | None:
    """The folder a note lives in, or None if it cannot be determined.

    AppleScript cannot do this: `container of note id X` fails with -1728 in current
    Notes, however the note is addressed. Reading it from NoteStore is exact, and matters
    because edit_note recreates the note and must re-file it where it was -- otherwise
    editing a note quietly moves it to the default folder.
    """
    try:
        uri = f"file:{NOTESTORE.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute(
                "SELECT ZFOLDER FROM ZICCLOUDSYNCINGOBJECT WHERE Z_PK = ?",
                (_note_pk(note_id),),
            ).fetchone()
        if not row or not row[0]:
            return None
        return next((f for f in folders() if f.pk == row[0]), None)
    except (sqlite3.Error, NoteStoreError):
        return None


def read_note_markdown(note_id: str, tables: list[str] | None = None) -> str:
    """Reconstruct a note's markdown from Notes' protobuf.

    `tables` supplies the markdown for each table in the note, in order; they are
    spliced into the U+FFFC placeholders. Callers should pass the tables extracted
    from the AppleScript HTML, which renders them faithfully.
    """
    text, runs = _load(_note_pk(note_id))
    return _render(_paragraphs(text, runs), tables or [])
