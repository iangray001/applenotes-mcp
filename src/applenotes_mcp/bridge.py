"""The Shortcuts bridge: the workflow that writes notes, and the code that drives it.

The note is built PIECEWISE. Create Note gives us the note entity, and every
subsequent action appends to the end of that note, so ordering the appends is what
positions the content:

    Create Note (title)
    Repeat with each block:
        If block.type is "checklist"  -> Append Checklist Item
        Otherwise                     -> Make Rich Text from Markdown -> Append to Note
        If block.checked is "yes"     -> Set Checklist Items Checked

Checklist items are always added to the END of the note by the intent -- but since
we build the whole note by appending in order, they still land in the right place.

"Set Checklist Items Checked" does NOT appear in the Shortcuts action library, though
Notes' intent metadata flags it discoverable. It nonetheless resolves and runs in a
hand-built shortcut, and it is the only way to write a TICKED item: Append Checklist
Item has no `checked` parameter.

Facts about `shortcuts sign`
  * its input file must be named `.shortcut`; a `.plist` is rejected outright
  * every action UUID must be unique across the user's whole Shortcuts library
  * it intermittently fails with "Failed to modify some records" on valid input, so
    signing must be retried with generous backoff
  * it does NOT validate action identifiers -- a bogus one signs happily and only
    shows up as a broken action after import
"""

from __future__ import annotations

import io
import json
import plistlib
import re
import sqlite3
import subprocess
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlparse

SHORTCUT_NAME = "Notes MCP Bridge"
BRIDGE_DIR = Path.home() / ".local" / "share" / "applenotes-mcp"
SIGN_ATTEMPTS = 12

# Bump whenever build_workflow() changes in a way that an already-imported shortcut would
# not reflect. The number is written into a Comment action in the workflow (see
# build_workflow) and read back from the installed shortcut (see installed_version), so a
# stale import can be detected and the user asked to re-import.
BRIDGE_VERSION = 2
_VERSION_RE = re.compile(r"applenotes-mcp bridge v(\d+)")
SHORTCUTS_DB = Path.home() / "Library" / "Shortcuts" / "Shortcuts.sqlite"

CONDITION_IS = 4  # WFCondition: equals

CHECKLIST_LINE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$")

# A whole-line image (`![alt](ref)`) or link (`[name](ref)`). Only treated as a file
# attachment when `ref` points at a LOCAL file -- a file:// URL or an absolute path -- which
# is exactly what the reader emits for an attachment, so a read note round-trips. An http(s)
# link, or anything else, stays ordinary markdown.
FILE_IMAGE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)$")
FILE_LINK = re.compile(r"^\[([^\]]*)\]\(([^)]+)\)$")


# Attachment display sizes settable via the Notes "Set Attachment Size" intent. "default"
# is the absence of a size (no `|size` suffix), so it never needs the intent.
ATTACHMENT_SIZES = {"small", "medium", "large"}


def file_ref(line: str) -> tuple[str, str, str | None] | None:
    """(display name, local path, size) if the line is a whole-line ref to a local file.

    The label may carry a trailing display size after a pipe -- `![photo|small](...)` -- one
    of small / medium / large; `size` is None otherwise. Public because both the writer
    (split_blocks) and the reader side (server._promote_title) need to recognise an
    attachment line.
    """
    match = FILE_IMAGE.match(line.strip()) or FILE_LINK.match(line.strip())
    if not match:
        return None
    label, ref = match.group(1), match.group(2).strip()
    if ref.startswith(("http://", "https://")):
        return None
    if ref.startswith("file://"):
        path = unquote(urlparse(ref).path)
    elif ref.startswith("/"):
        path = ref
    else:
        return None  # relative or scheme-less: not addressable as a file to attach

    size = None
    if "|" in label:
        head, tail = label.rsplit("|", 1)
        if tail.strip().lower() in ATTACHMENT_SIZES:
            label, size = head, tail.strip().lower()
    return label or Path(path).name, path, size

