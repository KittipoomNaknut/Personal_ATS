"""ATS parseability audit: can an applicant tracking system read this resume?

Each check inspects the :class:`ExtractedDocument` and reports :class:`Issue`
objects with a stable ``code``. Messages are rendered from the bilingual
``MESSAGES`` table so reports can be shown in Thai or English.

Score: starts at 1.0 and loses a fixed penalty per issue severity (floor 0.0).
The penalties are initial values; phase 3 calibrates them against the eval set.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Literal

from ats_core.extract.models import BlockKind, ExtractedDocument, SourceFormat
from ats_core.text.dates import Era, find_date_years
from ats_core.text.patterns import find_emails, find_phones
from ats_core.text.sections import Section, detect_section
from ats_core.text.thai import analyze_thai

__all__ = [
    "MESSAGES",
    "Issue",
    "IssueCode",
    "ParseabilityReport",
    "Severity",
    "audit",
    "render_issue",
]

Language = Literal["th", "en"]


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class IssueCode(StrEnum):
    EMPTY_DOCUMENT = "empty_document"
    NO_TEXT_LAYER = "no_text_layer"
    GARBLED_TEXT = "garbled_text"
    HIDDEN_TEXT = "hidden_text"
    CONTACT_IN_HEADER_FOOTER = "contact_in_header_footer"
    TEXT_IN_HEADER_FOOTER = "text_in_header_footer"
    TEXT_IN_TEXTBOX = "text_in_textbox"
    MULTI_COLUMN_LAYOUT = "multi_column_layout"
    TABLES_USED = "tables_used"
    TINY_FONT = "tiny_font"
    MISSING_CONTACT = "missing_contact"
    MISSING_SECTIONS = "missing_sections"
    BUDDHIST_ERA_YEARS = "buddhist_era_years"
    MIXED_ERA_YEARS = "mixed_era_years"
    IMAGES_PRESENT = "images_present"
    TOO_MANY_PAGES = "too_many_pages"


PENALTY: Final[dict[Severity, float]] = {
    Severity.CRITICAL: 0.35,
    Severity.WARNING: 0.10,
    Severity.INFO: 0.02,
}

MIN_TEXT_CHARS: Final = 200
SCANNED_PAGE_MAX_CHARS: Final = 20
HIDDEN_TEXT_MIN_CHARS: Final = 10
# Icon fonts legitimately produce a few unmapped glyphs; require a real share.
UNMAPPED_MIN_CHARS: Final = 10
UNMAPPED_MIN_SHARE: Final = 0.02
TINY_FONT_PT: Final = 8.0
TINY_FONT_MAX_SHARE: Final = 0.10
MAX_RECOMMENDED_PAGES: Final = 3
REQUIRED_SECTIONS: Final = (Section.EXPERIENCE, Section.EDUCATION, Section.SKILLS)


@dataclass(frozen=True, slots=True)
class Issue:
    code: IssueCode
    severity: Severity
    details: dict[str, str] = field(default_factory=dict)
    """Values interpolated into the message, e.g. affected pages."""


@dataclass(frozen=True, slots=True)
class ParseabilityReport:
    issues: tuple[Issue, ...]
    score: float
    sections_found: frozenset[Section]
    has_email: bool
    has_phone: bool

    def by_severity(self, severity: Severity) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.severity is severity)


# ── Message catalogue ────────────────────────────────────────────────────

MESSAGES: Final[dict[IssueCode, dict[Language, tuple[str, str]]]] = {
    # code: {lang: (problem, how to fix)}
    IssueCode.EMPTY_DOCUMENT: {
        "en": (
            "Almost no text could be extracted.",
            "Export the resume as a text-based PDF or DOCX.",
        ),
        "th": ("ดึงข้อความออกมาได้น้อยมาก", "ส่งออก resume เป็น PDF ที่มีข้อความจริงหรือไฟล์ DOCX"),
    },
    IssueCode.NO_TEXT_LAYER: {
        "en": (
            "Page(s) {pages} look scanned: images without a text layer. ATSs cannot read them.",
            "Recreate the resume in Word/Google Docs and export to PDF instead of scanning.",
        ),
        "th": (
            "หน้า {pages} เป็นภาพสแกน ไม่มีชั้นข้อความ ATS อ่านไม่ได้",
            "สร้าง resume ใหม่ใน Word/Google Docs แล้วส่งออกเป็น PDF แทนการสแกน",
        ),
    },
    IssueCode.GARBLED_TEXT: {
        "en": (
            "Text is garbled after extraction ({reason}). An ATS will see the same broken text.",
            "Use standard Unicode fonts (for Thai e.g. TH Sarabun New, Noto Sans Thai) and "
            "re-export the PDF from Word/Google Docs.",
        ),
        "th": (
            "ข้อความเพี้ยนหลังดึงข้อความ ({reason}) ATS จะเห็นข้อความเพี้ยนแบบเดียวกัน",
            "ใช้ฟอนต์แบบ Unicode (ภาษาไทยเช่น TH Sarabun New, Noto Sans Thai) "
            "แล้วส่งออก PDF ใหม่จาก Word/Google Docs",
        ),
    },
    IssueCode.HIDDEN_TEXT: {
        "en": (
            "{chars} characters are invisible (white or tiny text). This looks like keyword "
            "stuffing and is penalised by ATSs and recruiters.",
            "Remove hidden text; list real skills visibly instead.",
        ),
        "th": (
            "มีตัวอักษรที่มองไม่เห็น {chars} ตัว (สีขาวหรือเล็กมาก) ดูเหมือนการยัด keyword "
            "ซึ่ง ATS และ recruiter มองเป็นข้อเสีย",
            "ลบข้อความที่ซ่อนไว้ และเขียน skill จริงให้มองเห็นชัดเจน",
        ),
    },
    IssueCode.CONTACT_IN_HEADER_FOOTER: {
        "en": (
            "Contact details are only in the page header/footer, which many ATSs ignore.",
            "Move name, e-mail and phone into the main body at the top of the page.",
        ),
        "th": (
            "ข้อมูลติดต่ออยู่ใน header/footer เท่านั้น ซึ่ง ATS หลายระบบไม่อ่าน",
            "ย้ายชื่อ อีเมล และเบอร์โทรมาไว้ในเนื้อหาหลักด้านบนของหน้า",
        ),
    },
    IssueCode.TEXT_IN_HEADER_FOOTER: {
        "en": (
            "{lines} line(s) of text are in the header/footer and may be skipped.",
            "Keep important content in the main body.",
        ),
        "th": (
            "มีข้อความ {lines} บรรทัดอยู่ใน header/footer และอาจถูกข้าม",
            "เก็บเนื้อหาสำคัญไว้ในเนื้อหาหลัก",
        ),
    },
    IssueCode.TEXT_IN_TEXTBOX: {
        "en": (
            "{lines} line(s) are inside text boxes, which many ATSs cannot read.",
            "Replace text boxes with normal paragraphs.",
        ),
        "th": (
            "มี {lines} บรรทัดอยู่ใน text box ซึ่ง ATS หลายระบบอ่านไม่ได้",
            "เปลี่ยน text box เป็นย่อหน้าปกติ",
        ),
    },
    IssueCode.MULTI_COLUMN_LAYOUT: {
        "en": (
            "Multi-column layout detected ({where}). ATSs read across columns and mix the content.",
            "Use a single-column layout.",
        ),
        "th": (
            "พบการจัดหน้าแบบหลายคอลัมน์ ({where}) ATS จะอ่านข้ามคอลัมน์ทำให้เนื้อหาปนกัน",
            "ใช้การจัดหน้าแบบคอลัมน์เดียว",
        ),
    },
    IssueCode.TABLES_USED: {
        "en": (
            "{count} table(s) found. Table cells are often read out of order.",
            "Use plain headings and bullet points instead of tables.",
        ),
        "th": (
            "พบตาราง {count} ตาราง ATS มักอ่านช่องในตารางผิดลำดับ",
            "ใช้หัวข้อและ bullet point แทนตาราง",
        ),
    },
    IssueCode.TINY_FONT: {
        "en": (
            "{share}% of the text is smaller than {size} pt.",
            "Use at least 10 pt for body text.",
        ),
        "th": (
            "ข้อความ {share}% มีขนาดเล็กกว่า {size} pt",
            "ใช้ขนาดตัวอักษรอย่างน้อย 10 pt สำหรับเนื้อหา",
        ),
    },
    IssueCode.MISSING_CONTACT: {
        "en": ("No {missing} found in the resume body.", "Add them at the top of the first page."),
        "th": ("ไม่พบ {missing} ในเนื้อหา resume", "ใส่ไว้ด้านบนของหน้าแรก"),
    },
    IssueCode.MISSING_SECTIONS: {
        "en": (
            "Standard section heading(s) not found: {sections}. "
            "ATSs rely on them to split the resume.",
            "Use standard headings such as Experience, Education, Skills.",
        ),
        "th": (
            "ไม่พบหัวข้อมาตรฐาน: {sections} ATS ใช้หัวข้อเหล่านี้แบ่งส่วนของ resume",
            "ใช้หัวข้อมาตรฐาน เช่น ประสบการณ์ทำงาน การศึกษา ทักษะ",
        ),
    },
    IssueCode.BUDDHIST_ERA_YEARS: {
        "en": (
            "Dates use Buddhist Era years (e.g. {example}). Many ATSs assume Gregorian years and "
            "miscalculate experience.",
            "Write years in Gregorian (A.D.), e.g. 2020 instead of 2563.",
        ),
        "th": (
            "วันที่ใช้ปี พ.ศ. (เช่น {example}) ATS หลายระบบเข้าใจว่าเป็น ค.ศ. ทำให้คำนวณประสบการณ์ผิด",
            "เขียนปีเป็น ค.ศ. เช่น 2020 แทน 2563",
        ),
    },
    IssueCode.MIXED_ERA_YEARS: {
        "en": (
            "Dates mix Buddhist Era and Gregorian years.",
            "Use one calendar consistently, preferably Gregorian.",
        ),
        "th": ("วันที่ใช้ปี พ.ศ. และ ค.ศ. ปนกัน", "ใช้ปีแบบเดียวทั้งฉบับ แนะนำ ค.ศ."),
    },
    IssueCode.IMAGES_PRESENT: {
        "en": (
            "{count} image(s) found. Any text inside images is invisible to ATSs.",
            "Make sure no important information exists only as an image.",
        ),
        "th": (
            "พบรูปภาพ {count} รูป ข้อความในรูปภาพ ATS อ่านไม่ได้",
            "ตรวจว่าไม่มีข้อมูลสำคัญที่อยู่ในรูปภาพเท่านั้น",
        ),
    },
    IssueCode.TOO_MANY_PAGES: {
        "en": ("The resume has {pages} pages.", "Aim for 1-2 pages."),
        "th": ("resume มี {pages} หน้า", "ควรมีความยาว 1-2 หน้า"),
    },
}


def render_issue(issue: Issue, language: Language = "th") -> tuple[str, str]:
    """Return ``(problem, fix)`` text for ``issue`` in ``language``."""
    problem, fix = MESSAGES[issue.code][language]
    return problem.format(**issue.details), fix.format(**issue.details)


# ── Checks ───────────────────────────────────────────────────────────────

_Check = Callable[[ExtractedDocument], Iterable[Issue]]


def _check_text_presence(doc: ExtractedDocument) -> Iterable[Issue]:
    scanned = [
        p.number for p in doc.pages if p.char_count <= SCANNED_PAGE_MAX_CHARS and p.image_count > 0
    ]
    if scanned:
        yield Issue(
            IssueCode.NO_TEXT_LAYER, Severity.CRITICAL, {"pages": ", ".join(map(str, scanned))}
        )
    if len(doc.text.strip()) < MIN_TEXT_CHARS:
        yield Issue(IssueCode.EMPTY_DOCUMENT, Severity.CRITICAL)


def _garbled_reason(doc: ExtractedDocument) -> str | None:
    unmapped = sum(p.unmapped_chars for p in doc.pages)
    total = sum(p.char_count for p in doc.pages)
    if unmapped >= UNMAPPED_MIN_CHARS and unmapped > UNMAPPED_MIN_SHARE * total:
        return f"{unmapped} characters without a Unicode mapping"
    result = analyze_thai(doc.text)
    if not result.is_corrupted:
        return None
    if result.cid_tokens:
        return f"{result.cid_tokens} unmapped glyphs (cid)"
    if result.private_use_chars:
        return f"{result.private_use_chars} legacy-encoded Thai characters"
    if result.replacement_chars:
        return f"{result.replacement_chars} replacement characters"
    if result.control_chars:
        return f"{result.control_chars} unmapped glyphs (control characters)"
    if result.spaced_char_ratio > 0.3:
        return "Thai characters separated by spaces"
    return f"{result.invalid_sequences} invalid Thai vowel/tone sequences"


def _check_garbled_text(doc: ExtractedDocument) -> Iterable[Issue]:
    reason = _garbled_reason(doc)
    if reason is not None:
        yield Issue(IssueCode.GARBLED_TEXT, Severity.CRITICAL, {"reason": reason})


def _check_hidden_text(doc: ExtractedDocument) -> Iterable[Issue]:
    hidden = sum(line.hidden_chars for line in doc.lines)
    if hidden >= HIDDEN_TEXT_MIN_CHARS:
        yield Issue(IssueCode.HIDDEN_TEXT, Severity.CRITICAL, {"chars": str(hidden)})


def _has_contact(text: str) -> bool:
    return bool(find_emails(text) or find_phones(text))


_PAGE_NUMBER: Final = re.compile(r"^\s*(?:page|หน้า|p\.)?\s*\d+\s*(?:(?:of|/|จาก)\s*\d+)?\s*$", re.I)


def _check_header_footer(doc: ExtractedDocument) -> Iterable[Issue]:
    edge_lines = [
        line
        for line in doc.lines_of(BlockKind.HEADER, BlockKind.FOOTER)
        if not _PAGE_NUMBER.match(line.text)
    ]
    if not edge_lines:
        return
    edge_text = "\n".join(line.text for line in edge_lines)
    body_text = "\n".join(line.text for line in doc.lines_of(BlockKind.BODY, BlockKind.TABLE))
    if _has_contact(edge_text) and not _has_contact(body_text):
        yield Issue(IssueCode.CONTACT_IN_HEADER_FOOTER, Severity.CRITICAL)
    else:
        yield Issue(
            IssueCode.TEXT_IN_HEADER_FOOTER, Severity.WARNING, {"lines": str(len(edge_lines))}
        )


def _check_textboxes(doc: ExtractedDocument) -> Iterable[Issue]:
    boxes = doc.lines_of(BlockKind.TEXTBOX)
    if boxes:
        yield Issue(IssueCode.TEXT_IN_TEXTBOX, Severity.WARNING, {"lines": str(len(boxes))})


def _check_columns(doc: ExtractedDocument) -> Iterable[Issue]:
    if doc.source_format is SourceFormat.PDF:
        pages = [p.number for p in doc.pages if p.multi_column]
        if pages:
            where = "page " + ", ".join(map(str, pages))
            yield Issue(IssueCode.MULTI_COLUMN_LAYOUT, Severity.WARNING, {"where": where})
    elif doc.max_section_columns > 1:
        where = f"{doc.max_section_columns} columns"
        yield Issue(IssueCode.MULTI_COLUMN_LAYOUT, Severity.WARNING, {"where": where})


def _check_tables(doc: ExtractedDocument) -> Iterable[Issue]:
    if doc.table_count:
        yield Issue(IssueCode.TABLES_USED, Severity.WARNING, {"count": str(doc.table_count)})


def _check_font_size(doc: ExtractedDocument) -> Iterable[Issue]:
    sized = [line for line in doc.lines if line.font_size is not None and line.hidden_chars == 0]
    total = sum(len(line.text) for line in sized)
    if not total:
        return
    tiny = sum(
        len(line.text)
        for line in sized
        if line.font_size is not None and line.font_size < TINY_FONT_PT
    )
    share = tiny / total
    if share > TINY_FONT_MAX_SHARE:
        yield Issue(
            IssueCode.TINY_FONT,
            Severity.WARNING,
            {"share": f"{share * 100:.0f}", "size": f"{TINY_FONT_PT:g}"},
        )


def _check_contact(doc: ExtractedDocument) -> Iterable[Issue]:
    text = doc.text
    missing = []
    if not find_emails(text):
        missing.append("e-mail")
    if not find_phones(text):
        missing.append("phone")
    if missing:
        yield Issue(IssueCode.MISSING_CONTACT, Severity.WARNING, {"missing": " / ".join(missing)})


def _sections(doc: ExtractedDocument) -> frozenset[Section]:
    found = (detect_section(line.text) for line in doc.lines)
    return frozenset(s for s in found if s is not None)


def _check_sections(doc: ExtractedDocument) -> Iterable[Issue]:
    found = _sections(doc)
    missing = [s.value for s in REQUIRED_SECTIONS if s not in found]
    if missing:
        yield Issue(IssueCode.MISSING_SECTIONS, Severity.WARNING, {"sections": ", ".join(missing)})


def _check_eras(doc: ExtractedDocument) -> Iterable[Issue]:
    years = find_date_years(doc.text)
    buddhist = [y.year for y in years if y.era is Era.BUDDHIST]
    gregorian = [y.year for y in years if y.era is Era.GREGORIAN]
    if buddhist and gregorian:
        yield Issue(IssueCode.MIXED_ERA_YEARS, Severity.WARNING)
    elif buddhist:
        yield Issue(IssueCode.BUDDHIST_ERA_YEARS, Severity.WARNING, {"example": str(buddhist[0])})


def _check_images(doc: ExtractedDocument) -> Iterable[Issue]:
    if doc.image_count:
        yield Issue(IssueCode.IMAGES_PRESENT, Severity.INFO, {"count": str(doc.image_count)})


def _check_length(doc: ExtractedDocument) -> Iterable[Issue]:
    if doc.page_count > MAX_RECOMMENDED_PAGES:
        yield Issue(IssueCode.TOO_MANY_PAGES, Severity.INFO, {"pages": str(doc.page_count)})


_CHECKS: Final[tuple[_Check, ...]] = (
    _check_text_presence,
    _check_garbled_text,
    _check_hidden_text,
    _check_header_footer,
    _check_textboxes,
    _check_columns,
    _check_tables,
    _check_font_size,
    _check_contact,
    _check_sections,
    _check_eras,
    _check_images,
    _check_length,
)

_SEVERITY_ORDER: Final = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


def audit(doc: ExtractedDocument) -> ParseabilityReport:
    """Run every parseability check on ``doc``."""
    issues = sorted(
        (issue for check in _CHECKS for issue in check(doc)),
        key=lambda i: _SEVERITY_ORDER[i.severity],
    )
    score = max(0.0, 1.0 - sum(PENALTY[i.severity] for i in issues))
    text = doc.text
    return ParseabilityReport(
        issues=tuple(issues),
        score=round(score, 2),
        sections_found=_sections(doc),
        has_email=bool(find_emails(text)),
        has_phone=bool(find_phones(text)),
    )
