# applenotes-mcp

An MCP server for Apple Notes that writes **properly formatted** notes: real headings,
real bullet and numbered lists, real tables, tickable checklists, and file attachments
(images, PDFs) of any size.

## Why this exists

Every other Apple Notes MCP server drives Notes using AppleScript.
That path hands HTML to Notes' importer which ignores the styling and bakes
explicit inline font sizes onto everything it produces — 11px body text, 15px headings.
Headings arrive as bold spans rather than real headings, and tables and checklists are 
unreachable entirely. Better HTML does not help: an explicit `13px` is
rewritten to `11px` by the importer.

This MCP uses Shortcuts. Its built-in **Make Rich Text from Markdown** action produces a
genuine attributed string, and Notes' **Append to Note** App Intent ingests that natively,
never touching the HTML importer. Notes written this way contain real `<h1>`/`<h2>`, real
`<ul>`/`<ol>`, and real Notes table objects, with no font-size pollution.


## Setup

    uv sync

Register with Claude Code:

    claude mcp add applenotes -- uv run --directory /path/to/applenotes_mcp applenotes-mcp

On first use the server generates and signs the bridge shortcut and asks you to import it
(a one-time double-click; Shortcuts cannot be installed without user confirmation). When the
bridge workflow itself changes (e.g. gaining attachment support), delete the old
`Notes MCP Bridge` in the Shortcuts app first, then re-import — importing over a same-named
shortcut makes a `Notes MCP Bridge 2` duplicate rather than replacing it.

## Tools

| Tool | Purpose |
| --- | --- |
| `create_note(title, markdown, folder?)` | Create a formatted note, attachments and all. Returns its ID. |
| `read_note(note_id)` | Read a note back as markdown. |
| `edit_note(note_id, markdown, title?)` | Replace a note's body. **Destructive** — see below. |
| `search_notes(query)` | Find notes by **title**. Returns `id<TAB>title<TAB>folder<TAB>modified<TAB>snippet`. |
| `search_note_text(query)` | **Full-text** search over title and body. Same rows as `search_notes`. |
| `list_folders()` | The folders a note can be filed into, as full paths. |

The tools are annotated (`readOnlyHint`, `destructiveHint`), so a client can prompt for
`edit_note` while auto-approving the readers, and the server ships `instructions` covering
what spans the tools: IDs only ever come from a search; `search_notes` is title-only (use
`search_note_text` to search bodies); prefer `create_note` over the
destructive `edit_note`.




## How it works

**Writing** goes through a generated Shortcut (`Notes MCP Bridge`), driven headlessly by
`shortcuts run`. The markdown is split into ordered blocks — prose, individual checklist
items, tables, and file attachments — and the shortcut walks them, handing each to the
relevant Notes intent:

    Create Note (plain title)
    Repeat with each block:
        checklist  → Append Checklist Item   (+ Set Checklist Items Checked if ticked)
        file       → Add File to Note         (the file fetched by index from the inputs)
        otherwise  → Make Rich Text from Markdown → Append to Note

The note is built piecewise because checklists and attachments can only be *appended*: each
part is handed to its intent in order, so ordering the appends is what positions the
content. (Converting an empty string to rich text and appending that first also seeds the
accumulator as an attributed string; without it, everything appended is coerced to plain
text and the formatting is lost.)

**Attachments** ride in as extra `shortcuts run` inputs, not in the JSON. The server invokes
`shortcuts run "Notes MCP Bridge" -i payload.json -i file1 -i file2 …`, so input item 1 is
the JSON and each file follows; a `file` block carries the input index of its file, and the
shortcut fetches it with *Get Item from List* and calls **Add File to Note**. A whole-line
`![alt](/local/path)` or `[name](/local/path)` in the markdown (a `file://` URL or an
absolute path — an http link stays a link) becomes such a block, attached inline at its
position. This roundabout route is forced: there is **no** way to hand the intent a
filesystem *path* headlessly (see Limitations), but the `shortcuts` CLI reads the file with
your own permissions and passes its bytes in as input, so there is no sandbox and no size
limit — a multi-megabyte photo attaches byte-for-byte.

**Reading** reads from Apple's on-disk store directly. Notes are stored as a protobuf (`notestore.py`). This is done because both of the more obvious read paths are
lossy in different ways. AppleScript can convert notes to HTML, but it cannot see checklists at all (a ticked
box and a plain bullet are both just `<li>text</li>`) and it also merges a numbered list into a
preceding bullet list. Meanwhile Shortcuts' *Make Markdown from Rich Text* destroys tables and flattens
headings. The protobuf in `ZICNOTEDATA.ZDATA` has paragraph styles, checklist
state, inline formatting etc. so we read that (read-only) and reconstruct markdown.

Inline objects appear in the protobuf text only as a `U+FFFC` placeholder, but the
attribute run carrying each one records its identifier and type UTI. That is enough to
handle every kind:

