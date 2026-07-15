"""Tier 1: the HTML scraper.

Two jobs, and they carry very different weight:

  * extract_tables() is on the PRIMARY read path -- the protobuf holds a table only as a
    placeholder, so every table in every note comes through here.
  * html_to_markdown() is the degraded fallback for when NoteStore cannot be read. It
    cannot see checklists at all. edit_note refuses to run from it; read_note will use it.
"""

from __future__ import annotations

from applenotes_mcp.html_to_markdown import extract_tables, html_to_markdown

# Notes' real HTML shape: headings arrive wrapped in <div><b>...</b></div>, and tables are
# wrapped in an <object>.
TABLE_HTML = (
    "<div>intro</div>"
    "<object><table>"
    "<tr><td><b>Header A</b></td><td><b>Header B</b></td></tr>"
    "<tr><td>a1</td><td>b1</td></tr>"
    "</table></object>"
)


def test_extracts_a_table_as_markdown() -> None:
    assert extract_tables(TABLE_HTML) == ["| Header A | Header B |\n| --- | --- |\n| a1 | b1 |"]


def test_header_cells_are_not_left_bolded() -> None:
    # Notes bolds header cells itself. Keeping the ** would re-bold them on every rewrite,
    # accumulating asterisks round trip after round trip.
    assert "**" not in extract_tables(TABLE_HTML)[0]


def test_extracts_several_tables_in_document_order() -> None:
    html = (
        "<object><table><tr><td>first</td></tr></table></object>"
        "<div>between</div>"
        "<object><table><tr><td>second</td></tr></table></object>"
    )
    tables = extract_tables(html)
    assert len(tables) == 2
    assert "first" in tables[0]
    assert "second" in tables[1]


def test_ragged_rows_are_padded_to_the_widest() -> None:
    html = "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>"
    assert extract_tables(html) == ["| a | b |\n| --- | --- |\n| c |  |"]


def test_no_tables_means_no_tables() -> None:
    assert extract_tables("<div>just prose</div>") == []


# -- the degraded fallback -----------------------------------------------------------


def test_headings_survive_the_notes_div_b_h1_wrapper() -> None:
    assert html_to_markdown("<div><b><h1>Title</h1></b></div>").strip() == "# Title"


def test_the_wrapper_does_not_leave_stray_asterisks() -> None:
    # The <b> opens and closes OUTSIDE the <h1>, so a naive parser flushes lone "**" blocks.
    assert "**\n" not in html_to_markdown("<div><b><h1>Title</h1></b></div>")


def test_lists_and_inline_formatting() -> None:
    out = html_to_markdown("<ul><li>one</li><li><b>two</b></li></ul>")
    assert "- one" in out
    assert "- **two**" in out


def test_numbered_lists() -> None:
    out = html_to_markdown("<ol><li>one</li><li>two</li></ol>")
    assert "1. one" in out
    assert "2. two" in out


def test_links() -> None:
    out = html_to_markdown('<div><a href="https://example.com">text</a></div>')
    assert "[text](https://example.com)" in out


def test_object_placeholders_are_stripped() -> None:
    assert "￼" not in html_to_markdown("<div>text￼</div>")


def test_the_fallback_cannot_see_checklists() -> None:
    # Not a bug -- a fact, and the reason edit_note refuses to run from this path. A ticked
    # box and a plain bullet are both just <li>text</li> in the HTML. If this ever starts
    # failing, Apple has begun exporting checklist state and the refusal could be relaxed.
    out = html_to_markdown("<ul><li>done</li><li>todo</li></ul>")
    assert "[x]" not in out
    assert "[ ]" not in out
    assert "- done" in out
