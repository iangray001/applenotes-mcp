"""Tier 1: the pure parts of attachment resolution.

The database lookup and the on-disk search need a real library, so they live in the live
tier. What is testable here is the rendering decision -- image vs generic file, and the
markdown emitted -- plus the fixture proof that a real note's placeholders are typed
correctly so an image before a table no longer steals its slot.
"""

from __future__ import annotations

from pathlib import Path

from applenotes_mcp.attachments import _attachment_markdown, _is_image
from applenotes_mcp.notestore import decode, _paragraphs, _render
from applenotes_mcp.html_to_markdown import extract_tables

FIXTURES = Path(__file__).parent / "fixtures"


# -- image vs file classification ----------------------------------------------------


def test_image_utis_are_images() -> None:
    assert _is_image("public.png", Path("x.png"))
    assert _is_image("public.jpeg", Path("x.jpg"))


def test_non_image_utis_are_not_images() -> None:
    assert not _is_image("com.adobe.pdf", Path("x.pdf"))
    assert not _is_image("public.plain-text", Path("x.txt"))


def test_extension_is_a_fallback_when_the_uti_is_unknown() -> None:
    assert _is_image(None, Path("photo.HEIC"))
    assert not _is_image(None, Path("archive.zip"))


# -- rendered markdown ---------------------------------------------------------------


def test_an_image_renders_as_an_image() -> None:
    md = _attachment_markdown("public.png", "pic.png", Path("/tmp/pic.png"))
    assert md == "![pic.png](file:///tmp/pic.png)"


def test_a_file_renders_as_a_link() -> None:
    md = _attachment_markdown("com.adobe.pdf", "report.pdf", Path("/tmp/report.pdf"))
    assert md == "[report.pdf](file:///tmp/report.pdf)"


def test_spaces_in_the_path_are_percent_encoded() -> None:
    # Notes' container path contains "Group Containers"; an unencoded space breaks the link.
    md = _attachment_markdown("public.png", "a.png", Path("/tmp/a dir/a.png"))
    assert "a%20dir" in md
    assert " " not in md.split("(", 1)[1]


def test_a_missing_file_renders_a_marker_not_a_broken_link() -> None:
    md = _attachment_markdown("public.png", "gone.png", None)
    assert "gone.png" in md
    assert "file://" not in md


# -- the real note, keyed by injected media (hermetic) -------------------------------


def test_real_note_placeholders_are_typed_and_ordered() -> None:
    """The captured note is: prose, image, PDF, table. With media injected for the two
    files, each placeholder must resolve to its own thing, in order -- the table (last)
    still landing correctly despite two file placeholders before it."""
    text, runs = decode((FIXTURES / "attachments.zdata").read_bytes())
    tables = extract_tables((FIXTURES / "attachments.html").read_text())
    media = {
        "AC0CEF08-CDBA-4859-AA91-265345D071F0": "![an_image.png](file:///img.png)",
        "BB493554-D23B-4876-8E70-7BA547FA7173": "[test.pdf](file:///doc.pdf)",
    }
    out = _render(_paragraphs(text, runs), tables, media)

    assert "![an_image.png](file:///img.png)" in out
    assert "[test.pdf](file:///doc.pdf)" in out
    assert "| Heading 1 | Heading 2 |" in out
    # order: image before pdf before table
    assert out.index("img.png") < out.index("doc.pdf") < out.index("Heading 1")
