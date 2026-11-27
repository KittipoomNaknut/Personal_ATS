"""Format-independent representation of an extracted document."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["BBox", "BlockKind", "ExtractedDocument", "PageInfo", "SourceFormat", "TextLine"]


class SourceFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"


class BlockKind(StrEnum):
    """Where a line of text lives in the document structure."""

    BODY = "body"
    TABLE = "table"
    HEADER = "header"
    FOOTER = "footer"
    TEXTBOX = "textbox"


@dataclass(frozen=True, slots=True)
class BBox:
    """Bounding box in PDF points, origin at the top-left of the page."""

    x0: float
    top: float
    x1: float
    bottom: float


@dataclass(frozen=True, slots=True)
class TextLine:
    text: str
    kind: BlockKind = BlockKind.BODY
    page: int | None = None
    """1-based page number (PDF only; DOCX has no fixed pagination)."""
    bbox: BBox | None = None
    font_size: float | None = None
    """Median font size in points, if known."""
    min_font_size: float | None = None
    hidden_chars: int = 0
    """Characters a human cannot see: white, hidden-formatted or < 2 pt."""


@dataclass(frozen=True, slots=True)
class PageInfo:
    number: int
    width: float
    height: float
    char_count: int
    image_count: int
    table_count: int
    multi_column: bool
    unmapped_chars: int = 0
    """Characters whose font has no usable Unicode mapping (reported by PDFium)."""


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """Text in the order a typical ATS reads it, plus the layout facts that affect parsing."""

    source_format: SourceFormat
    lines: tuple[TextLine, ...]
    pages: tuple[PageInfo, ...] = ()
    """PDF pages. Empty for DOCX."""
    image_count: int = 0
    table_count: int = 0
    max_section_columns: int = 1
    """Highest column count of any DOCX section (always 1 for PDF; see ``PageInfo``)."""

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    def lines_of(self, *kinds: BlockKind) -> tuple[TextLine, ...]:
        return tuple(line for line in self.lines if line.kind in kinds)

    @property
    def page_count(self) -> int:
        return len(self.pages)
