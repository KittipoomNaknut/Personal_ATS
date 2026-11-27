"""Personal-data redaction for resume text before it leaves the machine.

Policy (see docs/ARCHITECTURE.md §2.4):

* **Always redacted** - direct identifiers: names, e-mail, phone numbers, Thai
  national ID, street addresses, personal profile URLs and messaging IDs.
* **Date of birth** - the date is removed but an approximate age is kept in the
  placeholder (``[DATE_OF_BIRTH_1 age 31]``) because Thai job ads often state an
  age range.
* **Sensitive attributes** - religion and marital status are removed; they are
  never a legitimate basis for matching.
* **Kept** - city/province, gender, nationality, military-service status and
  everything else, because they can be real job requirements and do not identify
  a person on their own.

Each distinct original value maps to one stable placeholder (``[EMAIL_1]``). The
mapping stays local in :class:`RedactionResult`, so model output can be restored
for display without the model ever seeing the values.

Detection favours recall for identifiers, but labels must start a segment and
English labels need a word boundary, so ordinary resume wording ("Company Name:",
"Addressed customer issues", "Product line: Mobile") is not redacted.
:func:`find_residual_pii` re-scans redacted text so callers can fail closed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

from ats_core.text.patterns import Span, email_spans, phone_spans
from ats_core.text.sections import detect_section

__all__ = [
    "Entity",
    "PiiKind",
    "RedactionResult",
    "find_residual_pii",
    "redact_pii",
]


class PiiKind(StrEnum):
    NAME = "NAME"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    NATIONAL_ID = "NATIONAL_ID"
    ADDRESS = "ADDRESS"
    PROFILE_URL = "PROFILE_URL"
    MESSAGING_ID = "MESSAGING_ID"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    RELIGION = "RELIGION"
    MARITAL_STATUS = "MARITAL_STATUS"


@dataclass(frozen=True, slots=True)
class Entity:
    kind: PiiKind
    original: str
    placeholder: str
    start: int
    end: int
    """Position of ``original`` in the input text."""


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    entities: tuple[Entity, ...]

    @property
    def mapping(self) -> dict[str, str]:
        """Placeholder to original value."""
        return {e.placeholder: e.original for e in self.entities}

    def restore(self, text: str) -> str:
        """Put original values back into ``text`` (e.g. model output). Local use only."""
        mapping = self.mapping
        for placeholder in sorted(mapping, key=len, reverse=True):
            text = text.replace(placeholder, mapping[placeholder])
        return text

    def count(self, kind: PiiKind) -> int:
        return sum(1 for e in self.entities if e.kind is kind)


# ── Shared pattern pieces ────────────────────────────────────────────────

_THAI_LETTERS: Final = r"ก-๎"
_SEGMENT_SPLIT: Final = re.compile(r"\s*[|\u2022\u00b7]\s*|\t+| {3,}")
_SEGMENT_LEAD: Final = re.compile(r"[\s\-\u2022*\d.)]*")  # bullets / numbering
_EN_END: Final = r"(?![A-Za-z])"
_OPTIONAL_SEPARATOR_VALUE: Final = r"\s*(?:[:\uff1a=]|-(?=\s))?\s*(?P<value>\S.*)"
_COLON_VALUE: Final = r"\s*[:\uff1a]\s*(?P<value>\S.*)"


def _label(alternatives: str, value: str) -> re.Pattern[str]:
    return re.compile(f"(?:{alternatives}){value}", re.IGNORECASE)


# ── Labelled fields (label must start the segment) ───────────────────────

_NAME_LABEL: Final = _label(
    rf"ชื่อ\s*[-\u2013]?\s*(?:นาม)?สกุล|ชื่อและนามสกุล|ชื่อจริง|ชื่อ|(?:full\s+)?name{_EN_END}",
    _COLON_VALUE,
)
_DOB_LABEL: Final = _label(
    r"วัน\s*/?\s*เดือน\s*/?\s*ปี\s*เกิด|วันเดือนปีเกิด|วันเกิด|เกิดวันที่"
    rf"|(?:date\s+of\s+birth|birth\s*date|birthday|d\.?\s?o\.?\s?b\.?|born){_EN_END}",
    _OPTIONAL_SEPARATOR_VALUE,
)
_RELIGION_LABEL: Final = _label(rf"ศาสนา|religion{_EN_END}", _OPTIONAL_SEPARATOR_VALUE)
_MARITAL_LABEL: Final = _label(
    rf"สถานภาพ(?:สมรส|การสมรส)?|สถานะ(?:การ)?สมรส|marital\s+status{_EN_END}",
    _OPTIONAL_SEPARATOR_VALUE,
)
_ADDRESS_LABEL: Final = _label(
    rf"ที่อยู่(?:ปัจจุบัน|ตามทะเบียนบ้าน|ที่ติดต่อได้)?|(?:home\s+|current\s+)?address{_EN_END}",
    _OPTIONAL_SEPARATOR_VALUE,
)
# A bare "line" needs a colon ("Product line management" is not an ID);
# an explicit "Line ID" does not.
_MESSAGING_LABEL: Final = _label(
    r"line|ไลน์|skype|wechat|telegram|whatsapp", rf"(?:\s*id)?{_COLON_VALUE}"
)
_MESSAGING_ID_LABEL: Final = _label(
    r"(?:line|skype|wechat|telegram|whatsapp)\s*id|ไอดีไลน์", _OPTIONAL_SEPARATOR_VALUE
)

# ── Free-standing identifiers ────────────────────────────────────────────

_THAI_ID: Final = re.compile(r"(?<!\d)\d(?:[\s-]?\d){12}(?!\d)")
_THAI_ID_LABEL: Final = re.compile(
    r"เลข(?:ที่)?บัตร(?:ประจำตัว)?ประชาชน|เลขประจำตัวประชาชน|บัตรประชาชน"
    r"|national\s*id|citizen\s*id|id\s*card|id\s*no\.?",
    re.IGNORECASE,
)
_PROFILE_URL: Final = re.compile(
    r"(?:https?://)?(?:(?:www|m|mobile|th)\.)?"
    r"(?:linkedin\.com/(?:in|pub)/|github\.com/|gitlab\.com/|bitbucket\.org/"
    r"|facebook\.com/|fb\.com/|instagram\.com/|twitter\.com/|x\.com/|tiktok\.com/@"
    r"|medium\.com/@|behance\.net/|dribbble\.com/|t\.me/|line\.me/ti/p/)"
    r"[^\s|,;)\]>]+",
    re.IGNORECASE,
)

# "somchai [at] example [dot] com" and similar spam-avoiding spellings.
_OBFUSCATED_EMAIL: Final = re.compile(
    r"[A-Za-z0-9._%+-]+\s*[\[(]?\s*(?:at|@)\s*[\])]?\s*[A-Za-z0-9-]+"
    r"(?:\s*[\[(]?\s*(?:dot|\.)\s*[\])]?\s*[A-Za-z]{2,})+",
    re.IGNORECASE,
)

# ── Addresses ────────────────────────────────────────────────────────────

_STREET_MARKERS: Final = re.compile(
    r"บ้านเลขที่|เลขที่|หมู่(?:ที่|บ้าน)?|ม\.\s?\d|ซอย|ซ\.|ถนน|ถ\.|ตรอก"
    rf"|(?<![A-Za-z])(?:road|rd\.|street|st\.|soi|moo|lane|avenue|ave\.){_EN_END}"
    # House numbers such as 123/45, but not dates such as 06/2020 or 15/06/2538.
    r"|(?<![\d/])\d{1,4}/\d{1,3}(?![\d/])",
    re.IGNORECASE,
)
_AREA_MARKERS: Final = re.compile(
    r"แขวง|ตำบล|ต\.|เขต|อำเภอ|อ\.|จังหวัด|จ\.|กรุงเทพ"
    rf"|(?<![A-Za-z])(?:sub-?district|district|province|tambon|amphoe|bangkok){_EN_END}",
    re.IGNORECASE,
)
_POSTAL_CODE: Final = re.compile(r"(?<!\d)[1-9]\d{4}(?!\d)")

# ── Names ────────────────────────────────────────────────────────────────

_THAI_HONORIFIC: Final = r"(?:นางสาว|นาง|นาย|น\.ส\.|ดร\.|ว่าที่\s?ร\.ต\.)"
_EN_HONORIFICS: Final = r"(?:Mr|Mrs|Ms|Miss|Dr)"
_THAI_NAME_WORD: Final = rf"[{_THAI_LETTERS}]{{2,}}"
_EN_NAME_WORD: Final = r"[A-Z][a-zA-Z'\-]+"
_ANY_NAME_WORD: Final = rf"(?:{_THAI_NAME_WORD}|{_EN_NAME_WORD})"
# Thai honorifics attach to the name ("นายสมชาย"), so they only count at the start
# of a segment (optionally after a "ชื่อ:" label) and must be followed by a first
# name and surname. Common words that merely start with an honorific (นายจ้าง
# "employer", นางพยาบาล "nurse") are excluded explicitly.
_HONORIFIC_COMPOUNDS: Final = (
    "จ้าง|หน้า|ทุน|ช่าง|ตรวจ|ประกัน|สถานี|ท่า|อำเภอ|ร้อย|พัน|พล|ทหาร|ตำรวจ|ด่าน|ห้าง"
    "|กรัฐมนตรี|กเทศมนตรี|กสมาคม|กองค์การ|แบบ|พยาบาล|งาม|ฟ้า|เอก|กำนัล|สนม"
)
_THAI_NAME_PART: Final = rf"[{_THAI_LETTERS}]{{2,20}}"
_THAI_HONORIFIC_NAME: Final = re.compile(
    r"^[\s\-\u2022*\d.)]*(?:ชื่อ(?:\s*[-\u2013]?\s*(?:นาม)?สกุล)?\s*[:\uff1a]?\s*)?"
    rf"{_THAI_HONORIFIC}\s?(?!(?:{_HONORIFIC_COMPOUNDS}))"
    rf"(?P<name>{_THAI_NAME_PART}\s{_THAI_NAME_PART})(?![{_THAI_LETTERS}])"
)
# English titles need a period or a full name, so "Ms Excel" is not a person.
_EN_HONORIFIC_NAME: Final = re.compile(
    rf"(?<![A-Za-z])(?:{_EN_HONORIFICS}\.\s*(?P<name>{_EN_NAME_WORD}(?:\s{_EN_NAME_WORD}){{0,2}})"
    rf"|{_EN_HONORIFICS}\s+(?P<name2>{_EN_NAME_WORD}(?:\s{_EN_NAME_WORD}){{1,2}}))"
)
_HEADER_NAME_LINE: Final = re.compile(
    rf"^(?:{_THAI_HONORIFIC}\s?(?!(?:{_HONORIFIC_COMPOUNDS}))|{_EN_HONORIFICS}\.?\s+)?"
    rf"(?P<name>{_ANY_NAME_WORD}(?:\s{_ANY_NAME_WORD}){{1,3}})$"
)
_MAX_HEADER_NAME_LENGTH: Final = 50
_HEADER_SEARCH_LINES: Final = 3
_MIN_THAI_NAME_WORD: Final = 3
_MIN_EN_NAME_WORD: Final = 2
# Words that make a short line a title rather than a person's name. Name words
# in these lists are also never redacted on their own. Thai compounds words
# without spaces ("วิศวกรซอฟต์แวร์"), so Thai entries match inside a word.
_NOT_NAME_WORDS: Final = frozenset(
    {
        "resume", "curriculum", "vitae", "cv", "profile", "portfolio", "summary",
        "software", "senior", "junior", "lead", "principal", "staff", "chief", "head",
        "engineer", "developer", "programmer", "architect", "analyst", "scientist",
        "designer", "manager", "director", "consultant", "specialist", "officer",
        "intern", "assistant", "associate", "executive", "administrator", "coordinator",
        "data", "web", "backend", "frontend", "full", "stack", "devops", "cloud",
        "marketing", "sales", "product", "project", "business", "finance", "hr",
        "nurse", "teacher", "accountant", "technician",
    }
)  # fmt: skip
_NOT_NAME_THAI_PARTS: Final = (
    "ประวัติ", "เรซูเม่", "วิศวกร", "นักพัฒนา", "นักวิเคราะห์", "นักออกแบบ", "นักบัญชี",
    "นักการตลาด", "ผู้จัดการ", "ผู้ช่วย", "ผู้อำนวยการ", "เจ้าหน้าที่", "พนักงาน", "พยาบาล",
    "ช่าง", "ครู", "อาจารย์", "แพทย์", "เภสัช", "ซอฟต์แวร์", "อาวุโส", "บริษัท", "โรงพยาบาล",
)  # fmt: skip


def _is_not_name_word(word: str) -> bool:
    return word.casefold() in _NOT_NAME_WORDS or any(part in word for part in _NOT_NAME_THAI_PARTS)


_YEAR_IN_VALUE: Final = re.compile(r"(?<!\d)(?:19|20|24|25)\d{2}(?!\d)")
_BUDDHIST_ERA_OFFSET: Final = 543


# ── Candidate collection ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Candidate:
    start: int
    end: int
    kind: PiiKind
    age: int | None = None


def _segments(text: str) -> Iterator[Span]:
    """Yield ``(start, end)`` of each separator-delimited segment of each line."""
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        cursor = 0
        for match in _SEGMENT_SPLIT.finditer(content):
            yield offset + cursor, offset + match.start()
            cursor = match.end()
        yield offset + cursor, offset + len(content)
        offset += len(line)


def _label_match(pattern: re.Pattern[str], segment: str) -> re.Match[str] | None:
    """Match ``pattern`` at the start of ``segment`` (after bullets / numbering)."""
    lead = _SEGMENT_LEAD.match(segment)
    return pattern.match(segment, lead.end() if lead else 0)


def _labelled(text: str, pattern: re.Pattern[str], kind: PiiKind) -> Iterator[_Candidate]:
    for start, end in _segments(text):
        segment = text[start:end]
        match = _label_match(pattern, segment)
        if match and match.group("value").strip():
            yield _Candidate(start + match.start("value"), start + len(segment.rstrip()), kind)


def _thai_id_is_valid(digits: str) -> bool:
    total = sum(int(d) * (13 - i) for i, d in enumerate(digits[:12]))
    return (11 - total % 11) % 10 == int(digits[12])


def _national_ids(text: str) -> Iterator[_Candidate]:
    for match in _THAI_ID.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        labelled = _THAI_ID_LABEL.search(text[max(0, match.start() - 40) : match.start()])
        if _thai_id_is_valid(digits) or labelled:
            yield _Candidate(match.start(), match.end(), PiiKind.NATIONAL_ID)


def _age_from(value: str, today: date) -> int | None:
    match = _YEAR_IN_VALUE.search(value)
    if not match:
        return None
    year = int(match.group())
    if year >= 2400:
        year -= _BUDDHIST_ERA_OFFSET
    age = today.year - year
    return age if 0 < age < 100 else None


def _dates_of_birth(text: str, today: date) -> Iterator[_Candidate]:
    for candidate in _labelled(text, _DOB_LABEL, PiiKind.DATE_OF_BIRTH):
        value = text[candidate.start : candidate.end]
        if any(ch.isdigit() for ch in value):  # "Born and raised in ..." is not a date
            yield _Candidate(
                candidate.start, candidate.end, candidate.kind, _age_from(value, today)
            )


def _addresses(text: str) -> Iterator[_Candidate]:
    yield from _labelled(text, _ADDRESS_LABEL, PiiKind.ADDRESS)
    for start, end in _segments(text):
        segment = text[start:end]
        if _label_match(_ADDRESS_LABEL, segment):
            continue  # already covered by the labelled value; keep the label visible
        street = len(_STREET_MARKERS.findall(segment))
        area = len(_AREA_MARKERS.findall(segment))
        postal = bool(_POSTAL_CODE.search(segment))
        has_digit = any(ch.isdigit() for ch in segment)
        if (postal and street + area >= 1) or (street >= 1 and area >= 1 and has_digit):
            lead = len(segment) - len(segment.lstrip())
            yield _Candidate(start + lead, start + len(segment.rstrip()), PiiKind.ADDRESS)


def _obfuscated_emails(text: str) -> Iterator[_Candidate]:
    for match in _OBFUSCATED_EMAIL.finditer(text):
        # Prose such as "worked at Agoda.com" has the same shape, so require a
        # bracketed "[at]" / "(at)", or both "at" and "dot" spelled out.
        value = match.group()
        bracketed = re.search(r"(?i)[\[(]\s*at\s*[\])]", value)
        spelled = re.search(r"(?i)\sat\s", value) and re.search(r"(?i)\sdot\s", value)
        if bracketed or spelled:
            yield _Candidate(match.start(), match.end(), PiiKind.EMAIL)


def _regex(text: str, pattern: re.Pattern[str], kind: PiiKind) -> Iterator[_Candidate]:
    for match in pattern.finditer(text):
        yield _Candidate(match.start(), match.end(), kind)


def _header_name(text: str) -> str | None:
    """The candidate's name: usually the first short, name-shaped line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines[:_HEADER_SEARCH_LINES]:
        match = _HEADER_NAME_LINE.match(line)
        if not match or len(line) > _MAX_HEADER_NAME_LENGTH or detect_section(line):
            continue
        words = match.group("name").split()
        if not any(_is_not_name_word(word) for word in words):
            return match.group("name")
    return None