* **Tables** hold their content as a CRDT in a separate object, not in the note protobuf,
  so their text is taken from the AppleScript HTML (which renders tables faithfully) and
  spliced into the placeholder.
* **Files** (images, PDFs, …) are resolved to their path on disk. The attachment's
  identifier joins to a media row (`ZMEDIA`) carrying a filename, and the file lives at
  `<container>/Media/<media-id>/<generation-dir>/<filename>`. `read_note` emits an image
  as `![name](file://…)` and any other file as `[name](file://…)`; a file present in the
  note but not downloaded from iCloud becomes a visible `[attachment not downloaded: …]`
  marker rather than a broken link.

Reading the UTI per placeholder is also what keeps them aligned: an image sitting before a
table no longer consumes the table's slot, because each placeholder is resolved from the
run that carries it rather than by assuming every placeholder is the next table.

**Editing is delete-and-recreate**, not in-place — see Limitations.


## Limitations

**Editing is destructive.** Shortcuts can only write to a note it can *find*, and there is
no way to find a specific note:

* `Find Notes` by **name** is a fuzzy ranked search, not a substring match.
* `Find Notes` by **tag** needs a *static* tag entity (`applenotes:tag/foo`) compiled into
  the shortcut; the dynamic slot will not coerce a string into one.
* and a note cannot be tagged programmatically anyway — AppleScript cannot set tags, and a
  `#hashtag` written into the body stays plain text.

That is circular, so `edit_note` instead reads the note, deletes it by ID (exact, via
AppleScript) and recreates it through the bridge. It can never touch the wrong note, but:

* the note gets a **new ID and a new creation date**;
* **attachments survive.** `read_note` emits a `file://` path to each attachment's file on
  disk, the replacement re-attaches them from those paths (Notes copies the file on attach),
  and — this is the load-bearing detail — the replacement is created *before* the original
  is deleted, so those files still exist at attach time. Keep an attachment's `![](file://…)`
  line in the markdown to preserve it, drop the line to remove it. The one refusal is an
  attachment that is **not downloaded locally** (evicted to iCloud): it reads back as a
  marker rather than a path, so recreating could not re-attach it, and the edit is blocked
  until you download it. Tables need none of this — they are rebuilt from the markdown;
* if the note's true structure cannot be read from NoteStore, the edit is **refused**
  rather than run from the degraded HTML, which would turn checkboxes into plain bullets;
* every edit writes a JSON backup to `~/.local/share/applenotes-mcp/backups/` first, and
  the original also lands in Notes' Recently Deleted for 30 days.

The note is re-filed in its original folder afterwards. That folder has to be read from
NoteStore too: AppleScript's `container of note id X` fails with error -1728 in current
Notes, however the note is addressed.

**Heading depth is flattened below level 3.** Apple's markdown converter maps `#` to Notes'
*Title* style and `##` to its *Heading* style, both of which round trip intact. `###` maps
to *Heading* as well — Notes has a *Subheading* style (`style_type` 2) but the converter
never emits it — so `### Foo` reads back as `## Foo`. Deeper levels collapse the same way.
The loss is one level and it is stable: reading `## Foo` back and rewriting it yields
`## Foo`, so it does not drift on repeated round trips. Pinned by
`tests/test_apple_conversions.py`.

**Links gain a trailing slash.** Notes normalises a bare-host URL, so
`https://example.com` comes back as `https://example.com/`. Harmless, and stable after the
first round trip, but it means one write/read cycle is not byte-identical.

**Folders must already exist.** `create_note` resolves the folder before writing anything —
the note is created first and moved second, so a bad name would otherwise strand it in the
default folder while returning an error.

**Folder names are not unique, so folders are addressed by ID.** Nesting allows both
`Personal/Recipes` and `Personal/Projects/Brewing/Recipes`, and AppleScript's
`move note ... to folder "Recipes"` silently picks one of them — a coin flip that files the
note somewhere the user may never find it. `list_folders` therefore reports full paths, an
ambiguous bare name is **refused** rather than guessed at, and the move addresses the folder
by its Core Data ID. That ID needs no extra lookup: every object in the library shares one
store UUID, so the note's own ID yields the folder's —
`x-coredata://<uuid>/ICNote/p123` → `x-coredata://<uuid>/ICFolder/p465`.
`edit_note` re-files by ID too, so a note living in one of two same-named folders goes back
to the one it came from.

The folder list is read from NoteStore rather than AppleScript, because three things have to
be filtered out to match what Notes.app actually shows:

* `ZFOLDERTYPE != 0` — **Recently Deleted** and **Quick Notes** are system folders, not
  filing destinations, but AppleScript lists them alongside real ones.
* `ZMARKEDFORDELETION` — folders pending deletion.
* `ZNEEDSINITIALFETCHFROMCLOUD` — **CloudKit stubs**: records the database knows exist but
  whose contents were never fetched. Notes.app does not display them and they hold no
  notes, but they persist indefinitely — ours are years older than the live records,
  presumably folders created and deleted on another device whose deletion never reconciled.
  They are the main source of *apparently* duplicate folder names, and leaving them in makes
  real folders look ambiguous when they are not.

