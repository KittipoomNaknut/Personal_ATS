from __future__ import annotations

import io
from collections.abc import Callable

import pytest
from builders import (
    CLEAN_RESUME_LINES,
    THAI_FONT,
    Grid,
    Picture,
    Text,
    build_pdf,
    text_lines,
)
from reportlab.lib import pdfencrypt
from reportlab.pdfgen import canvas

from ats_core.extract import (
    BlockKind,
    DocumentTooLargeError,
    EncryptedDocumentError,
    ExtractionError,
    SourceFormat,
)
from ats_core.extract import pdf as pdf_module
from ats_core.extract.pdf import MAX_PDF_PAGES, extract_pdf


def test_extracts_lines_in_reading_order() -> None:
    doc = extract_pdf(build_pdf(text_lines(CLEAN_RESUME_LINES)))
    assert doc.source_format is SourceFormat.PDF
    assert [line.text for line in doc.lines] == CLEAN_RESUME_LINES
    assert all(line.kind is BlockKind.BODY and line.page == 1 for line in doc.lines)
    assert doc.page_count == 1


def test_records_geometry_and_font_size() -> None:
    doc = extract_pdf(build_pdf([Text(100, 200, "Hello world", size=14)]))
    (line,) = doc.lines
    assert line.font_size == pytest.approx(14)
    assert line.bbox is not None
    # PDFium reports the ink box; the glyph "H" has ~1 pt left side bearing.
    assert line.bbox.x0 == pytest.approx(100, abs=2)
    assert line.bbox.bottom == pytest.approx(200, abs=4)


def test_thai_text_round_trips() -> None:
    thai = "ประสบการณ์ทำงาน นักพัฒนาซอฟต์แวร์ ผู้จัดการ น้ำ พันธุ์"
    doc = extract_pdf(build_pdf([Text(72, 100, thai, font=THAI_FONT)]))
    assert doc.lines[0].text == thai


def test_counts_white_and_tiny_text_as_hidden() -> None:
    doc = extract_pdf(
        build_pdf(
            [
                Text(72, 100, "Visible line"),
                Text(72, 120, "kubernetes docker aws", color=(1, 1, 1)),
                Text(72, 140, "terraform", size=1),
            ]
        )
    )
    hidden = {line.text: line.hidden_chars for line in doc.lines}
    assert hidden["Visible line"] == 0
    assert hidden["kubernetes docker aws"] == len("kubernetesdockeraws")
    assert hidden["terraform"] == len("terraform")


def test_detects_two_column_page() -> None:
    sidebar = text_lines([f"Skill {i}" for i in range(20)], x=40)
    main = text_lines([f"Experience detail line number {i}" for i in range(25)], x=230)
    doc = extract_pdf(build_pdf(sidebar + main))
    assert doc.pages[0].multi_column


def test_single_column_page_is_not_flagged() -> None:
    doc = extract_pdf(build_pdf(text_lines(CLEAN_RESUME_LINES)))
    assert not doc.pages[0].multi_column


def test_counts_tables_and_images() -> None:
    doc = extract_pdf(
        build_pdf(
            [
                *text_lines(["Skills table"]),
                Grid(xs=(72, 250, 450), tops=(300, 330, 360, 390)),
                Picture(400, 500),
            ]
        )
    )
    assert doc.table_count == 1
    assert doc.image_count == 1
    assert doc.pages[0].table_count == 1


def test_image_only_page_has_no_chars() -> None:
    doc = extract_pdf(build_pdf([Picture(72, 72, 400, 600)]))
    assert doc.pages[0].char_count == 0
    assert doc.pages[0].image_count == 1
    assert doc.lines == ()


def test_multiple_pages_are_numbered() -> None:
    doc = extract_pdf(build_pdf(text_lines(["Page one"]), text_lines(["Page two"])))
    assert [(line.text, line.page) for line in doc.lines] == [("Page one", 1), ("Page two", 2)]
    assert [p.number for p in doc.pages] == [1, 2]


def test_rejects_too_many_pages() -> None:
    data = build_pdf(*[text_lines([f"p{i}"]) for i in range(MAX_PDF_PAGES + 1)])
    with pytest.raises(DocumentTooLargeError):
        extract_pdf(data)


def _encrypted(user_password: str, owner_password: str | None = None) -> bytes:
    buf = io.BytesIO()
    enc = pdfencrypt.StandardEncryption(user_password, ownerPassword=owner_password)
    c = canvas.Canvas(buf, encrypt=enc)
    c.drawString(72, 700, "secret resume")
    c.save()
    return buf.getvalue()


def test_password_protected_pdf_is_rejected() -> None:
    with pytest.raises(EncryptedDocumentError):
        extract_pdf(_encrypted("open-sesame"))


def test_owner_password_only_pdf_is_readable() -> None:
    # Permissions-only encryption: anyone (and any ATS) can still read the text.
    doc = extract_pdf(_encrypted("", owner_password="owner"))
    assert doc.lines[0].text == "secret resume"


