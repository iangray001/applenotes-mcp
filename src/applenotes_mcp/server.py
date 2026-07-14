"""MCP server for Apple Notes that writes properly formatted notes.

Writes go through Shortcuts (bridge.py), which is the only route that produces real
headings, lists, tables and checklists; AppleScript's `body` setter mangles all of
them. Reads go through Notes' own protobuf (notestore.py), which is the only source
that knows a checklist from a bullet list. AppleScript is used for what it is good
at: addressing a note exactly by ID, to fetch its HTML, move it, or delete it.

Editing is delete-and-recreate, not in-place. Shortcuts can only write to a note it
can *find*, and there is no way to find a specific note: Find Notes' name filter is
a fuzzy ranked search that returns unrelated notes, its tag filter needs a static
tag compiled into the shortcut, and a note cannot be tagged programmatically in the
first place (AppleScript cannot set tags; a #hashtag written into the body stays
plain text). So `edit_note` reads the old note, deletes it by ID, and recreates it.

That is safe -- it can never touch the wrong note -- but it is destructive, so it is
hedged about: every edit backs the original up to BACKUP_DIR first; notes holding
real attachments (photos, PDFs -- not tables) are refused outright; and if the note's
true structure cannot be read from NoteStore, the edit is refused rather than run
from the degraded HTML, which would silently turn checkboxes into plain bullets.
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
from .attachments import destructible_attachments
from .html_to_markdown import extract_tables, html_to_markdown
from .notestore import Folder, NoteStoreError, folders, note_folder, read_note_markdown

# Surfaced to the client as server-level guidance. This is for what spans the tools and
# so belongs in no single docstring; per-tool detail stays on the tool.
INSTRUCTIONS = """\
Apple Notes, with real rich-text formatting: headings, bullet and numbered lists,
tables, and tickable checklists all survive as genuine Notes objects.

Working with note IDs:
  * A note ID always comes from `search_notes`. Never construct or guess one -- they are
    Core Data URLs (x-coredata://.../ICNote/p123), and a wrong guess addresses a real but
    unrelated note.
  * `search_notes` matches on the TITLE ONLY, not the body. "no matches" therefore means
    no note has that word in its title; it does not mean no note mentions it.

Writing:
  * `create_note` needs an existing folder; this server never creates one. Call
    `list_folders` to see what is available rather than guessing a name.
  * Heading depth is not preserved. Apple's markdown converter maps `##` onto Notes'
    Title style, so `## Foo` reads back as `# Foo`. This is stable, not compounding --
    do not try to correct for it by adding levels.
  * `- [ ]` and `- [x]` produce real, tickable checkboxes, and the ticked state survives a
    round trip. Use them for anything list-like the user might tick off.

Editing:
  * `edit_note` is DESTRUCTIVE: Apple offers no in-place rewrite that keeps formatting, so
    the note is deleted and recreated, and gets a NEW ID and a new creation date. Prefer
    `create_note` whenever the content is genuinely new.
  * To edit, `read_note` first and pass back the full modified markdown -- it replaces the
    body wholesale, so anything omitted is gone.
  * It refuses notes holding photos or PDFs, since recreating the note would destroy them.
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


def _note_field(note_id: str, field: str) -> str:
    return _osascript(f'tell application "Notes" to return {field} of note id {_as_str(note_id)}')


def _ids_with_title(title: str) -> set[str]:
    raw = _osascript(f"""
        tell application "Notes"
            set out to ""
            repeat with n in (notes whose name is {_as_str(title)})
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
            f"no folder named {folder!r}. This server does not create folders; make it "
            "in Notes first. Call list_folders to see what exists."
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

    `create_note` can only file a note into a folder that already exists -- this server
    never creates one -- so call this rather than guessing a folder name.

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

    Args:
        title: the note's title.
        markdown: the body as markdown. A leading `# <title>` is dropped, since Notes
            takes the title from the title argument and would otherwise repeat it.
        folder: optional existing folder to file the note under, given as a name or, where
            the name is not unique, as a full path like "Personal/Projects/Recipes". Call
            list_folders to see what exists; an ambiguous name is rejected, not guessed.

    Returns the new note's ID.
    """
    # Resolve the folder BEFORE writing: the note is created first and moved second, so an
    # unknown or ambiguous name would otherwise leave the note stranded in the default
    # folder while the caller sees an error and assumes nothing happened.
    target = _resolve_folder(folder) if folder else None

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
    protobuf holds them only as a placeholder.

    Heading depth is not preserved, but that is a write-side loss, not a read-side one:
    Apple's markdown converter maps `##` onto Notes' Title style, so `## Foo` was already
    stored as a title and reads back as `# Foo`.

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

    Notes holding attachments are refused, because recreating the note would destroy
    them. Read the note, edit that markdown, and pass it back here.

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

    at_risk = destructible_attachments(note_id)
    if at_risk:
        raise ValueError(
            f"refusing to edit: this note has attachments that recreating it would "
            f"destroy ({', '.join(at_risk)}). Apple offers no in-place rewrite that "
            "preserves formatting. Tables are fine -- they are rebuilt from markdown."
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

    _osascript(f'tell application "Notes" to delete note id {_as_str(note_id)}')
    if folder:
        # By ID, so a note that lived in one of two same-named folders goes back to the
        # one it came from. No need to special-case the default folder: moving a note to
        # the folder it is already in is a no-op.
        _move_note(new_id, folder)

    return f"{new_id} (was {note_id}; backup at {backup})"


@mcp.tool(
    annotations=ToolAnnotations(
        title="Find notes by title",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def search_notes(query: str, limit: int = 10) -> str:
    """Find notes whose TITLE contains `query`. Returns `id<TAB>title` lines.

    This is the only way to obtain a note ID, which `read_note` and `edit_note` both need.

    It does NOT search note bodies. "no matches" means no note has `query` in its title --
    it does not mean no note mentions it, so do not conclude from this that the content
    does not exist.
    """
    raw = _osascript(f"""
        tell application "Notes"
            set out to ""
            repeat with n in (notes whose name contains {_as_str(query)})
                set out to out & (id of n) & tab & (name of n) & linefeed
            end repeat
            return out
        end tell
    """)
    lines = [line for line in raw.splitlines() if line.strip()]
    return "\n".join(lines[:limit]) or "no matches"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