## Traps in Apple's markdown converter

Both of these are silent — the note is quietly wrong, with no error — and both are worked
around in `bridge.py`, but they will bite anyone driving `Make Rich Text from Markdown`:

* **Table delimiter rows need three dashes.** `| - | - |` is left as literal text;
  `| --- | --- |` becomes a table. GFM allows a single dash, so hand-written markdown hits
  this constantly. The bridge widens short delimiter rows before sending.
* **A list immediately after a table, in the same converted chunk, is destroyed** — it
  comes out as literal text with a bullet glyph and tabs, which a subsequent round trip
  then reads as an indented code block, compounding the damage. The bridge sends each
  table as its own block so that whatever follows converts cleanly.

## Why not read notes through Shortcuts?

There is a tempting chain — `Find Notes` → `Get Text from Input` → `Make Markdown from
Rich Text` — which does emit `- [x]` / `- [ ]` with the right ticked state. It is a dead
end for two reasons:

* **It is not really reading checklists.** `Get Text from Input` flattens the note to
  *plain text*, and Notes' plain-text rendering happens to spell checklist items with
  brackets; the markdown converter then just escapes the special characters (you get
  `\- [x] item` and `1\. numbered`). Everything else is lost: the table collapses to loose
  lines, headings vanish, bold and italic vanish.
* **It cannot address a note safely.** It has to locate the note with `Find Notes`, whose
  only operator is a fuzzy ranked name search. Since `edit_note` rewrites a note from what
  the reader returned, a read that silently grabbed the *wrong* note would overwrite note A
  with note B's contents.

Note that AppleScript's `plaintext` property is *not* equivalent: it strips checklist
markers entirely.

If NoteStore cannot be read at all (no Full Disk Access), `read_note` falls back to the
HTML — which is blind to checklists — and `edit_note` **refuses to run**, rather than
silently turning your checkboxes into bullets.

## Checklists

`- [ ]` and `- [x]` produce real, tickable checkboxes, positioned correctly among prose,
and their ticked state survives a full read/edit round trip. Two undocumented things make
this work:

* Checklist items can only be *appended* to the end of a note, but the bridge builds the
  whole note by appending in order, so they still land in the right place.
* Ticking requires **Set Checklist Items Checked**, which Apple does not show in the
  Shortcuts action library at all — only *Append Checklist Item* is listed. It is flagged
  discoverable in Notes' intent metadata, and it resolves and runs perfectly well in a
  hand-built shortcut. It is the only way to write a ticked item, since Append Checklist
  Item has no `checked` parameter.

## Attaching files, learned the hard way

**Add File to Note** wants a *file*, and there is no headless way to hand it one by path.
Both routes that look like they should work do not, and both fail *silently* under
`shortcuts run` (no prompt can appear, so access is just denied):

* **Get File** (`documentpicker.open`) resolves its path relative to a file provider
  (iCloud Drive), so an absolute local path comes back as *“no such file.”* Granting
  Shortcuts Full Disk Access changes nothing — it is not a permission failure.
* **Get Contents of URL** on a `file://` URL fails with a CFNetwork error; its engine is a
  network downloader and will not fetch local files.

What *does* work is passing the file as the shortcut's **input**. The `shortcuts` CLI reads
it with your own permissions — outside the shortcut sandbox — and hands the bytes in, so
there is no path resolution, no scheme restriction, and no size cap.

* `shortcuts run` takes **repeated `-i`**, and a shortcut that *iterates* its input sees
  them all, so one run carries the JSON note-definition (item 1) plus every file (items
  2…N). The note is created and the files attached in the **same run**, which sidesteps the
  note-addressing problem entirely — no fuzzy `Find Notes`, no most-recent-note guess.
* The rejected alternative was embedding bytes in the JSON as a `data:` URI through Get
  Contents of URL. It works and is byte-perfect, but the data-URI string is capped around
  **350 KB** — above that the run *succeeds with no attachment*. Direct input has no such
  limit.

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

## Notes on the Shortcuts CLI, learned the hard way

* `shortcuts sign` requires its **input** file to be named `.shortcut`. A `.plist` is
  rejected with "isn't in the correct format".
* Every action UUID must be unique across your **whole** Shortcuts library, or signing
  fails with "Failed to modify some records".
* Signing is **flaky** — the same valid input intermittently fails with that same error, so
  it needs a retry loop with generous backoff.
* Signing does **not** validate action identifiers. A deliberately bogus one signs happily
  and only shows up as a broken action after import.
* Notes' `Metadata.appintents/extract.actionsdata` lists all 48 of its App Intents, and
  `~/Library/Shortcuts/Shortcuts.sqlite` holds the exact serialisation of any shortcut you
  build by hand — far more reliable than guessing identifiers.
