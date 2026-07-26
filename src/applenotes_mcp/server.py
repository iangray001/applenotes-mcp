"""MCP server for Apple Notes that writes properly formatted notes.

For implementation details and design rationale, see NOTES.md
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import bridge
from .attachments import unresolved_attachments
from .html_to_markdown import extract_tables, html_to_markdown
from .notestore import (
    Folder,
    FolderListing,
    NoteDetails,
    NoteStoreError,
    _note_pk,
    folder_contents,
    folder_id_for_pk,
    folders,
    note_details,
    note_folder,
    note_id_for_pk,
    read_note_markdown,
    search_titles,
)

# Surfaced to the client as server-level guidance. This is for what spans the tools and
# so belongs in no single docstring; per-tool detail stays on the tool.
INSTRUCTIONS = """\
Apple Notes, with real rich-text formatting: headings, bullet and numbered lists,
tables, and tickable checklists all survive as genuine Notes objects.

Working with note IDs:
  * A note ID always comes from a search (`search_notes`, `search_note_text`) or from
    `list_folder`. Never construct or guess one -- they are Core Data URLs
    (x-coredata://.../ICNote/p123), and a wrong guess addresses a real but unrelated note.
  * `search_notes` matches on the TITLE ONLY. To find notes that *mention* something in
    their body, use `search_note_text` (full-text over title and body). Reach for
    `search_notes` when you know the title -- it is faster -- and `search_note_text` when
    you don't. If a title search comes up empty, try `search_note_text` before telling the
    user a note does not exist. Both return the same rows and take the same id.
  * Each search row carries the note's folder, modification date and a body snippet
    alongside its id. When several notes share a title, use those to let the user pick the
    right one rather than guessing.

Writing:
  * `create_note` needs an EXISTING folder and refuses an unknown name (so a typo never
    files a note somewhere by surprise). Call `list_folders` to see what exists. To make a
    new folder, call `create_folder(path)` first -- it creates any missing parents -- then
    create the note into it.
  * The `title` argument is the note's title -- the bold first line Apple shows in the
    notes list. Give a real one; do NOT also repeat it as a `# <title>` heading at the top
    of the markdown.
  * For section headings inside the body use `##`/`###`, not `#`. Apple's `#` is the Title
    style, so a `#` heading in the body renders as a second title. `#` and `##` round-trip
    intact; only `###` and deeper flatten (to `##`), stably -- do not add levels to correct
    for it.
  * `- [ ]` and `- [x]` produce real, tickable checkboxes, and the ticked state survives a
    round trip. Use them for anything list-like the user might tick off.
  * A whole-line `![alt](/local/path)` or `[name](/local/path)` attaches that local file
    (image, PDF, ...) at that point, of any byte size. An http(s) link stays a link. Add a
    display size with a pipe -- `![alt|small](...)` -- one of small / medium / large; omit
    it for the default. This round-trips: `read_note` emits the same `|size`.
  * Writes are SLOW and SERIALISED: each `create_note`/`edit_note` drives a Shortcuts run
    taking several seconds, and the server processes them one at a time. Issuing many write
    calls in a single parallel batch gains no speed -- they just queue, and the later ones
    may hit the client's tool timeout. Create notes one at a time, waiting for each to
    return before starting the next.

Editing:
  * `edit_note` is DESTRUCTIVE: Apple offers no in-place rewrite that keeps formatting, so
    the note is deleted and recreated, and gets a NEW ID and a new creation date. Prefer
    `create_note` whenever the content is genuinely new.
  * To edit, `read_note` first and pass back the full modified markdown -- it replaces the
    body wholesale, so anything omitted is gone. Keep an attachment's `![](file://...)` line
    to preserve it; drop it to remove it. Editing is refused only when a note has an
    attachment that is not downloaded from iCloud (it cannot be re-attached).
"""

mcp = FastMCP("applenotes", instructions=INSTRUCTIONS)

BACKUP_DIR = Path.home() / ".local" / "share" / "applenotes-mcp" / "backups"


def _osascript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "AppleScript failed")
    return result.stdout.strip()


def _as_str(value: str) -> str:
    """Quote a Python string for interpolation into AppleScript source."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _strip_leading_title(title: str, markdown: str) -> str:
    """Drop a leading `# Title` duplicating the note title, which Notes sets itself."""
    lines = markdown.lstrip().splitlines()
    if lines and re.fullmatch(rf"#\s+{re.escape(title.strip())}\s*", lines[0]):
        return "\n".join(lines[1:]).lstrip()
    return markdown


def _promote_title(markdown: str) -> tuple[str, str]:
    """Derive a title from the markdown's first meaningful line; return (title, remainder).

    Apple Notes has no separate title field -- the first line of a note IS its title. When
    the caller gives a blank title we make that first line explicit rather than let Notes
    pick: it skips images and table rows, so a note is never titled by its header image or
    a table, and takes the first heading or line of prose instead. The line is removed from
    the body so it is not then repeated beneath the title.
    """
    lines = markdown.strip().splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("|") or bridge.file_ref(stripped):
            continue
        title = re.sub(r"^#+\s+", "", stripped)  # heading marker
        title = re.sub(r"^[-*]\s+(\[[ xX]\]\s+)?", "", title)  # list / checklist marker
        title = re.sub(r"[*`_]", "", title).strip()[:200]  # inline emphasis
        return title or "New Note", "\n".join(lines[:i] + lines[i + 1 :])
    return "New Note", markdown


def _note_field(note_id: str, field: str) -> str:
    return _osascript(f'tell application "Notes" to return {field} of note id {_as_str(note_id)}')


def _ids_with_title(title: str) -> set[str]:
    """Ids of notes whose title matches `title`, used to spot the one the bridge just made.

    Match on a PREFIX of the first line, not the exact title. Notes stores a note's `name`
    as its first line, truncated (to ~64 chars plus an ellipsis) once it is long enough, so
    `name is <title>` misses any long or multi-line title -- which made create_note fail to
    find the note it had just created and, because that raised before the folder move,
    strand it in the default folder. The first 40 characters always survive, and
    `_create_and_identify`'s before/after diff still pins the note uniquely.
    """
    prefix = (title.splitlines() or [""])[0][:40]
    raw = _osascript(f"""
        tell application "Notes"
            set out to ""
            repeat with n in (notes whose name begins with {_as_str(prefix)})
                set out to out & (id of n) & linefeed
            end repeat
            return out
        end tell
    """)
    return {line.strip() for line in raw.splitlines() if line.strip()}


def _create_and_identify(title: str, markdown: str) -> str:
    """Create a note via the bridge and return ITS id.

    Identified by diffing the ids carrying this title before and after. Matching on
    title alone is not safe: during an edit the old note is still present with the
    same title, and picking the first match returns the note we are about to delete.
    """
    before = _ids_with_title(title)
    bridge.run(title=title, markdown=_strip_leading_title(title, markdown))
    created = _ids_with_title(title) - before

    if len(created) != 1:
        raise RuntimeError(
            f"expected exactly one new note titled {title!r}, found {len(created)}; "
            "refusing to guess which one it is"
        )
    return created.pop()


def _resolve_folder(folder: str) -> Folder:
    """Resolve a folder path or name to exactly one folder, or refuse.

    Folder names are NOT unique -- nesting allows both "Personal/Recipes" and
    "Personal/Projects/Brewing/Recipes" -- and AppleScript's `folder "Recipes"` quietly
    picks one of them. So an ambiguous name is an error here rather than a coin flip:
    filing a note into the wrong folder is silent, and the user may never find it again.
    """
    known = folders()
    matches = [f for f in known if f.path == folder] or [f for f in known if f.name == folder]

    if not matches:
        raise ValueError(
            f"no folder named {folder!r}. Call list_folders to see what exists, or "
            "create_folder to make this one (it creates any missing parents), then retry."
        )
    if len({f.path for f in matches}) > 1:
        paths = ", ".join(sorted(f.path for f in matches))
        raise ValueError(
            f"{folder!r} is ambiguous -- {len(matches)} folders share that name: {paths}. "
            "Pass the full path instead of the bare name."
        )
    if len(matches) > 1:
        # Same name AND same parent, so the path cannot separate them either. Notes does
        # allow this (three top-level "New Folder"s, say). Nothing sensible to pick.
        raise ValueError(
            f"{len(matches)} different folders have the exact path {matches[0].path!r}. "
            "They cannot be told apart, so this note will not be filed into any of them; "
            "rename or remove the duplicates in Notes first."
        )
    return matches[0]


def _move_note(note_id: str, folder: Folder) -> None:
    """Move a note into a folder, addressing the folder by ID rather than by name.

    `move note ... to folder "Recipes"` would let AppleScript choose between same-named
    folders. The ID is exact. It can be derived from the note's own ID because every
    object in the library shares one Core Data store UUID:
        x-coredata://<uuid>/ICNote/p123  ->  x-coredata://<uuid>/ICFolder/p456
    """
    store, _, _ = note_id.partition("/ICNote/")
    folder_id = f"{store}/ICFolder/p{folder.pk}"
    _osascript(
        f'tell application "Notes" to move note id {_as_str(note_id)} '
        f"to folder id {_as_str(folder_id)}"
    )


def _backup(note_id: str, title: str, html: str, markdown: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^\w.-]+", "_", title)[:60] or "note"
    path = BACKUP_DIR / f"{stamp}-{safe}.json"
    path.write_text(
        json.dumps(
            {"note_id": note_id, "title": title, "html": html, "markdown": markdown},
            indent=2,
        )
    )
    return path


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Notes folders",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def list_folders() -> str:
    """List the folders a note can be filed into, as full paths, one per line.

    `create_note` can only file a note into a folder that already exists -- it never creates
    one -- so call this rather than guessing a folder name. To file into a folder that is not
    listed here, call `create_folder` first.

    Paths are shown because folder names are NOT unique: nesting allows both
    "Personal/Recipes" and "Personal/Projects/Brewing/Recipes". Pass the full path to
    `create_note` whenever a bare name appears more than once here; an ambiguous name is
    rejected rather than guessed at.
    """
    counts = Counter(f.path for f in folders())
    lines = [
        path
        if counts[path] == 1
        else f"{path}  [!! {counts[path]} distinct folders share this exact path -- "
        "cannot be filed into; rename them in Notes]"
        for path in sorted(counts)
    ]
    return "\n".join(lines) or "no folders"


def _format_listing(path: str, listing: FolderListing) -> str:
    """Render a folder's immediate subfolders (as paths) and notes (as search-style rows)."""
    out = [f"Folder: {path}"]

    out.append(f"\nSubfolders ({len(listing.subfolders)}):")
    out += [f"  {f.path}" for f in listing.subfolders] or ["  (none)"]

    out.append(f"\nNotes ({len(listing.notes)}):")
    out += [
        "  " + "\t".join([note_id, title, modified, snippet])
        for note_id, title, modified, snippet in listing.notes
    ] or ["  (none)"]

    return "\n".join(out)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List a folder's contents",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def list_folder(folder: str) -> str:
    """List the notes and immediate subfolders inside `folder`.

    `folder` is a path (or a name, where unique) as shown by `list_folders`; an ambiguous
    name is rejected rather than guessed at. Subfolders are given as full paths (pass one
    back here to descend into it). Notes are listed newest first, one per line, tab-separated
    as `id`, `title`, `modified` (`YYYY-MM-DD HH:MM`), `snippet` -- the same `id` that
    `read_note` and `edit_note` take. This is how to browse the library by structure, where
    `search_notes`/`search_note_text` find notes by their text.
    """
    target = _resolve_folder(folder)
    return _format_listing(target.path, folder_contents(target.pk))


@mcp.tool(
    annotations=ToolAnnotations(
        title="Create a folder",
        readOnlyHint=False,
        destructiveHint=False,  # only ever adds folders; never removes or renames one
        idempotentHint=True,  # a folder that already exists is left as is
        openWorldHint=False,
    )
)
def create_folder(path: str) -> str:
    """Create a folder at `path`, making any missing parent folders along the way.

    `path` is a full path like "Personal/Projects/Roadmap"; each segment that does not exist
    yet is created under the one before it (the first under the top level). A path that
    already exists is left untouched. This is separate from `create_note` on purpose:
    `create_note` still REFUSES an unknown folder, so a typo files nothing by surprise --
    call this first to make the folder deliberately, then create the note into it.

    Folders are addressed by id (built from the store UUID), so nesting under a folder whose
    name is not unique still works. Refuses if a parent path is itself ambiguous.
    """
    segments = [s.strip() for s in path.split("/") if s.strip()]
    if not segments:
        raise ValueError("empty folder path")

    all_folders = folders()
    parent_id: str | None = None
    depth = 0  # how many leading segments already exist
    for i in range(len(segments)):
        prefix = "/".join(segments[: i + 1])
        matches = [f for f in all_folders if f.path == prefix]
        if len(matches) > 1:
            raise ValueError(f"{prefix!r} is ambiguous ({len(matches)} folders); cannot create under it")
        if not matches:
            break
        parent_id = folder_id_for_pk(matches[0].pk)
        depth = i + 1

    if depth == len(segments):
        return f"already exists: {path}"

    for seg in segments[depth:]:
        at = f" at folder id {_as_str(parent_id)}" if parent_id else ""
        parent_id = _osascript(
            f'tell application "Notes" to return id of '
            f"(make new folder with properties {{name:{_as_str(seg)}}}{at})"
        )
    return f"created: {'/'.join(segments)}"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Create a formatted note",
        readOnlyHint=False,
        destructiveHint=False,  # only ever adds a new note; touches nothing existing
        idempotentHint=False,  # calling twice makes two notes
        openWorldHint=False,
    )
)
def create_note(title: str, markdown: str, folder: str | None = None) -> str:
    """Create an Apple Note from markdown, preserving real formatting.

    Headings, bullet and numbered lists, tables, bold and italic all survive as real
    Notes objects -- unlike AppleScript-based servers, which force the whole note to
    a fixed font size and cannot produce tables at all.

    Attachments: a whole-line image `![alt](/path/to/file)` or link `[name](/path)` whose
    target is a LOCAL file (an absolute path or a file:// URL) is attached to the note at
    that point, of any byte size. An http(s) link stays an ordinary link. A display size may
    be added after a pipe -- `![alt|small](...)` -- as small, medium or large.

    Args:
        title: the note's title -- the bold first line Apple shows in the notes list, not
            just metadata. Give it a real, descriptive value. If left blank, it is taken
            from the first heading or line of prose in the markdown (never an image).
        markdown: the body. Do NOT repeat the title as a `# <title>` heading at the top --
            Notes already shows the title, and a leading one matching `title` is dropped.
            For section headings inside the note use `##` and `###`: `#` is Apple's *Title*
            style, so a `#` heading renders as a second title. `## Foo` round-trips as
            `## Foo`; only `###` and deeper flatten (to `##`).
        folder: optional existing folder to file the note under, given as a name or, where
            the name is not unique, as a full path like "Personal/Projects/Recipes". Call
            list_folders to see what exists; an ambiguous name is rejected, not guessed.

    Returns the new note's ID.
    """
    # Resolve the folder BEFORE writing: the note is created first and moved second, so an
    # unknown or ambiguous name would otherwise leave the note stranded in the default
    # folder while the caller sees an error and assumes nothing happened.
    target = _resolve_folder(folder) if folder else None

    # A blank title would let Notes title the note from whatever lands first -- including a
    # header image. Derive a text title from the markdown instead.
    if not title.strip():
        title, markdown = _promote_title(markdown)

    note_id = _create_and_identify(title, markdown)
    if target:
        _move_note(note_id, target)
    return note_id


