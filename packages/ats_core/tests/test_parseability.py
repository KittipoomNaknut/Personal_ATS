from __future__ import annotations

import string
from collections.abc import Sequence

import pytest
from builders import (
    CLEAN_RESUME_LINES,
    Grid,
    Picture,
    Text,
    add_textbox,
    build_pdf,
    docx_bytes,
    mixed_text,
    new_docx,
    set_columns,
    text_lines,
)

from ats_core.extract import (
    BlockKind,
    ExtractedDocument,
    PageInfo,
    SourceFormat,
    TextLine,
    extract_document,
)
from ats_core.parseability import (
    MESSAGES,
    PENALTY,
    Issue,
    IssueCode,
    Severity,
    audit,
    render_issue,
)
from ats_core.text.sections import Section


def page(
    number: int = 1,
    *,
    char_count: int = 1500,
    image_count: int = 0,
    multi_column: bool = False,
) -> PageInfo:
    return PageInfo(
        number=number,
        width=612.0,
        height=792.0,
        char_count=char_count,
        image_count=image_count,
        table_count=0,
        multi_column=multi_column,
    )


def pdf_doc(
    lines: Sequence[str | TextLine],
    pages: tuple[PageInfo, ...] = (page(),),
    *,
    image_count: int = 0,
    table_count: int = 0,
) -> ExtractedDocument:
    return ExtractedDocument(
        source_format=SourceFormat.PDF,
        lines=tuple(TextLine(x, page=1) if isinstance(x, str) else x for x in lines),
        pages=pages,
        image_count=image_count,
        table_count=table_count,
    )


def docx_doc(lines: Sequence[TextLine], *, max_section_columns: int = 1) -> ExtractedDocument:
    return ExtractedDocument(
        source_format=SourceFormat.DOCX,
        lines=tuple(lines),
        max_section_columns=max_section_columns,
    )


def codes(doc: ExtractedDocument) -> set[IssueCode]:
    return {issue.code for issue in audit(doc).issues}


# ── Clean baseline ───────────────────────────────────────────────────────


def test_clean_resume_has_no_issues() -> None:
    report = audit(pdf_doc(CLEAN_RESUME_LINES))
    assert report.issues == ()
    assert report.score == 1.0
    assert {Section.EXPERIENCE, Section.EDUCATION, Section.SKILLS} <= report.sections_found
    assert report.has_email
    assert report.has_phone


def test_clean_resume_pdf_end_to_end() -> None:
    report = audit(extract_document(build_pdf(text_lines(CLEAN_RESUME_LINES))))
    assert report.issues == ()
    assert report.score == 1.0


def test_clean_resume_docx_end_to_end() -> None:
    d = new_docx()
    for line in CLEAN_RESUME_LINES:
        d.add_paragraph(line)
    report = audit(extract_document(docx_bytes(d)))
    assert report.issues == ()


# ── Individual checks ────────────────────────────────────────────────────


def test_empty_document() -> None:
    assert IssueCode.EMPTY_DOCUMENT in codes(pdf_doc(["Name"]))


def test_scanned_page_without_text_layer() -> None:
    doc = pdf_doc(CLEAN_RESUME_LINES, pages=(page(1), page(2, char_count=0, image_count=1)))
    report = audit(doc)
    (issue,) = [i for i in report.issues if i.code is IssueCode.NO_TEXT_LAYER]
    assert issue.severity is Severity.CRITICAL
    assert issue.details == {"pages": "2"}


def test_page_with_few_chars_but_no_image_is_not_scanned() -> None:
    doc = pdf_doc(CLEAN_RESUME_LINES, pages=(page(1), page(2, char_count=5)))
    assert IssueCode.NO_TEXT_LAYER not in codes(doc)


