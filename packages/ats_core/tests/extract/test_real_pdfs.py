"""Regression tests on real word-processor exports (see fixtures/make_libreoffice_pdfs.py).

These guard the two failure modes found during development:
pdfminer mis-decoding Thai in well-formed PDFs, and white-on-dark text being
mistaken for hidden keyword stuffing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from builders import CLEAN_RESUME_LINES, HIDDEN_KEYWORDS, THAI_RESUME_LINES

from ats_core.extract import ExtractedDocument, extract_document
from ats_core.parseability import IssueCode, audit

FIXTURES = Path(__file__).parent.parent / "fixtures" / "libreoffice"


def load(name: str) -> ExtractedDocument:
    return extract_document((FIXTURES / f"{name}.pdf").read_bytes())


def issue_codes(doc: ExtractedDocument) -> set[IssueCode]:
    return {issue.code for issue in audit(doc).issues}


@pytest.mark.parametrize("name", ["th_sarabun", "th_garuda", "th_ibm_plex"])
def test_thai_characters_are_extracted_correctly(name: str) -> None:
    lines = [line.text for line in load(name).lines]
    assert len(lines) == len(THAI_RESUME_LINES)
    # Thai has no spaces between words, so character accuracy is what matters
    # for keyword matching. Ignore spacing differences in this check.
    for got, expected in zip(lines, THAI_RESUME_LINES, strict=True):
        assert got.replace(" ", "") == expected.replace(" ", "")


@pytest.mark.parametrize("name", ["th_sarabun", "th_garuda"])
def test_thai_lines_are_exact_including_spaces(name: str) -> None:
    assert [line.text for line in load(name).lines] == THAI_RESUME_LINES


@pytest.mark.parametrize("name", ["th_sarabun", "th_garuda", "th_ibm_plex"])
def test_good_thai_pdf_is_not_reported_as_garbled(name: str) -> None:
    found = issue_codes(load(name))
    assert IssueCode.GARBLED_TEXT not in found
    assert IssueCode.MISSING_SECTIONS not in found
    assert IssueCode.BUDDHIST_ERA_YEARS in found  # the sample really uses พ.ศ.


def test_clean_english_resume() -> None:
    doc = load("en_clean")
    assert [line.text for line in doc.lines] == CLEAN_RESUME_LINES
    assert issue_codes(doc) == set()


def test_white_text_in_dark_cell_is_not_hidden() -> None:
    doc = load("en_dark_cell_white_text")
    assert "CONTACT SIDEBAR HEADING" in doc.text
    assert sum(line.hidden_chars for line in doc.lines) == 0
    assert IssueCode.HIDDEN_TEXT not in issue_codes(doc)


def test_white_text_on_white_page_is_hidden() -> None:
    doc = load("en_white_on_white")
    assert sum(line.hidden_chars for line in doc.lines) == len(HIDDEN_KEYWORDS.replace(" ", ""))
    assert IssueCode.HIDDEN_TEXT in issue_codes(doc)


def test_ruled_table_is_detected() -> None:
    doc = load("en_table")
    assert doc.table_count == 1
    assert issue_codes(doc) == {IssueCode.TABLES_USED}


def test_two_column_section_is_detected() -> None:
    doc = load("en_two_column")
    assert doc.pages[0].multi_column
    assert IssueCode.MULTI_COLUMN_LAYOUT in issue_codes(doc)