# A table delimiter row, e.g. "| --- | :-: |". Apple's markdown parser needs at least
# THREE dashes per cell: "| - | - |" is silently left as literal text rather than being
# turned into a table, with no error. GFM permits a single dash, so hand-written markdown
# hits this regularly.
TABLE_DELIMITER = re.compile(r"^\s*\|(?:\s*:?-+:?\s*\|)+\s*$")


def _widen_table_delimiters(markdown: str) -> str:
    def widen(line: str) -> str:
        if not TABLE_DELIMITER.match(line):
            return line
        cells = line.strip().strip("|").split("|")
        fixed = []
        for cell in cells:
            cell = cell.strip()
            left, right = cell.startswith(":"), cell.endswith(":")
            fixed.append(("" if not left else ":") + "---" + ("" if not right else ":"))
        return "| " + " | ".join(fixed) + " |"

    return "\n".join(widen(line) for line in markdown.splitlines())


class BridgeError(RuntimeError):
    pass


def _uid() -> str:
    return str(uuid.uuid4())


def _output(u: str, name: str) -> dict:
    """A previous action's output, in an attachment-typed slot."""
    return {
        "Value": {"OutputName": name, "OutputUUID": u, "Type": "ActionOutput"},
        "WFSerializationType": "WFTextTokenAttachment",
    }


def _output_as_string(u: str, name: str) -> dict:
    """A previous action's output, in a string-typed slot."""
    return {
        "Value": {
            "attachmentsByRange": {
                "{0, 1}": {"OutputName": name, "OutputUUID": u, "Type": "ActionOutput"}
            },
            "string": "￼",
        },
        "WFSerializationType": "WFTextTokenString",
    }


def _repeat_item() -> dict:
    return {
        "Value": {"Type": "Variable", "VariableName": "Repeat Item"},
        "WFSerializationType": "WFTextTokenAttachment",
    }


def _notes_intent(identifier: str, bundle: str = "com.apple.Notes") -> dict:
    return {
        "AppIntentIdentifier": identifier,
        "BundleIdentifier": bundle,
        "Name": "Notes",
        "TeamIdentifier": "0000000000",
    }


def _dict_value(source: dict, u: str, key: str) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.getvalueforkey",
        "WFWorkflowActionParameters": {
            "UUID": u,
            "WFInput": source,
            "WFDictionaryKey": key,
            "WFGetDictionaryValueType": "Value",
        },
    }


def _extension_input() -> dict:
    """The shortcut's whole input list: item 1 is the JSON payload, items 2..N the files."""
    return {"Value": {"Type": "ExtensionInput"}, "WFSerializationType": "WFTextTokenAttachment"}


def _list_item(source: dict, u: str, specifier: str, index: dict | None = None) -> dict:
    params = {"UUID": u, "WFInput": source, "WFItemSpecifier": specifier}
    if index is not None:
        params["WFItemIndex"] = index
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.getitemfromlist",
        "WFWorkflowActionParameters": params,
    }


def _if(group: str, u: str, value: str, compared: dict) -> dict:
    """Open an `If <compared> is <value>` block."""
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
        "WFWorkflowActionParameters": {
            "UUID": u,
            "GroupingIdentifier": group,
            "WFControlFlowMode": 0,
            "WFCondition": CONDITION_IS,
            "WFConditionalActionString": value,
            "WFInput": {"Type": "Variable", "Variable": compared},
        },
    }


def _endif(group: str) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
        "WFWorkflowActionParameters": {"UUID": _uid(), "GroupingIdentifier": group,
                                       "WFControlFlowMode": 2},
    }