@pytest.mark.parametrize(
    ("bad_line", "reason_fragment"),
    [
        ("(cid:12)(cid:34)(cid:56)", "cid"),
        ("ผู\uf70bจัดการ", "legacy-encoded"),
        ("ประสบ\ufffdการณ์", "replacement"),
        (" ".join("ประสบการณ์ทำงานด้านการพัฒนาซอฟต์แวร์"), "spaces"),
        ("\x00\x00 Engineer", "control"),
        ("ผ\u0e49\u0e39จัดการ " * 5, "invalid"),  # tone mark typed before the vowel
    ],
)
def test_thai_corruption_reasons(bad_line: str, reason_fragment: str) -> None:
    report = audit(pdf_doc([*CLEAN_RESUME_LINES, bad_line]))
    (issue,) = [i for i in report.issues if i.code is IssueCode.GARBLED_TEXT]
    assert reason_fragment in issue.details["reason"]


def test_hidden_text_threshold() -> None:
    below = pdf_doc([*CLEAN_RESUME_LINES[:-1], TextLine("kubernetes", page=1, hidden_chars=9)])
    above = pdf_doc([*CLEAN_RESUME_LINES, TextLine("kubernetes aws", page=1, hidden_chars=13)])
    assert IssueCode.HIDDEN_TEXT not in codes(below)
    assert IssueCode.HIDDEN_TEXT in codes(above)


BODY_WITHOUT_CONTACT = [TextLine(t) for t in CLEAN_RESUME_LINES if "@" not in t]


def test_contact_only_in_header_is_critical() -> None:
    doc = docx_doc([*BODY_WITHOUT_CONTACT, TextLine("somchai@example.com", BlockKind.HEADER)])
    report = audit(doc)
    assert IssueCode.CONTACT_IN_HEADER_FOOTER in {
        i.code for i in report.by_severity(Severity.CRITICAL)
    }


def test_other_header_text_is_a_warning() -> None:
    doc = docx_doc(
        [*map(TextLine, CLEAN_RESUME_LINES), TextLine("Curriculum Vitae", BlockKind.HEADER)]
    )
    report = audit(doc)
    assert IssueCode.TEXT_IN_HEADER_FOOTER in codes(doc)
    assert IssueCode.CONTACT_IN_HEADER_FOOTER not in codes(doc)
    assert report.score == pytest.approx(1 - PENALTY[Severity.WARNING])


@pytest.mark.parametrize("footer", ["Page 1", "page 2 of 3", "หน้า 1", "1/2", " 3 "])
def test_page_numbers_in_footer_are_ignored(footer: str) -> None:
    doc = docx_doc([*map(TextLine, CLEAN_RESUME_LINES), TextLine(footer, BlockKind.FOOTER)])
    assert codes(doc) == set()


def test_text_boxes() -> None:
    doc = docx_doc([*map(TextLine, CLEAN_RESUME_LINES), TextLine("Sidebar", BlockKind.TEXTBOX)])
    assert IssueCode.TEXT_IN_TEXTBOX in codes(doc)


def test_multi_column_pdf_page() -> None:
    doc = pdf_doc(CLEAN_RESUME_LINES, pages=(page(1), page(2, multi_column=True)))
    (issue,) = [i for i in audit(doc).issues if i.code is IssueCode.MULTI_COLUMN_LAYOUT]
    assert issue.details == {"where": "page 2"}


def test_multi_column_docx_section() -> None:
    doc = docx_doc(list(map(TextLine, CLEAN_RESUME_LINES)), max_section_columns=2)
    (issue,) = [i for i in audit(doc).issues if i.code is IssueCode.MULTI_COLUMN_LAYOUT]
    assert issue.details == {"where": "2 columns"}


def test_tables() -> None:
    assert IssueCode.TABLES_USED in codes(pdf_doc(CLEAN_RESUME_LINES, table_count=2))


def test_tiny_font_share() -> None:
    normal = [TextLine(t, page=1, font_size=11) for t in CLEAN_RESUME_LINES]
    tiny = [TextLine("x" * 200, page=1, font_size=6)]
    assert IssueCode.TINY_FONT not in codes(pdf_doc(normal))
    report = audit(pdf_doc(normal + tiny))
    (issue,) = [i for i in report.issues if i.code is IssueCode.TINY_FONT]
    assert issue.details["size"] == "8"


def test_hidden_tiny_text_is_reported_as_hidden_not_tiny() -> None:
    normal = [TextLine(t, page=1, font_size=11) for t in CLEAN_RESUME_LINES]
    stuffed = [TextLine("x" * 300, page=1, font_size=1, hidden_chars=300)]
    found = codes(pdf_doc(normal + stuffed))
    assert IssueCode.HIDDEN_TEXT in found
    assert IssueCode.TINY_FONT not in found


