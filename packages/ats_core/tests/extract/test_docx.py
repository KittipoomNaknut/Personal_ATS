from __future__ import annotations

import io
import zipfile

import pytest
from builders import add_picture, add_run, add_textbox, docx_bytes, new_docx, set_columns
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Pt

from ats_core.extract import BlockKind, DocumentTooLargeError, ExtractionError, SourceFormat
from ats_core.extract import docx as docx_module
from ats_core.extract.docx import extract_docx


def kinds(data: bytes) -> list[tuple[str, BlockKind]]:
    return [(line.text, line.kind) for line in extract_docx(data).lines]


def test_body_paragraphs_and_tables_in_document_order() -> None:
    d = new_docx()
    d.add_paragraph("Experience")
    table = d.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Python"
    table.cell(0, 1).text = "5 years"
    d.add_paragraph("Education")
    d.add_paragraph("   ")  # blank lines are dropped
    doc = extract_docx(docx_bytes(d))
    assert doc.source_format is SourceFormat.DOCX
    assert [(line.text, line.kind) for line in doc.lines] == [
        ("Experience", BlockKind.BODY),
        ("Python", BlockKind.TABLE),
        ("5 years", BlockKind.TABLE),
        ("Education", BlockKind.BODY),
    ]
    assert doc.table_count == 1
    assert doc.pages == ()


def test_merged_cells_are_emitted_once() -> None:
    d = new_docx()
    table = d.add_table(rows=1, cols=3)
    merged = table.cell(0, 0).merge(table.cell(0, 1))
    merged.text = "Merged"
    table.cell(0, 2).text = "Single"
    assert kinds(docx_bytes(d)) == [("Merged", BlockKind.TABLE), ("Single", BlockKind.TABLE)]


def test_nested_tables_are_read() -> None:
    d = new_docx()
    outer = d.add_table(rows=1, cols=1)
    inner = outer.cell(0, 0).add_table(rows=1, cols=1)
    inner.cell(0, 0).text = "Inner"
    assert ("Inner", BlockKind.TABLE) in kinds(docx_bytes(d))
    assert extract_docx(docx_bytes(d)).table_count == 2


def test_headers_and_footers_are_tagged() -> None:
    d = new_docx()
    d.add_paragraph("Body text")
    d.sections[0].header.paragraphs[0].text = "somchai@example.com"
    d.sections[0].footer.paragraphs[0].text = "Page 1"
    footer_table = d.sections[0].footer.add_table(rows=1, cols=1, width=d.sections[0].page_width)
    footer_table.cell(0, 0).text = "Footer cell"
    assert kinds(docx_bytes(d)) == [
        ("Body text", BlockKind.BODY),
        ("somchai@example.com", BlockKind.HEADER),
        ("Page 1", BlockKind.FOOTER),
        ("Footer cell", BlockKind.FOOTER),
    ]


def test_header_linked_to_previous_section_is_not_duplicated() -> None:
    d = new_docx()
    d.sections[0].header.paragraphs[0].text = "Shared header"
    d.add_section()
    assert kinds(docx_bytes(d)).count(("Shared header", BlockKind.HEADER)) == 1


def test_text_box_is_extracted_once_and_not_duplicated_in_body() -> None:
    d = new_docx()
    d.add_paragraph("Body")
    add_textbox(d, "Contact in a box")
    lines = kinds(docx_bytes(d))
    assert lines == [("Body", BlockKind.BODY), ("Contact in a box", BlockKind.TEXTBOX)]


@pytest.mark.parametrize(
    ("vanish", "white", "size", "expected_hidden"),
    [
        (True, False, None, True),
        (False, True, None, True),
        (False, False, 1.0, True),
        (False, False, 11.0, False),
        (False, False, None, False),
    ],
    ids=["vanish", "white", "1pt", "11pt", "plain"],
)
def test_hidden_runs(vanish: bool, white: bool, size: float | None, expected_hidden: bool) -> None:
    d = new_docx()
    add_run(d, "keyword", hidden=vanish, white=white, size=size)
    (line,) = extract_docx(docx_bytes(d)).lines
    assert line.hidden_chars == (len("keyword") if expected_hidden else 0)


def test_font_sizes_are_recorded() -> None:
    d = new_docx()
    p = d.add_paragraph()
    for text, size in (("small ", 8), ("normal ", 11), ("big", 14)):
        p.add_run(text).font.size = Pt(size)
    (line,) = extract_docx(docx_bytes(d)).lines
    assert line.font_size == 11
    assert line.min_font_size == 8


def test_hyperlink_runs_are_included_in_sizes() -> None:
    d = new_docx()
    p = d.add_paragraph()
    p._p.append(
        parse_xml(
            f'<w:hyperlink {nsdecls("w")}><w:r><w:rPr><w:sz w:val="2"/></w:rPr>'
            "<w:t>tinylink</w:t></w:r></w:hyperlink>"
        )
    )
    (line,) = extract_docx(docx_bytes(d)).lines
    assert line.text == "tinylink"
    assert line.hidden_chars == len("tinylink")


def test_section_columns_and_images() -> None:
    d = new_docx()
    d.add_paragraph("Two column resume")
    set_columns(d, 2)
    add_picture(d)
    doc = extract_docx(docx_bytes(d))
    assert doc.max_section_columns == 2
    assert doc.image_count == 1


def test_default_document_has_one_column_and_no_images() -> None:
    d = new_docx()
    d.add_paragraph("Plain")
    doc = extract_docx(docx_bytes(d))
    assert doc.max_section_columns == 1
    assert doc.image_count == 0


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buf.getvalue()


def test_zip_without_document_xml_is_rejected() -> None:
    with pytest.raises(ExtractionError):
        extract_docx(_zip({"hello.txt": b"hi"}))


def test_corrupt_zip_is_rejected() -> None:
    with pytest.raises(ExtractionError):
        extract_docx(b"PK\x03\x04 definitely not a zip")


def test_invalid_document_xml_is_rejected() -> None:
    with pytest.raises(ExtractionError):
        extract_docx(_zip({"word/document.xml": b"<not-xml", "[Content_Types].xml": b"<x/>"}))


def test_decompression_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    d = new_docx()
    d.add_paragraph("x" * 1000)
    monkeypatch.setattr(docx_module, "MAX_DOCX_UNCOMPRESSED_BYTES", 1000)
    with pytest.raises(DocumentTooLargeError):
        extract_docx(docx_bytes(d))


def test_empty_runs_and_empty_header_paragraphs_are_skipped() -> None:
    d = new_docx()
    p = d.add_paragraph()
    p.add_run("")  # empty run alongside real text
    p.add_run("Real text")
    d.sections[0].header.paragraphs[0].text = ""  # header defined but blank
    d.sections[0].header.add_paragraph("Header line")
    assert kinds(docx_bytes(d)) == [
        ("Real text", BlockKind.BODY),
        ("Header line", BlockKind.HEADER),
    ]
