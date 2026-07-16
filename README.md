# applenotes-mcp

An MCP server for Apple Notes that writes properly formatted notes with support for 
headings, bulleted and numbered lists, tables, ticked checklists, and file attachments
(images, PDFs).

## Why this exists

Other Apple Notes MCP servers drive Notes using AppleScript, and this results in 
a number of significant limitations. Writing a note through AppleScript requires 
handing HTML to Notes' importer which ignores the styling and bakes
explicit inline font sizes onto everything it produces. 
Headings are bold spans rather than real headings, font sizes are wrong, and tables, 
checklists, and attachments cannot be created. Better HTML does not help: for example 
an explicit `13px` is rewritten to `11px` by the importer.

This MCP uses Shortcuts.app instead as the method of writing. Its built-in 
*Make Rich Text from Markdown* action produces a properly attributed string, 
and Notes' *Append to Note* App Intent ingests that natively,
avoiding the HTML importer. Notes written this way contain proper formatting.

Using Shortcuts also lets us write the things that AppleScript does not (checklists, 
attachments, tables...)

## Setup

    uv sync

Register with Claude Code:

    claude mcp add applenotes -- uv run --directory /path/to/applenotes_mcp applenotes-mcp

On first use the server generates and signs the bridge shortcut and asks you to import it. 

## Tools

| Tool | Purpose |
| --- | --- |
| `create_note(title, markdown, folder?)` | Create a formatted note, attachments and all. Returns its ID. |
| `read_note(note_id)` | Read a note back as markdown. |
| `edit_note(note_id, markdown, title?)` | Replace a note's body. **Destructive** — see below. |
| `search_notes(query)` | Find notes by **title**. Returns `id<TAB>title<TAB>folder<TAB>modified<TAB>snippet`. |
| `search_note_text(query)` | **Full-text** search over title and body. Same rows as `search_notes`. |
| `list_folders()` | The folders a note can be filed into, as full paths. |
| `list_folder(folder)` | Browse one folder: its notes (newest first) and immediate subfolders. |

The tools are annotated (`readOnlyHint`, `destructiveHint`) and the server contains `instructions` to explain the tools to your model. 

## Limitations

**Editing is destructive.** Shortcuts can only write to a note it can "find", and there is
no guaranteed way to find a specific note with Shortcuts:

* `Find Notes` by **name** is a fuzzy ranked search, not a substring match.
* `Find Notes` by **tag** needs a *static* tag entity (`applenotes:tag/foo`) compiled into
  the shortcut; the dynamic slot will not coerce a string into one.

`edit_note` instead reads the note, deletes it by ID (exact, via AppleScript) and 
recreates it through the bridge. It will not touch the wrong note, but: 

* the note gets a **new ID and a new creation date**
* this will also mess with Shared Notes

If the note's structure cannot be properly read from NoteStore then the edit is 
**refused** rather than run from the degraded HTML.

Every edit writes a JSON backup to `~/.local/share/applenotes-mcp/backups/` first, and 
the original also lands in Notes' Recently Deleted for 30 days.

**Heading depth is flattened below level 3.** Apple's markdown converter maps `#` to Notes'
*Title* style and `##` to its *Heading* style, both of which round trip intact. `###` maps
to *Heading* as well — Notes has a *Subheading* style (`style_type` 2) but the converter
never emits it so `### Foo` reads back as `## Foo`.

**Links gain a trailing slash.** Notes.app normalises a bare-host URL, so
`https://example.com` comes back as `https://example.com/`.


## Testing

    uv sync            # installs the dev group (pytest)
    uv run pytest      # the hermetic tiers -- safe, ~0.5s

The suite is in three tiers, because the code has an awkward split: most of the logic is
pure and trivially testable, but the parts that matter most (`edit_note`) delete real notes
from a library that syncs to iCloud, where a bug in a *test* could destroy data.

**Tier 1 — pure functions.** `split_blocks` (including file-reference detection and the
per-file input index), the table-delimiter widening, the protobuf renderer
(`_paragraphs`/`_render`/`_inline`, image/table placeholders), the HTML scraper, the
empty-title guard, and the `edit_note` safety invariants driven with fakes (refuses a
degraded read, refuses an undownloaded attachment, allows on-disk ones, backs up the
*true* markdown, never deletes the
note it just created). No Notes, no NoteStore.

**Tier 2 — golden fixtures.** `tests/fixtures/*.zdata` are real note protobufs captured from
notes Notes itself wrote, paired with the AppleScript HTML and the expected markdown. The
reader is run against them with no database and no Notes.app anywhere in the loop, so the
whole read path is pinned deterministically. Regenerate with
`uv run python tests/capture_fixtures.py`, which writes `.actual` files you promote to
`.expected` by hand — a captured bug must not be blessed as correct automatically.

Tiers 1 and 2 also carry `tests/test_apple_conversions.py`: assertions about what Apple's
markdown converter did to our input (`###` flattens to `##`, a bare URL gains a trailing
slash). A failure there is *news about Apple*, not a regression in this code — and possibly
a sign that a workaround can be deleted.

**Tier 3 — live round trips** (`tests/test_live.py`, `tests/test_mcp_contract.py`). The
properties nothing else can check: that a note written by the bridge and read back through
the protobuf agree, that reading-then-rewriting reaches a fixed point (`f(f(x)) == f(x)`)
rather than drifting on every edit, and that a local image referenced in the markdown is
attached inline and byte-for-byte. These create and delete real notes, so they are **doubly
gated** and off by default:

    APPLENOTES_MCP_LIVE=1 uv run pytest -m live

Both gates must be set — the `live` marker is deselected by `pytest` by default, *and* every
live test is skipped unless `APPLENOTES_MCP_LIVE=1`. The guard rails matter more than the
assertions they protect:

* everything happens in a dedicated `MCP Live Tests` folder;
* teardown deletes *scoped to that folder by name*, an AppleScript structurally incapable of
  reaching a note outside it;
* the folder must be found **empty at the start**, or the run refuses — a blanket delete may
  only ever run against a folder we know holds nothing but notes this session created, so a
  non-empty folder (real notes, or leftovers from a hard-killed run) has to be cleared by
  hand first;
* teardown asserts the folder is empty afterwards, so a half-finished cleanup fails loudly.

The MCP contract test is in this file group but is itself hermetic: it spawns the server
over stdio and snapshots what a client sees — tool names, input schemas, the
`readOnly`/`destructive` annotations, the server instructions — without touching any note.

Two tests are `xfail(strict=True)`, marking known bugs so the suite tells us the moment one
is fixed: both are markdown-inside-a-code-fence cases (`- [ ]` and `| - |` inside a fence
being treated as real markup).
