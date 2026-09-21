## Implementation details

Attachments survive edits because `read_note` emits a `file://` path to each attachment's file on
disk and the replacement re-attaches them from those paths. The replacement is created *before* the original
is deleted so those files still exist at attach time. 
If an attachment is not downloaded locally (i.e. evicted to iCloud) it reads back as a
marker rather than a path, so recreating could not re-attach it, and the edit is blocked
until you download it. Tables need none of this — they are just rebuilt from the HTML/markdown.

**Writing** goes through a generated Shortcut (`Notes MCP Bridge`), driven headlessly by
`shortcuts run`. The markdown is split into ordered blocks — prose, individual checklist
items, and file attachments — and the shortcut walks them, handing each to the
relevant Notes intent:

    Create Note (plain title)
    Repeat with each block:
        checklist  → Append Checklist Item
        file       → Add File to Note   (the file fetched by index from the inputs)
        otherwise  → Append to Note, with `interpretAsMarkdown` = true

The note is built piecewise because checklists and attachments can only be *appended*: each
part is handed to its intent in order.

**The markdown is parsed by Notes, not by Shortcuts.** `interpretAsMarkdown` is a parameter
on the *Append to Note* intent (it appears in Notes' intent metadata as "Interpret as
Markdown"). It has no legacy Shortcuts key, so it serialises under its own name alongside
`WFInput`/`WFNote`. It reaches the same parser as *File > Import Markdown*, which handles
block quotes, fenced code blocks, horizontal rules and single-dash table delimiters — none
of which the Shortcuts *Make Rich Text from Markdown* action managed.

One trap: the value handed to `WFInput` must be a *typed* one. Feeding it the untyped
Dictionary Value straight out of *Get Value for Key* makes Shortcuts reject the whole
workflow at IMPORT time, with the same opaque message as the two refused actions below.
Passing it through a Text action first fixes it, exactly as the conditional operator needs
(see the `Dictionary Value` comment in `build_workflow`).

**Attachments** are added with extra `shortcuts run` inputs. The server invokes
`shortcuts run "Notes MCP Bridge" -i payload.json -i file1 -i file2 ...`, so input item 1 is
the JSON and each file follows; a `file` block carries the input index of its file, and the
shortcut fetches it with *Get Item from List* and calls *Add File to Note*. 

**Reading** reads from Apple's on-disk store directly. Notes are stored as a 
protobuf (`notestore.py`). This is done because both of the more obvious read paths are
lossy in different ways. AppleScript can convert notes to HTML, but it cannot see 
checklists at all (a ticked box and a plain bullet are both just `<li>text</li>`) and 
it also merges a numbered list into a preceding bullet list. Meanwhile Shortcuts' 
*Make Markdown from Rich Text* destroys tables and flattens
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
  note but not downloaded from iCloud becomes a visible `[attachment not downloaded: ...]`
  marker rather than a broken link.

**Folder names are not unique, so folders are addressed by ID.** AppleScript's
`move note ... to folder "FolderName"` silently picks one of them so `list_folders` therefore reports full paths, an
ambiguous bare name is **refused** rather than guessed at, and the move addresses the folder
by its Core Data ID. That ID needs no extra lookup: every object in the library shares one
store UUID, so the note's own ID yields the folder's —
`x-coredata://<uuid>/ICNote/p123` → `x-coredata://<uuid>/ICFolder/p465`.
`edit_note` re-files by ID too, so a note living in one of two same-named folders goes back
to the one it came from.

`create_folder` walks a path segment by segment, finding the longest prefix that already
exists and creating only what is missing beneath it, each new folder made `at folder id` of
the one before. It is deliberately a separate tool rather than an auto-create inside
`create_note`: `create_note` still **refuses** an unknown folder, so a mistyped name files
nothing by surprise, and making a folder stays an explicit act. A prefix that is itself
ambiguous is refused, since there would be no way to say which of them to nest under.

The folder list is read from NoteStore rather than AppleScript, because three things have to
be filtered out to match what Notes.app actually shows:

* `ZFOLDERTYPE != 0` — **Recently Deleted** and **Quick Notes** are system folders, not
  filing destinations, but AppleScript lists them alongside real ones.
* `ZMARKEDFORDELETION` — folders pending deletion.
* `ZNEEDSINITIALFETCHFROMCLOUD` — **CloudKit stubs**: records the database knows exist but
  whose contents were never fetched. Notes.app does not display them and they hold no
  notes, but they persist _indefinitely_.
  They are a main source of apparently duplicate folder names, and leaving them in makes
  real folders look ambiguous when they are not.


## "Features" of the old Shortcuts markdown converter

**Neither of these applies any more.** Notes' own parser gets both right, verified against a
live note on macOS 27, and both workarounds have been deleted. They are recorded because
they are what the block-splitting machinery was shaped around, and they would return if the
writer ever went back to *Make Rich Text from Markdown*. Both were silent — the note was
just quietly wrong.

* **Table delimiter rows need three dashes.** `| - | - |` is left as literal text;
  `| --- | --- |` becomes a table. GFM allows a single dash, so hand-written markdown hits
  this constantly. The bridge widens short delimiter rows before sending.
* **A list immediately after a table, in the same converted chunk, is destroyed** — it
  comes out as literal text with a bullet glyph and tabs, which a subsequent round trip
  then reads as an indented code block, compounding the damage. The bridge sends each
  table as its own block so that whatever follows converts cleanly.

## Reading notes using Shortcuts

Shortcuts can read notes. Chaining the intents `Find Notes` → `Get Text from Input` → `Make Markdown from Rich Text` produces 
output. However:

* *Formatting fails.* `Get Text from Input` flattens the note to
  plain text so tables collapse to loose lines, headings vanish, etc.
* *It cannot address a note safely.* It has to locate the note with `Find Notes`, whose
  only operator is a fuzzy ranked name search. Since `edit_note` rewrites a note from what
  the reader returned, a read that grabbed the wrong note could destroy data.

If NoteStore cannot be read at all for some reason then `read_note` falls back to the HTML and `edit_note` refuses to run.

## Checklists

`- [ ]` and `- [x]` produce real, tickable checkboxes, positioned correctly among prose,
and their ticked state survives a full read/edit round trip. Two undocumented things make
this work:

* Checklist items can only be appended to the end of a note, but the bridge builds the
  whole note by appending in order, so they still land in the right place.
* Ticking requires *Set Checklist Items Checked*, which Apple does not show in the
  Shortcuts action library at all - only *Append Checklist Item* is listed. It is flagged
  discoverable in Notes' intent metadata, and it resolves and runs perfectly well in a
  hand-built shortcut. It is the only way to write a ticked item, since Append Checklist
  Item has no `checked` parameter.

**On macOS 27 this depends on which build is installed.** The action still works, but the
workflow containing it can no longer be imported through the Shortcuts app — see the
section below. The basic build therefore leaves it out and `- [x]` writes an unticked box,
reported by `bridge.losses()`; the full build keeps it and ticks correctly.

## Attaching Files

*Add File to Note* wants a *file*, and there is no headless way to hand it one by path.
Both routes that look like they should work do not, and both fail silently under
`shortcuts run`:

* *Get File* (`documentpicker.open`) resolves its path relative to a file provider
  (iCloud Drive), so an absolute local path comes back as "no such file". Granting
  Shortcuts Full Disk Access does not fix this.
* *Get Contents of URL* on a `file://` URL fails with a `CFNetwork` error; it will not fetch local files.
* Data URIs work, but are limited to around 350kB so this would only work for small images / files.

What does work is passing the file as the shortcut's input. The `shortcuts` CLI reads
it with user permissions (i.e. outside the shortcut sandbox) and hands the bytes in, so
there is no path resolution, no scheme restriction, and no size cap.

`shortcuts run` takes repeated `-i` arguments, and a shortcut that *iterates* its input sees
them all, so one run carries the JSON note-definition (item 1) plus every file (items 
2...N). The note is created and the files attached in the same run which sidesteps the 
note-addressing problem.

## Notes on the Shortcuts CLI

* `shortcuts sign` requires its input file to be named `.shortcut`. A `.plist` is rejected with "isn't in the correct format".
* Every action UUID must be unique across the entire Shortcuts library, or signing fails with "Failed to modify some records".
* Signing seems flaky. The same valid input intermittently fails with that same error, so it needs a retry loop with generous backoff. This requires further investigation.
* Signing does _not_ validate action identifiers. An incorrect identifier signs happily and only shows up as a broken action after import.
* Notes' `Metadata.appintents/extract.actionsdata` lists all of its App Intents — 48 on macOS 26, 51 on macOS 27 — and `~/Library/Shortcuts/Shortcuts.sqlite` holds the exact serialisation of any shortcut you build by hand. Reading that table back is the only reliable way to learn how Shortcuts wants a parameter serialised.
* Signing does not validate anything the IMPORT then rejects, so a workflow can sign cleanly and still refuse to install. There is no CLI import, so each candidate costs a manual click — build probes in batches.

## macOS 27 refuses to import two of the Notes actions

Shortcuts on macOS 27 will not import a workflow containing either of:

    com.apple.Notes.SetChecklistItemCheckedLinkActionv2   (ticking a checklist item)
    com.apple.Notes.SetAttachmentSizeLinkAction           (attachment display size)

The dialog says only "This shortcut can't be imported because it contains features not
supported on this device". The log gives up nothing more than `Refusing to import shortcut
with reasons: <private>` — and note that zsh has its own `log` builtin, so you need
`/usr/bin/log show --predicate 'process == "Shortcuts"' --info --debug`.

It is the actions themselves, not their parameters or the workflow envelope. Narrowed down
by signing minimal probes and importing them one at a time:

| probe | imports |
| --- | --- |
| Create Note + Append to Note (legacy identifiers only) | yes |
| + full control flow, Shortcut Input, dictionary, repeat | yes |
| + `CreateChecklistItemLinkAction` | yes |
| + `AddFileAttachmentLinkAction` | yes |
| + `SetChecklistItemCheckedLinkActionv2` | **no** |
| …with its enum parameter omitted | **no** |
| …with its enum as a string token | **no** |
| + `SetAttachmentSizeLinkAction` | **no** |
| …with its enum as a string token | **no** |

Ruled out along the way: signing (the certificate chain is intact, the leaf valid, and
freshly-signed probes import), `WFWorkflowClientVersion` and `WFWorkflowTypes`, the
`com.apple.Notes.*` prefix in general, and the bare enum-case string serialisation. An
unmodified pre-markdown build of the bridge fails identically, so this is not a regression
from the `interpretAsMarkdown` work.

A copy imported under macOS 26 keeps running indefinitely, which is why this only bites on a
fresh import — and why it went unnoticed until the shortcut needed regenerating. The
practical consequence is that the "regenerate and re-import" recovery path is dead for any
workflow containing those actions.

The **reader** is untouched: ticked state and `ZMERGEABLEPREFERREDVIEWSIZE` are still
decoded, so a note ticked or resized by hand in Notes.app reads back correctly.

### Getting them back: the full build

The actions are not broken. Only the importer objects, so stepping around the importer
restores them, and `tools/wfimport.m` does exactly that: it unwraps the signed package with
`WFShortcutPackageFile`, rebuilds it as a `WFWorkflowRecord`, and inserts it straight into
`~/Library/Shortcuts/Shortcuts.sqlite` via `WFDatabase createWorkflowWithOptions:`. That is
private WorkflowKit SPI, and the approach follows
[pdfux/generate-shortcut-action-os-27](https://github.com/pdfux/generate-shortcut-action-os-27),
which hit the identical refusal for an unrelated action.

Verified end to end on macOS 27: a workflow the Shortcuts app refused three times imported
this way with every action intact, ran headlessly, and set an attachment's display size.
The whole live suite passes against the full build, ticking and all three sizes included.

So `build_workflow(full=...)` produces two builds. The basic one installs with a click and
is the default; the full one needs `wfimport`. Which is installed is stamped into the
leading Comment action ("v5 basic" / "v5 full"), so `losses()` knows whether to warn, and
an update never silently downgrades a full install to a basic one.

### Enum parameters must be BARE strings

An App Intent enum parameter (`changeOperation`, `attachmentSize`) is serialised as a plain
Python string — `"check"`, `"small"` — NOT as a `WFTextTokenString`. Wrapping one in a
string token leaves the parameter unresolved, and Shortcuts then stops mid-run to ask the
user which case they meant. Under `shortcuts run` that dialog is invisible: the run simply
never returns, which reads exactly like a hang, and whatever the user eventually picks is
silently applied to every branch. This cost an afternoon — `small` happened to be chosen,
so `small` appeared to round-trip correctly while `medium` and `large` both came back as
`small`.
