"""The Shortcuts bridge: the workflow that writes notes, and the code that drives it.

Why this exists
---------------
Every Apple Notes MCP server drives Notes via AppleScript's `body` property. That
path runs the HTML through Notes' importer, which discards the incoming styling and
bakes explicit inline sizes onto everything (11px body, 15px headings). The result
overrides the user's own text-size preference and cannot be fixed from Notes' UI,
which exposes named styles rather than point sizes. Headings arrive as bold spans,
and tables and checklists are unreachable entirely.

Shortcuts offers a way out. Its `getrichtextfrommarkdown` action produces a genuine
attributed string, and Notes' `Append to Note` App Intent ingests it natively -- no
HTML importer involved. Notes written this way carry real <h1>/<h2>, real <ul>/<ol>
and real table objects, with no font-size pollution.

Structure
---------
The note is built PIECEWISE. Create Note gives us the note entity, and every
subsequent action appends to the end of that note, so ordering the appends is what
positions the content:

    Create Note (title)
    Repeat with each block:
        If block.type is "checklist"  -> Append Checklist Item
        Otherwise                     -> Make Rich Text from Markdown -> Append to Note
        If block.checked is "yes"     -> Set Checklist Items Checked

Appending to the note directly (rather than accumulating into a variable) also
avoids a trap: appending rich text to a Shortcuts *variable* coerces it to plain
text unless the variable was first seeded with an empty rich-text value.

Checklist items are always added to the END of the note by the intent -- but since
we build the whole note by appending in order, they still land in the right place.

"Set Checklist Items Checked" does NOT appear in the Shortcuts action library, though
Notes' intent metadata flags it discoverable. It nonetheless resolves and runs in a
hand-built shortcut, and it is the only way to write a TICKED item: Append Checklist
Item has no `checked` parameter.

Facts about `shortcuts sign`, learned the hard way:
  * its *input* file must be named `.shortcut`; a `.plist` is rejected outright
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

SHORTCUT_NAME = "Notes MCP Bridge"
BRIDGE_DIR = Path.home() / ".local" / "share" / "applenotes-mcp"
SIGN_ATTEMPTS = 12

CONDITION_IS = 4  # WFCondition: equals

CHECKLIST_LINE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$")

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


def build_workflow() -> dict:
    u_dict, u_title, u_blocks = _uid(), _uid(), _uid()
    u_note, u_type, u_type_text, u_text, u_rich = _uid(), _uid(), _uid(), _uid(), _uid()
    u_checked, u_checked_text, u_item = _uid(), _uid(), _uid()
    repeat_group, if_group, tick_group = _uid(), _uid(), _uid()

    actions = [
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.detect.dictionary",
            "WFWorkflowActionParameters": {
                "UUID": u_dict,
                "WFInput": {
                    "Value": {"Type": "ExtensionInput"},
                    "WFSerializationType": "WFTextTokenAttachment",
                },
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
        # If the block is a checklist item ...
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
            "WFWorkflowActionParameters": {
                "GroupingIdentifier": if_group,
                "WFControlFlowMode": 0,
                "WFCondition": CONDITION_IS,
                "WFConditionalActionString": "checklist",
                "WFInput": {
                    "Type": "Variable",
                    "Variable": _output(u_type_text, "Text"),
                },
            },
        },
        {
            "WFWorkflowActionIdentifier": "com.apple.Notes.CreateChecklistItemLinkAction",
            "WFWorkflowActionParameters": {
                "UUID": u_item,
                "AppIntentDescriptor": _notes_intent("CreateChecklistItemLinkAction"),
                "name": _output_as_string(u_text, "Dictionary Value"),
                "noteEntity": _output(u_note, "Create Note"),
            },
        },
        # ... otherwise it is prose: markdown -> rich text -> append.
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
            "WFWorkflowActionParameters": {
                "GroupingIdentifier": if_group,
                "WFControlFlowMode": 1,
            },
        },
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
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
            "WFWorkflowActionParameters": {
                "UUID": _uid(),
                "GroupingIdentifier": if_group,
                "WFControlFlowMode": 2,
            },
        },
        # Tick the item we just appended, if the block was `- [x]`. This is a SECOND,
        # sequential If rather than one nested inside the branch above: nesting is more
        # fragile to serialise, and `checked` is only ever "yes" on a checklist block, so
        # the item reference can never be stale here.
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
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
            "WFWorkflowActionParameters": {
                "GroupingIdentifier": tick_group,
                "WFControlFlowMode": 0,
                "WFCondition": CONDITION_IS,
                "WFConditionalActionString": "yes",
                "WFInput": {
                    "Type": "Variable",
                    "Variable": _output(u_checked_text, "Text"),
                },
            },
        },
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
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
            "WFWorkflowActionParameters": {
                "UUID": _uid(),
                "GroupingIdentifier": tick_group,
                "WFControlFlowMode": 2,
            },
        },
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
    """Split markdown into prose blocks and individual checklist items, in order.

    A `- [ ]` / `- [x]` line becomes its own checklist block, carrying its ticked state;
    everything else accumulates into prose blocks. Every block carries a `checked` key
    ("yes"/"no") because the shortcut reads it unconditionally.
    """
    markdown = _widen_table_delimiters(markdown)
    blocks: list[dict[str, str]] = []
    prose: list[str] = []
    table: list[str] = []

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


def run(title: str, markdown: str, timeout: int = 120) -> None:
    """Drive the bridge shortcut. Runs headlessly; takes roughly 8s plus per-block time."""
    if not is_installed():
        path = generate_signed_shortcut()
        raise BridgeError(
            f"the '{SHORTCUT_NAME}' shortcut is not installed. A signed copy has been "
            f"written to {path} -- open it and click 'Add Shortcut', then retry. "
            "Shortcuts cannot be installed without this one-time confirmation."
        )

    payload = {"title": title, "blocks": split_blocks(markdown)}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        path = Path(fh.name)

    try:
        result = subprocess.run(
            ["shortcuts", "run", SHORTCUT_NAME, "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise BridgeError(f"shortcut failed: {result.stderr.strip() or 'no error output'}")
    finally:
        path.unlink(missing_ok=True)
