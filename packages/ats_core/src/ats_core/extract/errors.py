"""Errors raised while reading uploaded documents."""

from __future__ import annotations

__all__ = [
    "DocumentTooLargeError",
    "EncryptedDocumentError",
    "ExtractionError",
    "UnsupportedFormatError",
]


class ExtractionError(Exception):
    """The document could not be read."""


class UnsupportedFormatError(ExtractionError):
    """The file is neither a PDF nor a DOCX document."""


class DocumentTooLargeError(ExtractionError):
    """The file exceeds the size or page limits."""


class EncryptedDocumentError(ExtractionError):
    """The document is password protected."""
