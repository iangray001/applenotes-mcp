"""Tier 1: the renderer, driven from hand-built runs.

_paragraphs() and _render() take text plus attribute runs, so they can be exercised
directly -- no protobuf, no database. That makes it cheap to construct the awkward cases
(emoji offsets, an image next to a table) that are a nuisance to produce in a real note.
"""

from __future__ import annotations

import pytest

from applenotes_mcp.notestore import (
    OBJECT_PLACEHOLDER,
    STYLE_BULLET,
    STYLE_CHECKLIST,
    STYLE_HEADING,
    STYLE_NUMBERED,
    STYLE_TITLE,
    TABLE_UTI,
    Run,
    _paragraphs,
    _render,
)

BOLD, ITALIC = 1, 2


def render(
    *runs: tuple[str, Run],
    tables: list[str] | None = None,
    media: dict[str, str] | None = None,
) -> str:
    """Render (text, run) pairs as one note. Run.length is derived, so tests need not
    count UTF-16 code units by hand -- getting that wrong in the test would mask the very
    bug the test is looking for."""
    text = "".join(t for t, _ in runs)
    sized = []
    for chunk, run in runs:
        run.length = len(chunk.encode("utf-16-le")) // 2
        sized.append(run)
    return _render(_paragraphs(text, sized), tables or [], media or {})


# -- paragraph styles ----------------------------------------------------------------


def test_title_heading_and_body() -> None:
    out = render(
        ("Title\n", Run(0, style_type=STYLE_TITLE)),
        ("Heading\n", Run(0, style_type=STYLE_HEADING)),
        ("body\n", Run(0)),
    )
    assert out == "# Title\n\n## Heading\n\nbody\n"


def test_checklist_ticked_and_unticked() -> None:
    out = render(
        ("done\n", Run(0, style_type=STYLE_CHECKLIST, checked=True)),
        ("todo\n", Run(0, style_type=STYLE_CHECKLIST, checked=False)),
    )
    assert out == "- [x] done\n- [ ] todo\n"


def test_numbering_restarts_after_a_non_numbered_paragraph() -> None:
    out = render(
        ("one\n", Run(0, style_type=STYLE_NUMBERED)),
        ("two\n", Run(0, style_type=STYLE_NUMBERED)),
        ("interruption\n", Run(0)),
        ("one again\n", Run(0, style_type=STYLE_NUMBERED)),
    )
    assert "1. one\n2. two" in out
    assert out.rstrip().endswith("1. one again")


def test_a_numbered_list_after_a_bullet_list_gets_a_blank_line_between_them() -> None:
    # Without the blank line, a markdown parser reads the numbered items back as part of
    # the bullet list -- the exact corruption this reader exists to avoid.
    out = render(
        ("bullet\n", Run(0, style_type=STYLE_BULLET)),
        ("number\n", Run(0, style_type=STYLE_NUMBERED)),
    )
    assert out == "- bullet\n\n1. number\n"


def test_items_of_the_same_kind_stay_contiguous() -> None:
    out = render(
        ("one\n", Run(0, style_type=STYLE_BULLET)),
        ("two\n", Run(0, style_type=STYLE_BULLET)),
    )
    assert out == "- one\n- two\n"


def test_indent_becomes_nesting() -> None:
    out = render(
        ("top\n", Run(0, style_type=STYLE_BULLET)),
        ("nested\n", Run(0, style_type=STYLE_BULLET, indent=2)),
    )
    assert out == "- top\n    - nested\n"


# -- inline formatting ---------------------------------------------------------------


def test_bold_and_italic() -> None:
    assert render(("hi", Run(0, weight=BOLD)), ("\n", Run(0))) == "**hi**\n"
    assert render(("hi", Run(0, weight=ITALIC)), ("\n", Run(0))) == "*hi*\n"
    assert render(("hi", Run(0, weight=BOLD | ITALIC)), ("\n", Run(0))) == "***hi***\n"


def test_headings_are_not_re_bolded() -> None:
    # Notes stores headings as bold. Re-emitting that would give `# **Title**`, and worse,
    # accumulate another pair of asterisks on every round trip.
    out = render(("Title\n", Run(0, style_type=STYLE_TITLE, weight=BOLD)))
    assert out == "# Title\n"


