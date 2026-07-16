"""The MCP contract: what the server advertises over the wire.

Spawns the server exactly as a client would, over stdio, and inspects `initialize` and
`tools/list`. This is what a model actually sees -- tool names, input schemas, the
annotations a client uses for permission prompting, and the server instructions -- so a
change that silently alters the contract is caught here.

Hermetic: listing tools introspects the decorated functions and touches neither Notes.app
nor NoteStore, so this is NOT a live test and runs by default.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

mcp_client = pytest.importorskip("mcp.client.stdio")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

# Run the server out of this interpreter, so it uses the same venv the tests do rather than
# whatever `applenotes-mcp` happens to be on PATH.
PARAMS = StdioServerParameters(
    command=sys.executable,
    args=["-c", "from applenotes_mcp.server import main; main()"],
)


async def _handshake():
    async with stdio_client(PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = (await session.list_tools()).tools
            return init, {t.name: t for t in tools}


@pytest.fixture(scope="module")
def contract():
    return asyncio.run(_handshake())


def test_exactly_the_expected_tools_are_exposed(contract) -> None:
    _, tools = contract
    assert set(tools) == {
        "create_note",
        "read_note",
        "edit_note",
        "search_notes",
        "search_note_text",
        "list_folders",
        "list_folder",
    }


def test_server_ships_instructions(contract) -> None:
    init, _ = contract
    assert init.instructions
    # The two cross-tool facts most likely to be misused if lost.
    assert "search_notes" in init.instructions
    assert "list_folders" in init.instructions


def test_required_arguments_are_declared(contract) -> None:
    _, tools = contract
    assert set(tools["create_note"].inputSchema["required"]) == {"title", "markdown"}
    assert tools["read_note"].inputSchema["required"] == ["note_id"]
    assert set(tools["edit_note"].inputSchema["required"]) == {"note_id", "markdown"}


def test_read_only_tools_are_annotated_read_only(contract) -> None:
    _, tools = contract
    for name in ("read_note", "search_notes", "search_note_text", "list_folders", "list_folder"):
        assert tools[name].annotations is not None, f"{name} lost its annotations"
        assert tools[name].annotations.readOnlyHint is True


def test_edit_note_is_flagged_destructive(contract) -> None:
    # This is the hint a client uses to prompt for edit_note even when the server is
    # otherwise allowlisted. Losing it would let a destructive rewrite run unprompted.
    _, tools = contract
    ann = tools["edit_note"].annotations
    assert ann is not None
    assert ann.destructiveHint is True
    assert ann.readOnlyHint is not True


def test_create_note_is_not_flagged_read_only(contract) -> None:
    _, tools = contract
    assert tools["create_note"].annotations.readOnlyHint is not True