def _name_strings(text: str, extra_names: Iterable[str]) -> set[str]:
    names = {n.strip() for n in extra_names if n.strip()}
    for start, end in _segments(text):
        segment = text[start:end].strip()
        honorific = _THAI_HONORIFIC_NAME.search(segment)
        if honorific:
            names.add(honorific.group("name"))
        else:
            label = _label_match(_NAME_LABEL, segment)
            if label:
                names.add(label.group("value").strip())
        for match in _EN_HONORIFIC_NAME.finditer(segment):
            names.add(match.group("name") or match.group("name2"))
    header = _header_name(text)
    if header:
        names.add(header)
    return names


# A Thai name may follow its honorific directly ("นางสาวสมหญิง"); each lookbehind
# must be fixed-width, hence one per honorific.
_AFTER_THAI_HONORIFIC: Final = "|".join(
    f"(?<={h})" for h in ("นางสาว", "นาง", "นาย", r"น\.ส\.", r"ดร\.", r"ร\.ต\.")
)


def _bounded(value: str) -> re.Pattern[str]:
    edge = rf"A-Za-z0-9{_THAI_LETTERS}"
    start = rf"(?:{_AFTER_THAI_HONORIFIC}|(?<![{edge}]))"
    return re.compile(rf"{start}{re.escape(value)}(?![{edge}])")