def build_workflow() -> dict:
    u_input1, u_dict, u_title, u_blocks = _uid(), _uid(), _uid(), _uid()
    u_note, u_type, u_type_text, u_text, u_rich = _uid(), _uid(), _uid(), _uid(), _uid()
    u_name, u_n, u_file, u_add = _uid(), _uid(), _uid(), _uid()
    u_size, u_size_text = _uid(), _uid()
    u_checked, u_checked_text, u_item = _uid(), _uid(), _uid()
    repeat_group, cl_group, file_group, md_group, tick_group = (_uid() for _ in range(5))
    size_groups = {s: _uid() for s in ("small", "medium", "large")}

    actions = [
        # A leading Comment stamping the version. It does nothing when the shortcut runs;
        # its only job is to be read back from the installed shortcut so the code can tell
        # whether the imported copy is the one it expects (see installed_version).
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.comment",
            "WFWorkflowActionParameters": {
                "UUID": _uid(),
                "WFCommentActionText": f"applenotes-mcp bridge v{BRIDGE_VERSION} "
                "(generated -- do not edit)",
            },
        },
        # The input is a LIST: item 1 is the JSON payload, items 2..N are attachment files.
        # Pull item 1 and parse it; the files are fetched by index inside the loop.
        _list_item(_extension_input(), u_input1, "First Item"),
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.detect.dictionary",
            "WFWorkflowActionParameters": {
                "UUID": u_dict,
                "WFInput": _output(u_input1, "Item from List"),
            },
        },
        _dict_value(_output(u_dict, "Dictionary"), u_title, "title"),
        _dict_value(_output(u_dict, "Dictionary"), u_blocks, "blocks"),
        # Create the note with a plain-text title; it cannot carry an attributed string.
        {
            "WFWorkflowActionIdentifier": "com.apple.mobilenotes.SharingExtension",
            "WFWorkflowActionParameters": {
                "UUID": u_note,
                "AppIntentDescriptor": _notes_intent("CreateNoteLinkAction"),
                "ShowWhenRun": False,
                "WFCreateNoteInput": _output_as_string(u_title, "Dictionary Value"),
            },
        },
        # Repeat with each block ...
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.repeat.each",
            "WFWorkflowActionParameters": {
                "GroupingIdentifier": repeat_group,
                "WFControlFlowMode": 0,
                "WFInput": _output(u_blocks, "Dictionary Value"),
            },
        },
        _dict_value(_repeat_item(), u_type, "type"),
        _dict_value(_repeat_item(), u_text, "text"),
        _dict_value(_repeat_item(), u_name, "name"),
        _dict_value(_repeat_item(), u_n, "n"),
        _dict_value(_repeat_item(), u_size, "size"),
        _dict_value(_repeat_item(), u_checked, "checked"),
        # A Dictionary Value is an untyped value, and the "is" comparison is not valid
        # against one -- Shortcuts shows the operator in red and the run fails with
        # "Please choose a value for each parameter". Passing it through a Text action
        # first gives the conditional a genuine string to compare.
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
            "WFWorkflowActionParameters": {
                "UUID": u_type_text,
                "WFTextActionText": _output_as_string(u_type, "Dictionary Value"),
            },
        },
        # Three independent Ifs on the block type, rather than nested if/else -- flat
        # conditionals serialise more reliably, and exactly one matches per block.
        #
        # checklist item:
        _if(cl_group, _uid(), "checklist", _output(u_type_text, "Text")),
        {
            "WFWorkflowActionIdentifier": "com.apple.Notes.CreateChecklistItemLinkAction",
            "WFWorkflowActionParameters": {
                "UUID": u_item,
                "AppIntentDescriptor": _notes_intent("CreateChecklistItemLinkAction"),
                "name": _output_as_string(u_text, "Dictionary Value"),
                "noteEntity": _output(u_note, "Create Note"),
            },
        },
        _endif(cl_group),
        # file attachment: fetch the input file this block's `n` names, and add it. The
        # file rides in as an `-i` input, so its bytes never touch the JSON -- no size cap.
        _if(file_group, _uid(), "file", _output(u_type_text, "Text")),
        _list_item(_extension_input(), u_file, "Item At Index", index=_output(u_n, "Dictionary Value")),
        {
            "WFWorkflowActionIdentifier": "com.apple.Notes.AddFileAttachmentLinkAction",
            "WFWorkflowActionParameters": {
                "UUID": u_add,
                "AppIntentDescriptor": _notes_intent("AddFileAttachmentLinkAction"),
                "file": _output(u_file, "Item from List"),
                "name": _output_as_string(u_name, "Dictionary Value"),
                "note": _output(u_note, "Create Note"),
            },
        },
        _endif(file_group),
        # prose: markdown -> rich text -> append.
        _if(md_group, _uid(), "markdown", _output(u_type_text, "Text")),
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.getrichtextfrommarkdown",
            "WFWorkflowActionParameters": {
                "UUID": u_rich,
                "WFInput": _output(u_text, "Dictionary Value"),
            },
        },
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.appendnote",
            "WFWorkflowActionParameters": {
                "UUID": _uid(),
                "AppIntentDescriptor": _notes_intent("AppendToNoteLinkAction"),
                "WFInput": _output_as_string(u_rich, "Rich Text from Markdown"),
                "WFNote": _output(u_note, "Create Note"),
            },
        },
        _endif(md_group),
        # Tick the item we just appended, if the block was `- [x]`. A SECOND, sequential If:
        # `checked` is only ever "yes" on a checklist block, so the item reference is fresh.
        #
        # "Set Checklist Items Checked" is NOT in the Shortcuts action library -- Apple
        # hides it -- but it is flagged discoverable in Notes' intent metadata and does
        # resolve and run in a hand-built shortcut. It is the only way to write a ticked
        # item: CreateChecklistItem has no `checked` parameter.
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
            "WFWorkflowActionParameters": {
                "UUID": u_checked_text,
                "WFTextActionText": _output_as_string(u_checked, "Dictionary Value"),
            },
        },
        _if(tick_group, _uid(), "yes", _output(u_checked_text, "Text")),
        {
            "WFWorkflowActionIdentifier": "com.apple.Notes.SetChecklistItemCheckedLinkActionv2",
            "WFWorkflowActionParameters": {
                "UUID": _uid(),
                "AppIntentDescriptor": _notes_intent("SetChecklistItemCheckedLinkActionv2"),
                "changeOperation": "check",
                "entities": _output(u_item, "Append Checklist Item"),
                "note": _output(u_note, "Create Note"),
            },
        },
        _endif(tick_group),
        # Set the attachment's display size, if the file block asked for one. Independent
        # Ifs, like the tick above: `size` is only ever small/medium/large on a file block,
        # and AddFileAttachment (u_add) ran earlier in this same iteration, so its output is
        # the attachment we just added. One If per size because the enum parameter takes a
        # literal case string ("small"), the same way SetChecklistItemChecked takes "check".
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
            "WFWorkflowActionParameters": {
                "UUID": u_size_text,
                "WFTextActionText": _output_as_string(u_size, "Dictionary Value"),
            },
        },
        *[
            action
            for size, group in size_groups.items()
            for action in (
                _if(group, _uid(), size, _output(u_size_text, "Text")),
                {
                    "WFWorkflowActionIdentifier": "com.apple.Notes.SetAttachmentSizeLinkAction",
                    "WFWorkflowActionParameters": {
                        "UUID": _uid(),
                        "AppIntentDescriptor": _notes_intent("SetAttachmentSizeLinkAction"),
                        "target": _output(u_add, "Add File to Note"),
                        "attachmentSize": size,
                    },
                },
                _endif(group),
            )
        ],
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.repeat.each",
            "WFWorkflowActionParameters": {
                "UUID": _uid(),
                "GroupingIdentifier": repeat_group,
                "WFControlFlowMode": 2,
            },
        },
    ]

    return {
        "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": "3100.2",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {
            "WFWorkflowIconStartColor": 4282601983,
            "WFWorkflowIconGlyphNumber": 59511,
        },
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowTypes": [],
    }


