## Implementation details

Attachments survive edits because `read_note` emits a `file://` path to each attachment's file on
disk and the replacement re-attaches them from those paths. The replacement is created *before* the original
is deleted so those files still exist at attach time. 
If an attachment is not downloaded locally (i.e. evicted to iCloud) it reads back as a
marker rather than a path, so recreating could not re-attach it, and the edit is blocked
until you download it. Tables need none of this — they are just rebuilt from the HTML/markdown.

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
part is handed to its intent in order.

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


## "Features" of Apple's markdown converter

Both of these are silent (the note is just quietly wrong) and both are worked around in `bridge.py`.

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
* Notes' `Metadata.appintents/extract.actionsdata` lists all 48 of its App Intents, and `~/Library/Shortcuts/Shortcuts.sqlite` holds the exact serialisation of any shortcut you build by hand.