@mcp.tool(
    annotations=ToolAnnotations(
        title="Read a note as markdown",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def read_note(note_id: str) -> str:
    """Read a note as markdown, for inspecting or editing it.

    Reconstructed from Notes' own protobuf, so checklists come back with their ticked
    state, and a numbered list is not confused with a bullet list -- neither of which
    the HTML AppleScript exports can express. Tables are taken from the HTML, since the
    protobuf holds them only as a placeholder. File attachments (images, PDFs) come back
    as `![name](file://...)` / `[name](file://...)` pointing at the file on disk.

    Heading depth is preserved through `##`; only `###` and deeper are flattened (to `##`),
    and that is a write-side loss, not a read-side one -- `### Foo` was already stored as a
    Heading. `#` is Apple's Title style, so the note's own title reads back as `# <title>`.

    Falls back to the HTML alone if the protobuf cannot be read, which loses checklists;
    `edit_note` refuses to run in that state rather than rewrite from a degraded read.
    """
    html = _note_field(note_id, "body")
    try:
        return read_note_markdown(note_id, tables=extract_tables(html))
    except NoteStoreError:
        return html_to_markdown(html)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Replace a note's body (destructive)",
        readOnlyHint=False,
        # The note is deleted and recreated: it loses its ID and creation date, and the
        # old body is gone. Clients should prompt for this one even if the server is
        # otherwise allowlisted.
        destructiveHint=True,
        idempotentHint=False,  # each call mints a new note ID, so a retry is not a no-op
        openWorldHint=False,
    )
)
def edit_note(note_id: str, markdown: str, title: str | None = None) -> str:
    """Replace a note's body with new markdown, keeping its title and folder.

    IMPORTANT -- this is destructive. Apple provides no way to rewrite a note in
    place with real formatting, so the note is deleted and recreated: it gets a NEW
    note ID and a new creation date. The original is backed up to
    ~/.local/share/applenotes-mcp/backups first.

    Attachments are preserved: keep their `![name](file://...)` / `[name](file://...)`
    lines in the markdown you pass back and they are re-attached; drop a line to remove that
    attachment. The one exception is an attachment that is not downloaded locally (evicted
    to iCloud) -- the note is refused, since that file cannot be re-attached. The normal flow
    is to `read_note`, edit that markdown, and pass it back here.

    Returns the new note's ID.
    """
    old_html = _note_field(note_id, "body")
    old_title = _note_field(note_id, "name")
    folder = note_folder(note_id)

    # Editing rewrites the note from what we read, so a degraded read is not acceptable
    # here: the HTML fallback cannot see checklists (a ticked box and a plain bullet are
    # both just <li>text</li>), and rewriting from it would silently flatten them. Reading
    # for display can degrade; editing may not.
    try:
        old_markdown = read_note_markdown(note_id, tables=extract_tables(old_html))
    except NoteStoreError as exc:
        raise RuntimeError(
            f"refusing to edit: cannot read this note's true structure from NoteStore "
            f"({exc}). Editing rewrites the note, and the HTML fallback cannot see "
            "checklists, so any checkboxes would be silently turned into plain bullets. "
            "Grant Full Disk Access, or read the note and create a new one instead."
        ) from exc

    # Attachments survive the rewrite: read_note emitted a file:// path for each, so the
    # markdown re-attaches them, and the create-before-delete order below keeps the source
    # files alive until the copies are made. The exception is a file that is NOT on disk
    # (evicted to iCloud): it read back as a marker, not a path, so recreating would drop it.
    # Refuse only that case.
    stranded = unresolved_attachments(_note_pk(note_id))
    if stranded:
        raise ValueError(
            f"refusing to edit: {len(stranded)} attachment(s) are not downloaded locally "
            f"({', '.join(stranded)}), so they cannot be re-attached and recreating the "
            "note would drop them. Open the note in Notes to download them, then retry."
        )

    new_title = title or old_title
    # Back up the NoteStore markdown, not html_to_markdown(old_html): the backup is the
    # only safety net on a destructive operation, so it must not be the degraded read that
    # this function has just refused to edit from.
    backup = _backup(note_id, old_title, old_html, old_markdown)

    try:
        new_id = _create_and_identify(new_title, markdown)
    except RuntimeError as exc:
        raise RuntimeError(
            f"replacement note not created, so the original is untouched: {exc}. "
            f"Backup: {backup}"
        ) from exc

    if new_id == note_id:
        raise RuntimeError("internal error: replacement resolved to the original note")

    # ORDER MATTERS: create the replacement BEFORE deleting the original. Beyond keeping the
    # original intact if creation fails, this is what could make editing attachment-bearing
    # notes work: read_note emits file:// paths to the original's on-disk Media files, so the
    # replacement can re-attach them (Notes copies on attach) while the original -- and thus
    # those files -- still exists. Delete first and those paths would be dangling. If the
    # attachment refusal above is ever relaxed, do not reorder these two steps.
    _osascript(f'tell application "Notes" to delete note id {_as_str(note_id)}')
    if folder:
        # By ID, so a note that lived in one of two same-named folders goes back to the
        # one it came from. No need to special-case the default folder: moving a note to
        # the folder it is already in is a no-op.
        _move_note(new_id, folder)

    return f"{new_id} (was {note_id}; backup at {backup})"