def split_blocks(markdown: str) -> list[dict[str, str]]:
    """Split markdown into prose, checklist items, and file attachments, in order.

    A `- [ ]` / `- [x]` line becomes its own checklist block, carrying its ticked state;
    a whole-line reference to a local file becomes a `file` block; everything else
    accumulates into prose blocks. Every block carries a `checked` key ("yes"/"no")
    because the shortcut reads it unconditionally.

    A file block carries `path` (the local file) and `n`: the 1-based position of that file
    among the shortcut's inputs. The shortcut is driven as `-i payload.json -i file1 ...`,
    so input item 1 is the JSON and the files follow -- hence the first file is item 2.
    `run()` reads `path` out to build that `-i` list and drops it before sending the JSON.
    """
    markdown = _widen_table_delimiters(markdown)
    blocks: list[dict[str, str]] = []
    prose: list[str] = []
    table: list[str] = []
    file_input_index = 1  # item 1 is the JSON payload; files are numbered from 2

    def flush_prose() -> None:
        text = "\n".join(prose).strip()
        prose.clear()
        if text:
            blocks.append({"type": "markdown", "text": text, "checked": "no"})

    def flush_table() -> None:
        # A table gets a block to itself. Apple's markdown converter mangles anything
        # that FOLLOWS a table in the same chunk -- a bullet list after one comes out as
        # literal text with a bullet glyph and tabs, which a later round trip then reads
        # as an indented code block. Converting the table on its own avoids that.
        text = "\n".join(table).strip()
        table.clear()
        if text:
            blocks.append({"type": "markdown", "text": text, "checked": "no"})

    for line in markdown.splitlines():
        checklist = CHECKLIST_LINE.match(line)
        ref = None if checklist else file_ref(line)
        is_table_row = line.lstrip().startswith("|")

        if is_table_row:
            if not table:
                flush_prose()
            table.append(line)
            continue

        if table:
            flush_table()

        if checklist:
            flush_prose()
            state, text = checklist.groups()
            blocks.append(
                {
                    "type": "checklist",
                    "text": text,
                    "checked": "yes" if state.lower() == "x" else "no",
                }
            )
        elif ref:
            flush_prose()
            name, path, size = ref
            file_input_index += 1
            blocks.append(
                {
                    "type": "file",
                    "name": name,
                    "path": path,
                    "n": str(file_input_index),
                    "size": size or "",  # "" = default; the shortcut sets a size only if set
                    "text": "",
                    "checked": "no",
                }
            )
        else:
            prose.append(line)

    flush_table()
    flush_prose()
    return blocks