def _names(text: str, extra_names: Iterable[str]) -> Iterator[_Candidate]:
    values: set[str] = set()
    for name in _name_strings(text, extra_names):
        values.add(name)
        for word in name.split():
            is_thai = bool(re.match(rf"[{_THAI_LETTERS}]", word))
            minimum = _MIN_THAI_NAME_WORD if is_thai else _MIN_EN_NAME_WORD
            if len(word) >= minimum and not _is_not_name_word(word):
                values.add(word)
    for value in values:
        yield from _regex(text, _bounded(value), PiiKind.NAME)


def _collect(text: str, extra_names: Iterable[str], today: date) -> list[_Candidate]:
    candidates: list[_Candidate] = [
        *(_Candidate(a, b, PiiKind.EMAIL) for a, b in email_spans(text)),
        *_obfuscated_emails(text),
        *(_Candidate(a, b, PiiKind.PHONE) for a, b in phone_spans(text)),
        *_national_ids(text),
        *_regex(text, _PROFILE_URL, PiiKind.PROFILE_URL),
        *_labelled(text, _MESSAGING_LABEL, PiiKind.MESSAGING_ID),
        *_labelled(text, _MESSAGING_ID_LABEL, PiiKind.MESSAGING_ID),
        *_dates_of_birth(text, today),
        *_labelled(text, _RELIGION_LABEL, PiiKind.RELIGION),
        *_labelled(text, _MARITAL_LABEL, PiiKind.MARITAL_STATUS),
        *_addresses(text),
        *_names(text, extra_names),
    ]
    return [c for c in candidates if c.end > c.start]


