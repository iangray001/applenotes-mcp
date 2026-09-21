# applenotes-mcp

An MCP server for Apple Notes that writes properly formatted notes with support for 
headings, bulleted and numbered lists, tables, block quotes, code blocks, checklists, and
file attachments (images, PDFs).

## Why this exists

Other Apple Notes MCP servers drive Notes using AppleScript, and this results in 
a number of significant limitations. Writing a note through AppleScript requires 
handing HTML to Notes' importer which ignores the styling and bakes
explicit inline font sizes onto everything it produces. 
Headings are bold spans rather than real headings, font sizes are wrong, and tables, 
checklists, and attachments cannot be created. Better HTML does not help: for example 
an explicit `13px` is rewritten to `11px` by the importer.

This MCP uses Shortcuts.app instead as the method of writing, driving Notes' own App
Intents and avoiding the HTML importer entirely. The markdown is parsed by **Notes itself**,
via the `interpretAsMarkdown` parameter on the *Append to Note* intent — the same parser as
*File > Import Markdown*. Notes written this way contain proper formatting.

That parser is markedly better than the Shortcuts *Make Rich Text from Markdown* action
this used to go through: block quotes, fenced code blocks and horizontal rules survive,
headings land on the right paragraph style, and single-dash table delimiters (`| - |`) are
accepted. Two workarounds for that older converter — rewriting delimiter rows, and sending
each table as its own chunk so the list after it was not destroyed — are gone.

Using Shortcuts also lets us write the things that AppleScript does not (checklists,
attachments, tables...)

## Requirements

Developed on **macOS 26.5**, and currently tested on **macOS 27.0**. The minimum version
is not known because it depends on which Notes App Intents are present. Note that macOS 27
*removes* two capabilities from the default install — see **Ticked checkboxes and
attachment sizes need the full build** under Limitations.

You also need Python 3.13+ and [uv](https://docs.astral.sh/uv/).

### Permissions

_Two macOS permissions must be granted to whichever application runs the server_ - which is
generally your terminal or the Claude Desktop app (i.e. not to Python or to Notes.app). 

* **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access). Reading
  is done straight from Notes' own store at
  `~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite`, which is protected.
  Without it the server still runs but degraded. `read_note` falls back to AppleScript
  HTML, and `edit_note` will refuse to edit.
* **Automation → Notes** (System Settings → Privacy & Security → Automation). Prompted for
  on the first `osascript` call.

## Setup

    uv sync

Register with Claude Code:

    claude mcp add applenotes -- uv run --directory /path/to/applenotes_mcp applenotes-mcp

On first use the server generates and signs the bridge shortcut and asks you to import it.
This is a one-time confirmation that Shortcuts cannot be automated around: open the file it
names and click **Add Shortcut**, then retry. The same happens if you later update the
server and the installed shortcut falls behind — delete the old one and import the new. 

## Installing the full build

Optional, and only worth it if you want ticked checkboxes (`- [x]`) or attachment display
sizes (`![pic|small]`). Everything else works identically on the basic build.

The two actions those need still run perfectly; it is only the Shortcuts app's importer
that rejects them. `tools/wfimport.m` installs a signed shortcut by talking to WorkflowKit
directly, skipping that check:

    clang -fobjc-arc -framework Foundation -o wfimport tools/wfimport.m

Generate the full build, then install it — quit Shortcuts.app first, and delete any
existing *Notes MCP Bridge* so you do not end up with two:

    uv run python -c "from applenotes_mcp import bridge; print(bridge.generate_signed_shortcut(full=True))"
    ./wfimport "$HOME/.local/share/applenotes-mcp/Notes MCP Bridge.shortcut" \
               ~/Library/Shortcuts/Shortcuts.sqlite

**Understand what you are taking on.** This is private Apple SPI writing directly to a
database that syncs to iCloud. It works today and the whole live suite passes against it,
but Apple can change either at any release, and nothing here is supported. Back up
`~/Library/Shortcuts/` first. The server tells the two builds apart by a marker inside the
shortcut, so it always knows which capabilities it has, and updating regenerates whichever
build you installed rather than silently downgrading you.

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
* this messes up Shared Notes

If the note's structure cannot be properly read from NoteStore then the edit is 
refused rather than run from the degraded HTML. Still, every edit writes a JSON 
backup to `~/.local/share/applenotes-mcp/backups/` first, and 
the original also lands in Notes' Recently Deleted for 30 days so if anything goes wrong
then you can just fish it out of the bin.

**Ticked checkboxes and attachment sizes need the full build.** Not because the actions
stopped working, but because macOS 27's Shortcuts will no longer *import* a workflow that
contains them. `com.apple.Notes.SetChecklistItemCheckedLinkActionv2` and
`com.apple.Notes.SetAttachmentSizeLinkAction` are both refused, with only "This shortcut
can't be imported because it contains features not supported on this device" and a log line
reading `Refusing to import shortcut with reasons: <private>`. Every other Notes action the
bridge uses imports fine.

The default (basic) build therefore leaves them out: `- [x]` writes an **unticked** checkbox
and `![pic|small](…)` attaches at the default size. Neither is silent — both are reported as
`WARNING` lines after the returned note ID. Reading is unaffected either way: a note ticked
or resized by hand in Notes.app still reads back correctly. To get them back, install the
full build (below). See NOTES.md for how this was narrowed down.

**Heading depth is flattened below level 4.** Notes' markdown parser maps `#` to Notes'
*Title* style, `##` to *Heading* and `###` to *Subheading*, all of which round trip intact.
`####` maps to *Subheading* as well, so `#### Foo` reads back as `### Foo`.

**Links gain a trailing slash.** Notes.app normalises a bare-host URL, so
`https://example.com` comes back as `https://example.com/`.

**Any highlighted text will revert back to normal text.** It is possible to read the highlighting
out of the note store, but there is no way through either Shortcuts or HTML to write a highlight.

**Attachments show as "PDF Document" (or "Image", etc.), not a title.** Notes shows the tile
title from an attachment's `ZTITLE` field, which it only populates when you add a file through
its own UI. Nothing in the automation surface can set it: the *Add File to Note* intent leaves
it empty (its `name` parameter sets only the media filename, which Notes does not display),
there is no rename intent, the attachment entity's name is not writable, and AppleScript cannot
even see intent-created attachments. Label attachments by the surrounding note text instead.


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

## Licence

MIT — see [LICENSE](LICENSE).