@pytest.mark.parametrize(
    ("remove", "missing"),
    [("@", "e-mail"), ("081", "phone")],
)
def test_missing_contact(remove: str, missing: str) -> None:
    lines = [
        line.replace("somchai@example.com", "") if remove == "@" else line
        for line in CLEAN_RESUME_LINES
    ]
    lines = [line.replace("081-234-5678", "") if remove == "081" else line for line in lines]
    (issue,) = [i for i in audit(pdf_doc(lines)).issues if i.code is IssueCode.MISSING_CONTACT]
    assert issue.details == {"missing": missing}


def test_missing_sections() -> None:
    lines = [line for line in CLEAN_RESUME_LINES if line not in ("Education", "Skills")]
    (issue,) = [i for i in audit(pdf_doc(lines)).issues if i.code is IssueCode.MISSING_SECTIONS]
    assert issue.details == {"sections": "education, skills"}


def test_thai_section_headings_count() -> None:
    lines = [
        line.replace("Experience", "ประสบการณ์ทำงาน")
        .replace("Education", "ประวัติการศึกษา")
        .replace("Skills", "ทักษะ")
        if line in ("Experience", "Education", "Skills")
        else line
        for line in CLEAN_RESUME_LINES
    ]
    assert IssueCode.MISSING_SECTIONS not in codes(pdf_doc(lines))


def test_buddhist_era_years() -> None:
    lines = [*CLEAN_RESUME_LINES[:5], "วิศวกรซอฟต์แวร์ 2563 - ปัจจุบัน", *CLEAN_RESUME_LINES[9:]]
    lines = [line for line in lines if "20" not in line or "@" in line]
    report = audit(pdf_doc(lines))
    (issue,) = [i for i in report.issues if i.code is IssueCode.BUDDHIST_ERA_YEARS]
    assert issue.details == {"example": "2563"}


def test_mixed_era_years() -> None:
    lines = [*CLEAN_RESUME_LINES, "Internship 2560 - 2561"]
    found = codes(pdf_doc(lines))
    assert IssueCode.MIXED_ERA_YEARS in found
    assert IssueCode.BUDDHIST_ERA_YEARS not in found


def test_images_and_length_are_informational() -> None:
    pages = tuple(page(n) for n in range(1, 5))
    report = audit(pdf_doc(CLEAN_RESUME_LINES, pages=pages, image_count=1))
    assert {i.code for i in report.by_severity(Severity.INFO)} == {
        IssueCode.IMAGES_PRESENT,
        IssueCode.TOO_MANY_PAGES,
    }
    assert report.score == pytest.approx(1 - 2 * PENALTY[Severity.INFO])


# ── Ordering, scoring, messages ─────────────────────────────────────────


def test_issues_are_sorted_by_severity_and_score_floors_at_zero() -> None:
    doc = pdf_doc(
        ["(cid:1)(cid:2)"],
        pages=(page(1, char_count=0, image_count=1, multi_column=True),),
        table_count=1,
        image_count=1,
    )
    report = audit(doc)
    order = [i.severity for i in report.issues]
    rank = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
    assert order == sorted(order, key=rank.__getitem__)
    assert report.score == 0.0