def _resolve_overlaps(candidates: list[_Candidate]) -> list[_Candidate]:
    """Keep the longest candidate where spans overlap (earliest wins ties)."""
    chosen: list[_Candidate] = []
    for candidate in sorted(candidates, key=lambda c: (-(c.end - c.start), c.start)):
        if all(candidate.end <= c.start or candidate.start >= c.end for c in chosen):
            chosen.append(candidate)
    return sorted(chosen, key=lambda c: c.start)


# ── Public API ───────────────────────────────────────────────────────────


def redact_pii(
    text: str,
    *,
    extra_names: Iterable[str] = (),
    today: date | None = None,
) -> RedactionResult:
    """Replace personal data in ``text`` with placeholders.

    ``extra_names`` lets the caller supply known names (e.g. from the upload
    form) in addition to the ones detected automatically.
    """
    reference = today if today is not None else date.today()
    candidates = _resolve_overlaps(_collect(text, extra_names, reference))

    placeholders: dict[tuple[PiiKind, str], str] = {}
    counters: dict[PiiKind, int] = {}
    entities: list[Entity] = []
    parts: list[str] = []
    cursor = 0
    for candidate in candidates:
        original = text[candidate.start : candidate.end]
        key = (candidate.kind, original)
        if key not in placeholders:
            counters[candidate.kind] = counters.get(candidate.kind, 0) + 1
            label = f"{candidate.kind.value}_{counters[candidate.kind]}"
            if candidate.age is not None:
                label += f" age {candidate.age}"
            placeholders[key] = f"[{label}]"
        placeholder = placeholders[key]
        entities.append(
            Entity(candidate.kind, original, placeholder, candidate.start, candidate.end)
        )
        parts.append(text[cursor : candidate.start])
        parts.append(placeholder)
        cursor = candidate.end
    parts.append(text[cursor:])
    return RedactionResult(text="".join(parts), entities=tuple(entities))


_PLACEHOLDER: Final = re.compile(r"\[[A-Z_]+_\d+(?: age \d+)?\]")


def find_residual_pii(text: str) -> list[PiiKind]:
    """Kinds of high-confidence identifiers still present in (redacted) ``text``.

    Used as a fail-closed guard before text is sent to an external service.
    """
    cleaned = _PLACEHOLDER.sub(" ", text)
    found: list[PiiKind] = []
    if email_spans(cleaned) or next(_obfuscated_emails(cleaned), None) is not None:
        found.append(PiiKind.EMAIL)
    if phone_spans(cleaned):
        found.append(PiiKind.PHONE)
    if next(_national_ids(cleaned), None) is not None:
        found.append(PiiKind.NATIONAL_ID)
    if _PROFILE_URL.search(cleaned):
        found.append(PiiKind.PROFILE_URL)
    return found
