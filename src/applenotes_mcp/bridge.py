"""The Shortcuts bridge: the workflow that writes notes, and the code that drives it.

The note is built chunk by chunk. Create Note returns the note entity, and every
subsequent action appends to the end of that note, so ordering the appends
positions the content.

Markdown is parsed by NOTES ITSELF, via the `interpretAsMarkdown` parameter on the
Append to Note intent, rather than by the Shortcuts *Make Rich Text from Markdown*
action. It is the same parser as File > Import Markdown, and it is markedly better:
block quotes, fenced code blocks and horizontal rules all survive, headings land on
the right paragraph style, and single-dash table delimiters are accepted. What it
cannot do is a TICKED checklist item -- `- [x]` comes back as plain body text -- so
checklist items still go through the checklist intent, which is why blocks exist at all.

Two Notes intents were dropped in the macOS 27 era, NOT because they stopped working but
because Shortcuts will no longer IMPORT a workflow containing them:

    com.apple.Notes.SetChecklistItemCheckedLinkActionv2   (ticking an item)
    com.apple.Notes.SetAttachmentSizeLinkAction           (attachment display size)

Importing such a workflow fails with "This shortcut can't be imported because it contains
features not supported on this device", and the log says only "Refusing to import shortcut
with reasons: <private>". It is the actions themselves, not their parameters: probes with
the enum omitted, and with the enum serialised as a string token, are refused just the
same, while Create Note, Append to Note, Append Checklist Item and Add File to Note all
import fine. A copy imported under macOS 26 keeps running, which is why this only bites on
a fresh import.

The cost is that `- [x]` now writes an UNTICKED checkbox, and `![img|small]` attaches at
the default size. Both are visible losses, reported by `run()` to the caller.

Gotchas and rationale are detailed in NOTES.md.
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
BRIDGE_VERSION = 4
_VERSION_RE = re.compile(r"applenotes-mcp bridge v(\d+)")
SHORTCUTS_DB = Path.home() / "Library" / "Shortcuts" / "Shortcuts.sqlite"

CONDITION_IS = 4  # WFCondition: equals

CHECKLIST_LINE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$")

# A whole-line image ![alt](ref) or link [name](ref). Only treated as a file
# attachment when ref points at a LOCAL file - a file:// URL or an absolute path - which
# is exactly what the reader emits for an attachment, so a read note round-trips. An http(s)
# link, or anything else, stays ordinary markdown.
FILE_IMAGE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)$")
FILE_LINK = re.compile(r"^\[([^\]]*)\]\(([^)]+)\)$")


# Attachment display sizes settable via the Notes "Set Attachment Size" intent. "default"
# is the absence of a size (no `|size` suffix), so it never needs the intent.
ATTACHMENT_SIZES = {"small", "medium", "large"}


def file_ref(line: str) -> tuple[str, str, str | None] | None:
    """(display name, local path, size) if the line is a whole-line ref to a local file.

    The label may carry a trailing display size after a pipe:
        ![photo|small](...)
    This would be {small, medium, large}, size is None otherwise. Public because both the writer
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
    # The shortcut will fail to sign if UUIDs are not globally unique so we need a bunch
    u_input1, u_dict, u_title, u_blocks, \
    u_note, u_type, u_type_text, u_text, u_md_text, \
    u_name, u_n, u_file, \
    repeat_group, cl_group, file_group, md_group \
        = (_uid() for _ in range(16))

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
        # Three independent Ifs on the block type rather than nested if/else, 
        # exactly one matches per block.
        #
        # checklist item:
        _if(cl_group, _uid(), "checklist", _output(u_type_text, "Text")),
        {
            "WFWorkflowActionIdentifier": "com.apple.Notes.CreateChecklistItemLinkAction",
            "WFWorkflowActionParameters": {
                "UUID": _uid(),
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
        # prose: hand the markdown straight to Notes and let IT parse it.
        #
        # `interpretAsMarkdown` is a parameter on the Append to Note intent (it is in
        # Notes' intent metadata as "Interpret as Markdown"). It has no legacy Shortcuts
        # key, so it serialises under its own name alongside WFInput/WFNote.
        _if(md_group, _uid(), "markdown", _output(u_type_text, "Text")),
        # Through a Text action first, for the same reason the conditional above needs
        # one: a Dictionary Value is untyped. `getrichtextfrommarkdown` used to sit here
        # and yield a typed Rich Text output; handing the untyped value straight to the
        # intent instead makes Shortcuts reject the whole workflow at IMPORT time, with
        # "contains features not supported on this device" and no indication of which
        # action is at fault.
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
            "WFWorkflowActionParameters": {
                "UUID": u_md_text,
                "WFTextActionText": _output_as_string(u_text, "Dictionary Value"),
            },
        },
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.appendnote",
            "WFWorkflowActionParameters": {
                "UUID": _uid(),
                "AppIntentDescriptor": _notes_intent("AppendToNoteLinkAction"),
                "WFInput": _output_as_string(u_md_text, "Text"),
                "WFNote": _output(u_note, "Create Note"),
                "interpretAsMarkdown": True,
            },
        },
        _endif(md_group),
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
        # macOS 27's Shortcuts REFUSES to import a workflow that reads Shortcut Input
        # (is.workflow.actions.input, via _extension_input) while declaring no surface that
        # could supply it: the dialog says only "contains features not supported on this
        # device" and the log says "Refusing to import shortcut with reasons: <private>".
        # Naming surfaces that take input makes it importable again. macOS 26 accepted the
        # empty list, so an already-imported v2 kept working across the upgrade while a
        # fresh import of the very same workflow failed.
        "WFWorkflowTypes": ["NCWidget", "WatchKit"],
        "WFQuickActionSurfaces": [],
    }