def test_monospace_and_links() -> None:
    assert render(("code", Run(0, monospaced=True)), ("\n", Run(0))) == "`code`\n"
    assert (
        render(("text", Run(0, link="https://example.com")), ("\n", Run(0)))
        == "[text](https://example.com)\n"
    )


def test_surrounding_whitespace_stays_outside_the_markers() -> None:
    # `** bold **` is not bold in markdown; the asterisks must hug the text.
    out = render(("a ", Run(0)), ("bold ", Run(0, weight=BOLD)), ("b\n", Run(0)))
    assert out == "a **bold** b\n"


# -- UTF-16 arithmetic ---------------------------------------------------------------


def test_emoji_do_not_shift_the_runs_after_them() -> None:
    # Run lengths are UTF-16 code units; an emoji is a surrogate pair. If the slicing
    # treated them as one unit, every run after the emoji would slide by one and the bold
    # would land on the wrong characters.
    out = render(("a 🍺 b ", Run(0)), ("bold", Run(0, weight=BOLD)), (" c\n", Run(0)))
    assert out == "a 🍺 b **bold** c\n"


def test_several_emoji_in_a_row() -> None:
    out = render(("🍺🎓⚽ ", Run(0)), ("x", Run(0, weight=BOLD)), ("\n", Run(0)))
    assert out == "🍺🎓⚽ **x**\n"


# -- attachment placeholders ---------------------------------------------------------


def test_a_table_is_spliced_into_its_placeholder() -> None:
    table = "| a | b |\n| --- | --- |\n| 1 | 2 |"
    out = render(
        ("intro\n", Run(0)),
        (OBJECT_PLACEHOLDER + "\n", Run(0)),
        ("outro\n", Run(0)),
        tables=[table],
    )
    assert table in out
    assert OBJECT_PLACEHOLDER not in out


def test_an_image_before_a_table_does_not_steal_the_tables_placeholder() -> None:
    # The note is: prose, an IMAGE, then a TABLE, with one table so tables=[table]. The
    # image and table are told apart by the UTI on their runs, so the image resolves from
    # `media` and never consumes the table's slot. This was the placeholder-misalignment
    # bug; each placeholder is now resolved from the run that carries it.
    table = "| a | b |\n| --- | --- |\n| 1 | 2 |"
    out = render(
        ("intro\n", Run(0)),
        (OBJECT_PLACEHOLDER + "\n", Run(0, att_uti="public.png", att_identifier="img1")),
        (OBJECT_PLACEHOLDER + "\n", Run(0, att_uti=TABLE_UTI)),
        tables=[table],
        media={"img1": "![pic](file:///pic.png)"},
    )
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines[0] == "intro"
    assert lines[1] == "![pic](file:///pic.png)", "image slot did not get the image"
    assert table in out, "the single table was lost"


def test_a_file_attachment_is_resolved_from_media_by_its_identifier() -> None:
    out = render(
        (OBJECT_PLACEHOLDER + "\n", Run(0, att_uti="com.adobe.pdf", att_identifier="doc1")),
        media={"doc1": "[report.pdf](file:///report.pdf)"},
    )
    assert out.strip() == "[report.pdf](file:///report.pdf)"


def test_a_file_placeholder_not_in_media_falls_back_to_a_visible_marker() -> None:
    # DB unreadable, say: note_media returns {}. A raw U+FFFC must never survive -- it is
    # invisible to the model and silently corrupts a later edit -- so a marker stands in.
    out = render((OBJECT_PLACEHOLDER + "\n", Run(0, att_uti="public.png")), media={})
    assert OBJECT_PLACEHOLDER not in out
    assert "public.png" in out


def test_a_placeholder_with_no_attachment_info_is_treated_as_a_table() -> None:
    # Legacy notes may carry a placeholder run with no AttachmentInfo. The old behaviour
    # was to splice the next table there; preserve it rather than guess.
    out = render((OBJECT_PLACEHOLDER + "\n", Run(0)), tables=["| a |\n| --- |\n| 1 |"])
    assert "| a |" in out
    assert OBJECT_PLACEHOLDER not in out


# -- structural ----------------------------------------------------------------------


def test_output_always_ends_with_exactly_one_newline() -> None:
    assert render(("x\n\n\n", Run(0))) == "x\n"


def test_empty_note_renders_empty() -> None:
    assert render(("", Run(0))).strip() == ""
