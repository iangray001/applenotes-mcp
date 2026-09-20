"""Convert the HTML Apple Notes hands back via AppleScript into markdown.

This is not the primary read path (see notestore.py). It does two things:
  * extract_tables(): tables live outside the note protobuf (they are attachment objects
    holding a CRDT), so their content has to come from the HTML, which renders them well.
  * html_to_markdown(): a fallback when the protobuf cannot be read (no Full Disk Access,
    or a locked note). It loses checklists entirely and merges a numbered list into a
    preceding bullet list, so it is strictly a degraded mode.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

BLOCK_TAGS = {"div", "p", "h1", "h2", "h3", "h4", "li", "tr"}
HEADING_LEVEL = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}


class _NotesHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        # One entry per <table>, kept separate from `blocks` because two tables can sit
        # directly against each other: in `blocks` their rows are indistinguishable from
        # one longer table, and extract_tables has to tell them apart.
        self.tables: list[str] = []
        self._text: list[str] = []

        self._heading: int | None = None
        self._list_stack: list[str] = []  # "ul" / "ol"
        self._ol_counters: list[int] = []
        self._in_li = False

        # Tables are emitted as <object><table>...; rows accumulate then flush.
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

        self._bold = 0
        self._italic = 0
        self._mono = 0
        self._link: str | None = None

    # -- helpers ---------------------------------------------------------------

    def _emit(self, text: str) -> None:
        if self._cell is not None:
            self._cell.append(text)
        else:
            self._text.append(text)

    def _flush_block(self) -> None:
        text = "".join(self._text).strip()
        self._text = []
        # Notes wraps headings as <div><b><h1>..</h1></b></div>, so the <b> opens and
        # closes outside the heading and would otherwise flush as lone "**" blocks.
        if not text or not re.sub(r"[*`\s]", "", text):
            return

        if self._heading:
            self.blocks.append("#" * self._heading + " " + text)
        elif self._in_li:
            if self._list_stack and self._list_stack[-1] == "ol":
                self._ol_counters[-1] += 1
                marker = f"{self._ol_counters[-1]}."
            else:
                marker = "-"
            indent = "  " * (len(self._list_stack) - 1)
            self.blocks.append(f"{indent}{marker} {text}")
        else:
            self.blocks.append(text)

    # -- parser callbacks ------------------------------------------------------

    def handle_starttag(self, tag: str, attrs_list) -> None:
        attrs = dict(attrs_list)

        if tag == "table":
            self._flush_block()
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag in ("ul", "ol"):
            self._flush_block()
            self._list_stack.append(tag)
            self._ol_counters.append(0)
        elif tag == "li":
            self._flush_block()
            self._in_li = True
        elif tag in HEADING_LEVEL:
            self._flush_block()
            self._heading = HEADING_LEVEL[tag]
        elif tag in ("div", "p"):
            self._flush_block()
        elif tag in ("b", "strong"):
            self._bold += 1
            self._emit("**")
        elif tag in ("i", "em"):
            self._italic += 1
            self._emit("*")
        elif tag == "br":
            self._emit("\n")
        elif tag == "a" and attrs.get("href"):
            self._link = attrs["href"]
            self._emit("[")
        elif tag == "font" and "Monospaced" in (attrs.get("face") or ""):
            self._mono += 1
            self._emit("`")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self._flush_table()
        elif tag in ("ul", "ol"):
            self._flush_block()
            self._in_li = False
            if self._list_stack:
                self._list_stack.pop()
                self._ol_counters.pop()
        elif tag == "li":
            self._flush_block()
            self._in_li = False
        elif tag in HEADING_LEVEL:
            self._flush_block()
            self._heading = None
        elif tag in ("div", "p"):
            self._flush_block()
        elif tag in ("b", "strong") and self._bold:
            self._bold -= 1
            self._emit("**")
        elif tag in ("i", "em") and self._italic:
            self._italic -= 1
            self._emit("*")
        elif tag == "a" and self._link:
            self._emit(f"]({self._link})")
            self._link = None
        elif tag == "font" and self._mono:
            self._mono -= 1
            self._emit("`")

    def handle_data(self, data: str) -> None:
        if not data:
            return
        # Notes uses U+FFFC (object replacement) as an attachment placeholder.
        data = data.replace("￼", "")
        if data.strip() or self._text or self._cell is not None:
            self._emit(data)

    def _flush_table(self) -> None:
        rows = [r for r in (self._table or []) if any(c for c in r)]
        self._table = None
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]

        header, *body = rows
        # Notes bolds header cells itself; keeping the ** would re-bold on rewrite.
        header = [re.sub(r"^\*\*(.*)\*\*$", r"\1", c) for c in header]
        rendered = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * width) + " |",
            *("| " + " | ".join(row) + " |" for row in body),
        ]
        self.tables.append("\n".join(rendered))
        self.blocks.extend(rendered)

    def close(self):  # type: ignore[override]
        super().close()
        self._flush_block()
        return self


def html_to_markdown(html: str) -> str:
    """Best-effort markdown for a note's stored HTML. See module docstring for losses."""
    parser = _NotesHTMLParser()
    parser.feed(html)
    parser.close()

    out: list[str] = []
    for block in parser.blocks:
        is_list = re.match(r"^\s*(-|\d+\.)\s", block)
        is_table = block.startswith("| ")
        prev = out[-1] if out else ""
        prev_list = bool(re.match(r"^\s*(-|\d+\.)\s", prev))
        prev_table = prev.startswith("| ")

        # Blank line between blocks, but keep list items and table rows contiguous.
        if out and not ((is_list and prev_list) or (is_table and prev_table)):
            out.append("")
        out.append(block)

    text = "\n".join(out)
    text = re.sub(r"\*\*\s*\*\*", "", text)  # empty bold runs from styled wrappers
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def extract_tables(html: str) -> list[str]:
    """Markdown for each table in the note, in document order.

    Notes' protobuf represents a table only as a U+FFFC placeholder (its content is a
    CRDT in a separate attachment), so the table text has to come from the HTML, which
    renders tables faithfully.
    """
    parser = _NotesHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.tables
