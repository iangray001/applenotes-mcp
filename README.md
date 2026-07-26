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
| `create_folder(path)` | Create a folder, making any missing parents. `create_note` still refuses unknown folders. |

The tools are annotated (`readOnlyHint`, `destructiveHint`) and the server contains `instructions` to explain the tools to your model. 

## Limitations

**Editing is destructive.** Due to a variety of Apple-based limitations, editing involves 
reading the source note, creating an edited copy, and deleting the original. This means that:

* the note gets a new internal ID and a new creation date
* this mess up Shared Notes

If the note's structure cannot be properly read from NoteStore then the edit is 
refused rather than run from the degraded HTML. Still, every edit writes a JSON 
backup to `~/.local/share/applenotes-mcp/backups/` first, and 
the original also lands in Notes' Recently Deleted for 30 days so if anything goes wrong
then you can just fish it out of the bin.

**Heading depth is flattened below level 3.** Apple's markdown converter maps `#` to Notes'
*Title* style and `##` to its *Heading* style, both of which round trip intact. `###` maps
to *Heading* as well — Notes has a *Subheading* style (`style_type` 2) but the converter
never emits it so `### Foo` reads back as `## Foo`.

**Links gain a trailing slash.** Notes.app normalises a bare-host URL, so
`https://example.com` comes back as `https://example.com/`.

**Any highlighted text will revert back to normal text.** It is possible to read the highlighting
out of the note store, but there is no way through either Shortcuts or HTML to write a highlight.

**Attachments show as "PDF Document" (or "Image", etc.), not a title.** Notes shows the tile
title from an attachment's `ZTITLE` field, which it only populates when you add a file through
its own UI. Nothing in the automation surface can set it: the *Add File to Note* intent leaves
it empty (its `name` parameter sets only the media filename, which Notes does not display),
there is no rename intent, the attachment entity's name is not writable, and AppleScript cannot
even see intent-created attachments. Attachment display size, by contrast, *is* settable — so
label attachments by the surrounding note text.


## Tests

    uv sync
    uv run pytest

This will test all the normal code logic, renderers, AppleScript use etc. The test 
suite also includes `tests/fixtures/*.zdata` which are real note protobufs that Notes.app 
itself wrote, paired with the AppleScript HTML and the expected markdown. 
These can be regenerated with `uv run python tests/capture_fixtures.py`, which 
writes `.actual` files that you then promote to `.expected` by hand if everything looks correct.

The suite also includes a set of **live round trips** which test the entire flow. These create 
and delete real notes, so they are off by default. Everything happens in a dedicated 
`MCP Live Tests` folder, which must exist and be empty before the tests start.

    APPLENOTES_MCP_LIVE=1 uv run pytest -m live

Both `live` and `APPLENOTES_MCP_LIVE` must be set and both are checked before testing starts.