def split_blocks(markdown: str) -> list[dict[str, str]]:
    """Split markdown into prose, checklist items, and file attachments, in order.

    A `- [ ]` / `- [x]` line becomes its own checklist block, a whole-line reference to a
    local file becomes a `file` block, and everything else accumulates into prose blocks.

    Only those two need splitting out. Prose goes to Notes' own markdown parser, which
    handles a table, and a list directly after a table, and a single-dash delimiter row,
    all correctly -- so tables no longer get a block to themselves and delimiter rows are
    no longer rewritten. Both of those were workarounds for the Shortcuts *Make Rich Text
    from Markdown* converter, which this no longer goes through.

    A file block carries `path` (the local file) and `n`: the 1-based position of that file
    among the shortcut's inputs. The shortcut is driven as `-i payload.json -i file1 ...`,
    so input item 1 is the JSON and the files follow -- hence the first file is item 2.
    `run()` reads `path` out to build that `-i` list and drops it before sending the JSON.
    """
    blocks: list[dict[str, str]] = []
    prose: list[str] = []
    file_input_index = 1  # item 1 is the JSON payload; files are numbered from 2

    def flush_prose() -> None:
        text = "\n".join(prose).strip()
        prose.clear()
        if text:
            blocks.append({"type": "markdown", "text": text})

    for line in markdown.splitlines():
        checklist = CHECKLIST_LINE.match(line)
        ref = None if checklist else file_ref(line)

        if checklist:
            flush_prose()
            state, text = checklist.groups()
            blocks.append(
                {
                    "type": "checklist",
                    "text": text,
                    # Kept for the caller's benefit (see `losses`), not the shortcut's:
                    # ticking needs SetChecklistItemChecked, which macOS 27 will not import.
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
                    # Likewise informational: SetAttachmentSize will not import either.
                    "size": size or "",
                    "text": "",
                }
            )
        else:
            prose.append(line)

    flush_prose()
    return blocks


def losses(blocks: list[dict[str, str]]) -> list[str]:
    """What this markdown asked for that macOS 27's Shortcuts can no longer write.

    Both causes are import-time refusals of a Notes intent, detailed in the module
    docstring. The note is still written; these are the parts of it that will be wrong,
    and the caller is expected to say so rather than let the note be quietly incorrect.
    """
    out: list[str] = []
    if any(b.get("checked") == "yes" for b in blocks):
        out.append(
            "ticked checklist items were written UNTICKED: ticking needs the Notes "
            "'Set Checklist Items Checked' action, which macOS 27's Shortcuts refuses to import"
        )
    if any(b.get("size") for b in blocks):
        out.append(
            "attachment display sizes were ignored (attached at default size): sizing needs "
            "the Notes 'Set Attachment Size' action, which macOS 27's Shortcuts refuses to import"
        )
    return out


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