@pytest.mark.parametrize(
    "data",
    [b"%PDF-1.4\nnot really a pdf", build_pdf(text_lines(["x"]))[:150]],
    ids=["garbage", "truncated"],
)
def test_corrupt_pdf_raises_extraction_error(data: bytes) -> None:
    with pytest.raises(ExtractionError) as info:
        extract_pdf(data)
    assert not isinstance(info.value, EncryptedDocumentError)


def test_unexpected_parser_errors_become_extraction_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object) -> None:
        raise AttributeError("malformed object graph")

    monkeypatch.setattr(pdf_module, "_read_document", explode)
    with pytest.raises(ExtractionError) as info:
        extract_pdf(build_pdf(text_lines(["x"])))
    assert isinstance(info.value.__cause__, AttributeError)


def test_table_detection_failure_degrades_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_open(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("pdfminer choked")

    monkeypatch.setattr(pdf_module.pdfplumber, "open", broken_open)
    doc = extract_pdf(build_pdf(text_lines(["Still readable"])))
    assert doc.lines[0].text == "Still readable"
    assert doc.table_count == 0


def _hidden_by_text(draw: Callable[[canvas.Canvas], None]) -> dict[str, int]:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    c.setFont("Helvetica", 11)
    draw(c)
    c.save()
    return {line.text: line.hidden_chars for line in extract_pdf(buf.getvalue()).lines}


def test_white_text_on_dark_background_is_visible() -> None:
    def draw(c: canvas.Canvas) -> None:
        c.setFillColorRGB(0.1, 0.2, 0.4)
        c.rect(300, 600, 250, 100, fill=1, stroke=0)  # dark sidebar
        c.setFillColorRGB(1, 1, 1)
        c.drawString(310, 650, "Sidebar heading")

    assert _hidden_by_text(draw) == {"Sidebar heading": 0}


def test_dark_text_on_dark_background_is_hidden() -> None:
    def draw(c: canvas.Canvas) -> None:
        c.setFillColorRGB(0.1, 0.1, 0.1)
        c.rect(50, 600, 500, 100, fill=1, stroke=0)
        c.setFillColorRGB(0.12, 0.12, 0.12)
        c.drawString(60, 650, "camouflaged")

    assert _hidden_by_text(draw) == {"camouflaged": len("camouflaged")}


def test_light_grey_text_on_white_is_visible() -> None:
    def draw(c: canvas.Canvas) -> None:
        c.setFillColorRGB(0.6, 0.6, 0.6)
        c.drawString(72, 650, "Subtle caption")

    assert _hidden_by_text(draw) == {"Subtle caption": 0}


def test_grey_and_cmyk_white_text_is_hidden() -> None:
    def draw(c: canvas.Canvas) -> None:
        c.drawString(72, 700, "Visible")
        c.setFillGray(1)
        c.drawString(72, 680, "greyhidden")
        c.setFillColorCMYK(0, 0, 0, 0)
        c.drawString(72, 660, "cmykhidden")

    assert _hidden_by_text(draw) == {"Visible": 0, "greyhidden": 10, "cmykhidden": 10}


def test_invisible_render_mode_is_hidden() -> None:
    def draw(c: canvas.Canvas) -> None:
        text = c.beginText(72, 650)
        text.setTextRenderMode(3)  # "neither fill nor stroke", common for OCR layers
        text.setFont("Helvetica", 11)
        text.textOut("invisible")
        c.drawText(text)

    assert _hidden_by_text(draw) == {"invisible": len("invisible")}


def test_transparent_text_is_hidden() -> None:
    def draw(c: canvas.Canvas) -> None:
        c.setFillAlpha(0)
        c.drawString(72, 650, "transparent")

    assert _hidden_by_text(draw) == {"transparent": len("transparent")}


def test_spaces_between_words_are_reconstructed_from_gaps() -> None:
    # Words drawn separately without space glyphs, as many PDF generators do.
    doc = extract_pdf(build_pdf([Text(72, 100, "Senior"), Text(110, 100, "Engineer")]))
    assert doc.lines[0].text == "Senior Engineer"


def test_kerned_letters_are_not_split() -> None:
    doc = extract_pdf(build_pdf([Text(72, 100, "Kuber"), Text(72 + 28.7, 100, "netes")]))
    assert doc.lines[0].text == "Kubernetes"


def test_lines_on_the_same_baseline_merge_left_to_right() -> None:
    # Drawn right part first: geometry, not stream order, decides reading order.
    doc = extract_pdf(build_pdf([Text(300, 100, "2020 - 2023"), Text(72, 100, "Engineer")]))
    assert doc.lines[0].text == "Engineer 2020 - 2023"


def test_whitespace_only_lines_are_dropped() -> None:
    doc = extract_pdf(build_pdf([Text(72, 100, "     "), Text(72, 140, "Real line")]))
    assert [line.text for line in doc.lines] == ["Real line"]


def test_literal_line_break_characters_are_not_emitted() -> None:
    doc = extract_pdf(build_pdf([Text(72, 100, "Python\r\nDeveloper")]))
    assert "\r" not in doc.text
    assert "\n" not in doc.lines[0].text
