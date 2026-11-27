from __future__ import annotations

import io
import zipfile

import pytest
from builders import build_pdf, docx_bytes, new_docx, text_lines

from ats_core import extract as extract_module
from ats_core.extract import (
    DocumentTooLargeError,
    SourceFormat,
    UnsupportedFormatError,
    detect_format,
    extract_document,
)


def test_detects_pdf_and_docx_by_content() -> None:
    d = new_docx()
    d.add_paragraph("hi")
    assert detect_format(build_pdf(text_lines(["hi"]))) is SourceFormat.PDF
    assert detect_format(docx_bytes(d)) is SourceFormat.DOCX


def test_leading_whitespace_before_pdf_header_is_tolerated() -> None:
    assert detect_format(b"\n\r\n" + build_pdf(text_lines(["hi"]))) is SourceFormat.PDF


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"plain text resume",
        b"\x89PNG\r\n\x1a\n",
        b"PK\x03\x04broken zip",
    ],
    ids=["empty", "text", "png", "broken-zip"],
)
def test_rejects_unsupported_content(data: bytes) -> None:
    with pytest.raises(UnsupportedFormatError):
        detect_format(data)


def test_rejects_non_word_zip() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("xl/workbook.xml", "<x/>")  # an .xlsx is not a resume
    with pytest.raises(UnsupportedFormatError):
        detect_format(buf.getvalue())


def test_extract_document_dispatches() -> None:
    d = new_docx()
    d.add_paragraph("from docx")
    assert extract_document(docx_bytes(d)).lines[0].text == "from docx"
    assert extract_document(build_pdf(text_lines(["from pdf"]))).lines[0].text == "from pdf"


def test_extract_document_enforces_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_module, "MAX_FILE_BYTES", 10)
    with pytest.raises(DocumentTooLargeError):
        extract_document(build_pdf(text_lines(["hi"])))