def _fields(texts: Sequence[str]) -> set[str]:
    return {name for text in texts for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_issue_code_has_messages_in_both_languages() -> None:
    assert set(MESSAGES) == set(IssueCode)
    for code, by_language in MESSAGES.items():
        assert set(by_language) == {"th", "en"}, code
        # Both languages must use exactly the same placeholders.
        assert _fields(by_language["en"]) == _fields(by_language["th"]), code


def test_render_issue_fills_placeholders() -> None:
    issue = Issue(IssueCode.NO_TEXT_LAYER, Severity.CRITICAL, {"pages": "2, 3"})
    problem_th, fix_th = render_issue(issue, "th")
    problem_en, _ = render_issue(issue, "en")
    assert "หน้า 2, 3" in problem_th
    assert "Page(s) 2, 3" in problem_en
    assert fix_th


# ── End-to-end on generated files ───────────────────────────────────────


def test_problem_pdf_end_to_end() -> None:
    sidebar = text_lines([f"Skill {i}" for i in range(20)], x=40)
    main = text_lines([f"Experience detail line number {i}" for i in range(25)], x=230)
    data = build_pdf(
        [
            *sidebar,
            *main,
            Text(72, 720, "kubernetes terraform aws gcp", color=(1, 1, 1)),
            Grid(xs=(72, 250, 450), tops=(600, 630, 660)),
            Picture(450, 40, 60, 60),
        ]
    )
    found = codes(extract_document(data))
    assert {
        IssueCode.MULTI_COLUMN_LAYOUT,
        IssueCode.HIDDEN_TEXT,
        IssueCode.TABLES_USED,
        IssueCode.IMAGES_PRESENT,
        IssueCode.MISSING_CONTACT,
        IssueCode.MISSING_SECTIONS,
    } <= found


def test_thai_resume_pdf_end_to_end() -> None:
    thai_lines = [
        "สมชาย ตัวอย่าง",
        "somchai@example.com 081-234-5678",
        "ประสบการณ์ทำงาน",
        "วิศวกรซอฟต์แวร์ บริษัท ตัวอย่าง จำกัด มิ.ย. 2563 - ปัจจุบัน",
        "พัฒนาระบบหลังบ้านด้วยภาษาไพธอนและฐานข้อมูลโพสต์เกรส",
        "ประวัติการศึกษา",
        "วิศวกรรมศาสตรบัณฑิต สาขาวิศวกรรมคอมพิวเตอร์ 2559 - 2563",
        "ทักษะ",
        "การพัฒนาซอฟต์แวร์ การออกแบบระบบ การทำงานเป็นทีม การสื่อสาร",
    ]
    items = [
        item for i, line in enumerate(thai_lines) for item in mixed_text(72, 72 + i * 18, line)
    ]
    report = audit(extract_document(build_pdf(items)))
    assert IssueCode.GARBLED_TEXT not in {i.code for i in report.issues}
    assert IssueCode.MISSING_SECTIONS not in {i.code for i in report.issues}
    assert IssueCode.BUDDHIST_ERA_YEARS in {i.code for i in report.issues}


def test_problem_docx_end_to_end() -> None:
    d = new_docx()
    for line in CLEAN_RESUME_LINES[2:]:  # name and contact moved to the header
        d.add_paragraph(line)
    d.sections[0].header.paragraphs[0].text = "Somchai Example | somchai@example.com | 081-234-5678"
    add_textbox(d, "Open to relocation")
    set_columns(d, 2)
    found = codes(extract_document(docx_bytes(d)))
    assert {
        IssueCode.CONTACT_IN_HEADER_FOOTER,
        IssueCode.TEXT_IN_TEXTBOX,
        IssueCode.MULTI_COLUMN_LAYOUT,
    } <= found


def test_icon_font_glyphs_are_not_garbled_text() -> None:
    # Font Awesome phone / envelope icons live in the Private Use Area.
    lines = [line.replace("somchai@", "\uf0e0 somchai@") for line in CLEAN_RESUME_LINES]
    assert IssueCode.GARBLED_TEXT not in codes(pdf_doc(lines))


@pytest.mark.parametrize(
    ("unmapped", "char_count", "garbled"),
    [
        (4, 1500, False),  # a few icon glyphs
        (25, 1500, False),  # under 2 % of the text
        (40, 1500, True),
        (12, 300, True),
    ],
)
def test_unmapped_characters_threshold(unmapped: int, char_count: int, garbled: bool) -> None:
    pages = (
        PageInfo(
            number=1,
            width=612,
            height=792,
            char_count=char_count,
            image_count=0,
            table_count=0,
            multi_column=False,
            unmapped_chars=unmapped,
        ),
    )
    report = audit(pdf_doc(CLEAN_RESUME_LINES, pages=pages))
    found = [i for i in report.issues if i.code is IssueCode.GARBLED_TEXT]
    assert bool(found) is garbled
    if garbled:
        assert "without a Unicode mapping" in found[0].details["reason"]
