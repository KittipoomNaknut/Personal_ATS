"""DOCX text and structure extraction (python-docx, MIT licensed).

Text is emitted in the order a typical ATS reads it: body paragraphs and tables in
document order, then text boxes, then headers and footers. Each line is tagged
with its :class:`BlockKind` so the parseability audit can flag content that many
ATSs skip (headers, footers, text boxes) or garble (tables).
"""

from __future__ import annotations

import dataclasses
import io
import zipfile
from collections.abc import Iterable, Iterator
from typing import Final

import docx
from docx.document import Document
from docx.oxml.ns import qn
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.section import _Footer, _Header
from docx.table import Table
from docx.text.paragraph import Paragraph

from ats_core.extract.errors import DocumentTooLargeError, ExtractionError
from ats_core.extract.models import BlockKind, ExtractedDocument, SourceFormat, TextLine

__all__ = ["MAX_DOCX_UNCOMPRESSED_BYTES", "extract_docx"]

MAX_DOCX_UNCOMPRESSED_BYTES: Final = 50 * 1024 * 1024  # zip-bomb guard
_MIN_VISIBLE_HALF_POINTS: Final = 4  # w:sz is in half-points; 4 = 2 pt
_W_P: Final = qn("w:p")
_W_TBL: Final = qn("w:tbl")
_W_T: Final = qn("w:t")
_W_VAL: Final = qn("w:val")
_TEXTBOX_CONTENT: Final = qn("w:txbxContent")
_BLIP: Final = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_VML_IMAGE: Final = "{urn:schemas-microsoft-com:vml}imagedata"


def _check_archive(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            total = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as exc:
        raise ExtractionError("DOCX archive is corrupt") from exc
    if "word/document.xml" not in names:
        raise ExtractionError("DOCX archive has no word/document.xml")
    if total > MAX_DOCX_UNCOMPRESSED_BYTES:
        raise DocumentTooLargeError("DOCX uncompressed size exceeds the limit")


def _run_text(run: BaseOxmlElement) -> str:
    # Direct w:t children only: text boxes nested inside a run are handled separately.
    return "".join(t.text or "" for t in run.findall(_W_T))


def _run_size_half_points(run: BaseOxmlElement) -> int | None:
    size = run.find(f"{qn('w:rPr')}/{qn('w:sz')}")
    value = size.get(_W_VAL) if size is not None else None
    return int(value) if value is not None and value.isdigit() else None


def _run_is_hidden(run: BaseOxmlElement) -> bool:
    rpr = run.find(qn("w:rPr"))
    if rpr is None:
        return False
    vanish = rpr.find(qn("w:vanish"))
    if vanish is not None and vanish.get(_W_VAL, "true") not in ("0", "false"):
        return True
    color = rpr.find(qn("w:color"))
    if color is not None and (color.get(_W_VAL) or "").upper() == "FFFFFF":
        return True
    size = _run_size_half_points(run)
    return size is not None and size < _MIN_VISIBLE_HALF_POINTS


def _paragraph_line(paragraph: Paragraph, kind: BlockKind) -> TextLine | None:
    text = paragraph.text
    if not text.strip():
        return None
    hidden = 0
    sizes: list[float] = []
    for run in paragraph._p.xpath("./w:r | ./w:hyperlink/w:r"):
        run_text = _run_text(run).strip()
        if not run_text:
            continue
        if _run_is_hidden(run):
            hidden += len(run_text)
        half_points = _run_size_half_points(run)
        if half_points is not None:
            sizes.append(half_points / 2)
    sizes.sort()
    return TextLine(
        text=text,
        kind=kind,
        font_size=sizes[len(sizes) // 2] if sizes else None,
        min_font_size=sizes[0] if sizes else None,
        hidden_chars=hidden,
    )


def _table_lines(table: Table, kind: BlockKind) -> Iterator[TextLine]:
    seen: set[int] = set()
    for row in table.rows:
        for cell in row.cells:
            # Merged cells are returned once per grid position; emit each once.
            if id(cell._tc) in seen:
                continue
            seen.add(id(cell._tc))
            for paragraph in cell.paragraphs:
                line = _paragraph_line(paragraph, kind)
                if line:
                    yield line
            for nested in cell.tables:
                yield from _table_lines(nested, kind)


def _body_lines(document: Document) -> Iterator[TextLine]:
    for child in document.element.body.iterchildren():
        if child.tag == _W_P:
            line = _paragraph_line(Paragraph(child, document), BlockKind.BODY)
            if line:
                yield line
        elif child.tag == _W_TBL:
            yield from _table_lines(Table(child, document), BlockKind.TABLE)


def _textbox_lines(document: Document) -> Iterator[TextLine]:
    # Word stores each text box twice (DrawingML + VML fallback); keep one copy.
    seen: set[str] = set()
    for box in document.element.body.iter(_TEXTBOX_CONTENT):
        for p in box.iter(_W_P):
            line = _paragraph_line(Paragraph(p, document), BlockKind.TEXTBOX)
            if line and line.text not in seen:
                seen.add(line.text)
                yield line


def _part_lines(
    paragraphs: Iterable[Paragraph], tables: Iterable[Table], kind: BlockKind
) -> Iterator[TextLine]:
    for paragraph in paragraphs:
        line = _paragraph_line(paragraph, kind)
        if line:
            yield line
    for table in tables:
        for line in _table_lines(table, kind):
            yield dataclasses.replace(line, kind=kind)


def _header_footer_lines(document: Document) -> Iterator[TextLine]:
    for section in document.sections:
        parts: tuple[tuple[_Header | _Footer, BlockKind], ...] = (
            (section.header, BlockKind.HEADER),
            (section.first_page_header, BlockKind.HEADER),
            (section.even_page_header, BlockKind.HEADER),
            (section.footer, BlockKind.FOOTER),
            (section.first_page_footer, BlockKind.FOOTER),
            (section.even_page_footer, BlockKind.FOOTER),
        )
        for part, kind in parts:
            # "Linked to previous" means the section defines no header/footer of its own.
            if not part.is_linked_to_previous:
                yield from _part_lines(part.paragraphs, part.tables, kind)


def _max_columns(document: Document) -> int:
    best = 1
    for sect_pr in document.element.body.iter(qn("w:sectPr")):
        cols = sect_pr.find(qn("w:cols"))
        num = cols.get(qn("w:num")) if cols is not None else None
        if num is not None and num.isdigit():
            best = max(best, int(num))
    return best


def _count_tables(document: Document) -> int:
    return sum(1 for _ in document.element.body.iter(_W_TBL))


def _count_images(document: Document) -> int:
    body = document.element.body
    return sum(1 for _ in body.iter(_BLIP)) + sum(1 for _ in body.iter(_VML_IMAGE))


def extract_docx(data: bytes) -> ExtractedDocument:
    """Extract text lines and structure facts from DOCX bytes.

    The input is untrusted: any parser failure becomes an :class:`ExtractionError`.
    """
    _check_archive(data)
    try:
        document = docx.Document(io.BytesIO(data))
        return ExtractedDocument(
            source_format=SourceFormat.DOCX,
            lines=(
                *_body_lines(document),
                *_textbox_lines(document),
                *_header_footer_lines(document),
            ),
            image_count=_count_images(document),
            table_count=_count_tables(document),
            max_section_columns=_max_columns(document),
        )
    except Exception as exc:  # malformed input can make the parser raise anything
        raise ExtractionError("DOCX could not be parsed") from exc
