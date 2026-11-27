"""Bilingual (Thai / English) resume section heading detection."""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from typing import Final

__all__ = ["Section", "detect_section"]


class Section(StrEnum):
    CONTACT = "contact"
    SUMMARY = "summary"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    SKILLS = "skills"
    PROJECTS = "projects"
    CERTIFICATIONS = "certifications"
    LANGUAGES = "languages"
    AWARDS = "awards"


_HEADINGS: Final[dict[Section, tuple[str, ...]]] = {
    Section.CONTACT: ("contact", "contact information", "contact info", "ข้อมูลติดต่อ", "ติดต่อ"),
    Section.SUMMARY: (
        "summary",
        "profile",
        "professional summary",
        "about me",
        "objective",
        "career objective",
        "สรุป",
        "ประวัติโดยย่อ",
        "เกี่ยวกับฉัน",
        "วัตถุประสงค์",
        "เป้าหมายในการทำงาน",
    ),
    Section.EXPERIENCE: (
        "experience",
        "work experience",
        "professional experience",
        "employment",
        "employment history",
        "work history",
        "career history",
        "ประสบการณ์",
        "ประสบการณ์ทำงาน",
        "ประสบการณ์การทำงาน",
        "ประวัติการทำงาน",
    ),
    Section.EDUCATION: (
        "education",
        "academic background",
        "educational background",
        "การศึกษา",
        "ประวัติการศึกษา",
        "วุฒิการศึกษา",
    ),
    Section.SKILLS: (
        "skills",
        "technical skills",
        "core skills",
        "key skills",
        "competencies",
        "core competencies",
        "ทักษะ",
        "ความสามารถ",
        "ทักษะและความสามารถ",
        "ความสามารถพิเศษ",
    ),
    Section.PROJECTS: (
        "projects",
        "personal projects",
        "key projects",
        "โครงการ",
        "ผลงาน",
        "โปรเจกต์",
    ),
    Section.CERTIFICATIONS: (
        "certifications",
        "certificates",
        "licenses",
        "licenses & certifications",
        "licenses and certifications",
        "ใบรับรอง",
        "ใบประกาศนียบัตร",
        "ประกาศนียบัตร",
    ),
    Section.LANGUAGES: ("languages", "language skills", "ภาษา", "ทักษะทางภาษา"),
    Section.AWARDS: ("awards", "honors", "achievements", "รางวัล", "รางวัลและผลงาน"),
}

_LOOKUP: Final[dict[str, Section]] = {
    heading: section for section, headings in _HEADINGS.items() for heading in headings
}
_MAX_HEADING_LENGTH: Final = 40
# Separators and bullets around headings (incl. fullwidth colon, en/em dash).
_DECORATION: Final = re.compile(r"[\s:\uff1a\-\u2013\u2014|\u2022\u00b7*#_=]+")


def _normalize(line: str) -> str:
    text = unicodedata.normalize("NFC", line).casefold()
    return _DECORATION.sub(" ", text).strip()


def detect_section(line: str) -> Section | None:
    """Return the section a heading line introduces, or ``None`` if it is not a heading."""
    if len(line) > _MAX_HEADING_LENGTH:
        return None
    normalized = _normalize(line)
    if normalized in _LOOKUP:
        return _LOOKUP[normalized]
    # Bilingual headings such as "Experience / ประสบการณ์" or "ทักษะ (Skills)".
    for part in re.split(r"\s*[/()]\s*", normalized):
        if part and part in _LOOKUP:
            return _LOOKUP[part]
    return None
