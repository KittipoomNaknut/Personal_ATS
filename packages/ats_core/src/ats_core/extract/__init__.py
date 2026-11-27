"""Document extraction: PDF / DOCX bytes to an :class:`ExtractedDocument`.

Input is untrusted. The format is detected from magic bytes (never from the file
name), and size, page and decompression limits are enforced before parsing.
"""

from __future__ import annotations

import io
import zipfile
from typing import Final

from ats_core.extract.docx import extract_docx
from ats_core.extract.errors import (
    DocumentTooLargeError,
    EncryptedDocumentError,
    ExtractionError,
    UnsupportedFormatError,
)
from ats_core.extract.models import (
    BBox,
    BlockKind,
    ExtractedDocument,
    PageInfo,
    SourceFormat,
    TextLine,
)
from ats_core.extract.pdf import extract_pdf

__all__ = [
    "MAX_FILE_BYTES",
    "BBox",
    "BlockKind",
    "DocumentTooLargeError",
    "EncryptedDocumentError",
    "ExtractedDocument",
    "ExtractionError",
    "PageInfo",
    "SourceFormat",
    "TextLine",
    "UnsupportedFormatError",
    "detect_format",
    "extract_document",
]

MAX_FILE_BYTES: Final = 10 * 1024 * 1024


def detect_format(data: bytes) -> SourceFormat:
    """Identify PDF or DOCX from the file content."""
    if data.lstrip()[:5] == b"%PDF-":
        return SourceFormat.PDF
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "word/document.xml" in archive.namelist():
                    return SourceFormat.DOCX
        except zipfile.BadZipFile:
            pass
    raise UnsupportedFormatError("Only PDF and DOCX files are supported")


def extract_document(data: bytes) -> ExtractedDocument:
    """Extract an uploaded resume. Raises :class:`ExtractionError` subclasses."""
    if len(data) > MAX_FILE_BYTES:
        raise DocumentTooLargeError(f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB")
    if detect_format(data) is SourceFormat.PDF:
        return extract_pdf(data)
    return extract_docx(data)