def generate_signed_shortcut() -> Path:
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    unsigned = BRIDGE_DIR / "bridge-unsigned.shortcut"
    signed = BRIDGE_DIR / f"{SHORTCUT_NAME}.shortcut"

    with open(unsigned, "wb") as fh:
        plistlib.dump(build_workflow(), fh)

    last = ""
    for attempt in range(1, SIGN_ATTEMPTS + 1):
        result = subprocess.run(
            ["shortcuts", "sign", "-m", "anyone", "-i", str(unsigned), "-o", str(signed)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            unsigned.unlink(missing_ok=True)
            return signed
        last = result.stderr.strip()
        time.sleep(1.5 * attempt)

    raise BridgeError(f"could not sign the bridge shortcut after {SIGN_ATTEMPTS} tries: {last}")


def is_installed() -> bool:
    result = subprocess.run(["shortcuts", "list"], capture_output=True, text=True)
    return SHORTCUT_NAME in result.stdout.splitlines()


def installed_version() -> int | None:
    """The version stamped in the installed bridge's leading Comment.

    Returns the version integer; 0 if the shortcut is installed and readable but carries no
    version marker (an old, pre-versioning, or hand-made build); or None if it cannot be
    determined at all -- the Shortcuts database is missing or unreadable, or its schema has
    changed. `run` treats None as "cannot tell" and does NOT block on it, so a schema change
    in a future macOS degrades to the old behaviour rather than refusing every write.
    """
    if not SHORTCUTS_DB.exists():
        return None
    try:
        uri = f"file:{SHORTCUTS_DB.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute(
                """
                SELECT a.ZDATA FROM ZSHORTCUTACTIONS a
                JOIN ZSHORTCUT s ON s.Z_PK = a.ZSHORTCUT
                WHERE s.ZNAME = ?
                """,
                (SHORTCUT_NAME,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        actions = plistlib.load(io.BytesIO(bytes(row[0])))
    except Exception:
        return None
    return _version_from_actions(actions)


def _version_from_actions(actions: list[dict]) -> int:
    """The version stamped in a workflow's Comment action, or 0 if there is no marker."""
    for action in actions:
        if action.get("WFWorkflowActionIdentifier") == "is.workflow.actions.comment":
            text = action.get("WFWorkflowActionParameters", {}).get("WFCommentActionText", "")
            match = _VERSION_RE.search(text)
            if match:
                return int(match.group(1))
    return 0


def _attachment_paths(blocks: list[dict[str, str]]) -> list[Path]:
    """The local files a set of blocks references, in `n` order (input item 2, 3, ...).

    Validates each exists and is a regular file BEFORE the shortcut runs, so a bad path is
    a clean error rather than a note that is created and then only partly populated. The
    `n` on each block is the shortcut-input index, so sorting by it fixes the `-i` order.
    """
    files = sorted((b for b in blocks if b["type"] == "file"), key=lambda b: int(b["n"]))
    paths: list[Path] = []
    for block in files:
        path = Path(block["path"]).expanduser()
        if not path.is_file():
            raise BridgeError(f"attachment not found: {block['path']!r}")
        paths.append(path)
    return paths


def run(title: str, markdown: str, timeout: int = 120) -> None:
    """Drive the bridge shortcut. Runs headlessly; takes roughly 8s plus per-block time.

    Attachments in the markdown (whole-line `![](file)` / `[](file)` refs to local files)
    are passed as additional `-i` inputs, one per file, so the shortcut receives the JSON
    as input item 1 and each file as the item its block's `n` names.
    """
    if not is_installed():
        path = generate_signed_shortcut()
        raise BridgeError(
            f"the '{SHORTCUT_NAME}' shortcut is not installed. A signed copy has been "
            f"written to {path} -- open it and click 'Add Shortcut', then retry. "
            "Shortcuts cannot be installed without this one-time confirmation."
        )

    # The installed shortcut may be an older build than this code expects (the user updated
    # the server but did not re-import). A None means the version could not be read, which we
    # do NOT block on -- only a definite mismatch.
    version = installed_version()
    if version is not None and version != BRIDGE_VERSION:
        path = generate_signed_shortcut()
        raise BridgeError(
            f"the installed '{SHORTCUT_NAME}' shortcut is out of date (v{version}, this "
            f"server needs v{BRIDGE_VERSION}). An updated signed copy is at {path} -- delete "
            "the old shortcut in the Shortcuts app, open this one, click 'Add Shortcut', then "
            "retry."
        )

    blocks = split_blocks(markdown)
    attachments = _attachment_paths(blocks)
    # `path` is a local filesystem path; it travels as an `-i` input, not in the JSON, so
    # drop it from the payload (it would otherwise leak the local path into the note input).
    payload = {"title": title, "blocks": [{k: v for k, v in b.items() if k != "path"} for b in blocks]}

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        path = Path(fh.name)

    inputs: list[str] = ["-i", str(path)]
    for attachment in attachments:
        inputs += ["-i", str(attachment)]

    try:
        result = subprocess.run(
            ["shortcuts", "run", SHORTCUT_NAME, *inputs],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise BridgeError(f"shortcut failed: {result.stderr.strip() or 'no error output'}")
    finally:
        path.unlink(missing_ok=True)
