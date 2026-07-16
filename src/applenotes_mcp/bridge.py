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

import json
import plistlib
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

SHORTCUT_NAME = "Notes MCP Bridge"
BRIDGE_DIR = Path.home() / ".local" / "share" / "applenotes-mcp"
SIGN_ATTEMPTS = 12

CONDITION_IS = 4  # WFCondition: equals

CHECKLIST_LINE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$")

# A whole-line image (`![alt](ref)`) or link (`[name](ref)`). Only treated as a file
# attachment when `ref` points at a LOCAL file -- a file:// URL or an absolute path -- which
# is exactly what the reader emits for an attachment, so a read note round-trips. An http(s)
# link, or anything else, stays ordinary markdown.
FILE_IMAGE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)$")
FILE_LINK = re.compile(r"^\[([^\]]*)\]\(([^)]+)\)$")


def file_ref(line: str) -> tuple[str, str] | None:
    """(display name, local path) if the line is a whole-line ref to a local file.

    Public because both the writer (split_blocks, deciding what to attach) and the reader
    side (server._promote_title, deciding what to skip when deriving a title) need to
    recognise an attachment line.
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
    return label or Path(path).name, path

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
    u_name, u_n, u_file = _uid(), _uid(), _uid()
    u_checked, u_checked_text, u_item = _uid(), _uid(), _uid()
    repeat_group, cl_group, file_group, md_group, tick_group = (_uid() for _ in range(5))

    actions = [
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
                "UUID": _uid(),
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
            name, path = ref
            file_input_index += 1
            blocks.append(
                {
                    "type": "file",
                    "name": name,
                    "path": path,
                    "n": str(file_input_index),
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