def _format_search(matches: list[tuple[str, str]], details: dict[int, NoteDetails]) -> str:
    """One tab-separated row per match: id, title, folder, modified, snippet.

    The extra columns are what let a caller tell apart notes that share a title -- two
    "Recipes", one in `Personal/Recipes` edited today, one in `Work/Cakes` edited in March.
    Missing detail (an unreadable NoteStore) just leaves those columns blank.
    """
    rows = []
    for note_id, title in matches:
        try:
            d = details.get(_note_pk(note_id))
        except NoteStoreError:
            d = None
        folder, modified, snippet = (d.folder, d.modified, d.snippet) if d else ("", "", "")
        rows.append("\t".join([note_id, title, folder, modified, snippet]))
    return "\n".join(rows) or "no matches"


def _applescript_search(field: str, query: str) -> list[tuple[str, str]]:
    """`(id, title)` for notes whose <field> contains <query>, via AppleScript.

    `field` is a fixed literal (`name` or `plaintext`), never user input, so it is safe to
    interpolate; only `query` is quoted.
    """
    raw = _osascript(f"""
        tell application "Notes"
            set out to ""
            repeat with n in (notes whose {field} contains {_as_str(query)})
                set out to out & (id of n) & tab & (name of n) & linefeed
            end repeat
            return out
        end tell
    """)
    matches: list[tuple[str, str]] = []
    for line in raw.splitlines():
        note_id, _, title = line.partition("\t")
        if note_id.strip():
            matches.append((note_id.strip(), title))
    return matches


def _enrich_and_format(matches: list[tuple[str, str]], limit: int) -> str:
    """Sort by modification date (newest first), keep `limit`, and format the rows.

    Enrich all matches, THEN sort, THEN cut -- so `limit` really is the most recent N, not
    the first N the search happened to return.
    """
    try:
        details = note_details([_note_pk(nid) for nid, _ in matches])
    except NoteStoreError:
        details = {}
    matches = sorted(
        matches,
        key=lambda m: details.get(_note_pk(m[0]), NoteDetails("", "", "")).modified,
        reverse=True,
    )[:limit]
    return _format_search(matches, details)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Find notes by title",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def search_notes(query: str, limit: int = 10) -> str:
    """Find notes whose TITLE contains `query`, most recently modified first.

    Returns one row per match, tab-separated: `id`, `title`, `folder`, `modified`
    (`YYYY-MM-DD HH:MM`), `snippet` (a one-line body preview). The `id` is the only way to
    obtain a note ID, which `read_note` and `edit_note` both need; the other columns are for
    telling apart notes that share a title -- present several to the user by folder and date
    rather than guessing which is meant.

    This searches TITLES only and is fast. If it returns no matches -- or you are after
    notes that *mention* something rather than are titled after it -- fall back to
    `search_note_text`, which also searches bodies. "no matches" here never means the
    content is absent, only that no title contains `query`.
    """
    # Title search is a pure NoteStore query, so it works even when Notes.app is hung. If the
    # database cannot be read (no Full Disk Access), fall back to AppleScript, which does not
    # need it -- enrichment columns just come back blank in that case.
    try:
        matches = [(note_id_for_pk(pk), title) for pk, title in search_titles(query)]
    except NoteStoreError:
        matches = _applescript_search("name", query)
    return _enrich_and_format(matches, limit)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Full-text search notes",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def search_note_text(query: str, limit: int = 10) -> str:
    """Full-text search: find notes whose TEXT (title and body) contains `query`.

    Same row format as `search_notes` (`id`, `title`, `folder`, `modified`, `snippet`),
    most recently modified first. Use this when looking for notes that *mention* something
    rather than notes titled after it; use `search_notes` when you know the title, as it is
    faster. Matching is on the note's plain text, so formatting (checklists, tables) does not
    affect what matches.
    """
    return _enrich_and_format(_applescript_search("plaintext", query), limit)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
