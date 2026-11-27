"""Keyword matching between a job description and a resume (the ATS-filter view).

Most ATSs still filter on keywords, so this is reported separately from the
semantic judgement added in phase 2. The job description is split into
sections; a skill is *preferred* when it appears under a "nice to have" heading
or on a line with a preference cue ("is a plus", "จะพิจารณาเป็นพิเศษ"),
otherwise *required*. Skills in benefits / company / how-to-apply sections are
ignored.

Synonym matches are reported separately: a recruiter knows "K8s" means
"Kubernetes", but an exact-term ATS filter may not, so the report suggests
adding the job description's own wording.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ats_core.taxonomy import Skill, SkillMention, SkillTaxonomy, default_taxonomy

__all__ = [
    "Importance",
    "JdSkill",
    "KeywordReport",
    "SkillMatch",
    "extract_jd_skills",
    "match_keywords",
]


class Importance(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"


class _Section(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    IGNORED = "ignored"
    NEUTRAL = "neutral"


_MAX_HEADING_LENGTH: Final = 60
_HEADING_DECORATION: Final = re.compile(r"^[\s\-\u2022*#:|]+|[\s\-\u2022*#:|]+$")

_PREFERRED_HEADINGS: Final = re.compile(
    r"^(?:preferred(?: qualifications| skills)?|nice[ -]to[ -]have|good[ -]to[ -]have"
    r"|bonus(?: points)?"
    r"|(?:it'?s )?a plus|plus(?:es)?|advantages?|desirable|optional"
    r"|คุณสมบัติที่จะพิจารณาเป็นพิเศษ|จะพิจารณาเป็นพิเศษ|พิจารณาเป็นพิเศษ|ข้อได้เปรียบ|คุณสมบัติเพิ่มเติม)",
    re.IGNORECASE,
)
_REQUIRED_HEADINGS: Final = re.compile(
    r"^(?:requirements?|qualifications?|minimum qualifications|basic qualifications|must[ -]haves?"
    r"|required skills|skills(?: required| needed)?|what you(?:'ll)? need"
    r"|what we(?:'re| are) looking for"
    r"|who you are|about you|your profile|technical skills"
    r"|คุณสมบัติ(?:ผู้สมัคร|ที่ต้องการ|ประจำตำแหน่ง)?|ความต้องการ|ทักษะที่ต้องการ)",
    re.IGNORECASE,
)
_IGNORED_HEADINGS: Final = re.compile(
    r"^(?:benefits?|perks|what we offer|we offer|about us|about the company|company overview"
    r"|how to apply|to apply|contact|location|salary|compensation|working hours"
    r"|สวัสดิการ|เกี่ยวกับเรา|เกี่ยวกับบริษัท|วิธีการสมัคร|ช่องทางการสมัคร|ติดต่อ|สถานที่ปฏิบัติงาน|เงินเดือน)",
    re.IGNORECASE,
)
_NEUTRAL_HEADINGS: Final = re.compile(
    r"^(?:responsibilities|key responsibilities|job description|the role|about the role"
    r"|what you(?:'ll)? do"
    r"|duties|หน้าที่(?:ความรับผิดชอบ)?|ลักษณะงาน|รายละเอียดงาน)",
    re.IGNORECASE,
)
_PREFERENCE_CUE: Final = re.compile(
    r"preferred|preferable|(?:is|are|would be) a (?:big )?plus|a plus|nice to have|good to have"
    r"|bonus|advantage(?:ous)?|desirable|optional"
    r"|จะพิจารณาเป็นพิเศษ|พิจารณาเป็นพิเศษ|เป็นข้อได้เปรียบ|ข้อได้เปรียบ|หากมี|ถ้ามี|จะได้รับการพิจารณา",
    re.IGNORECASE,
)


_CLAUSE_SPLIT: Final = re.compile(r"(?<=[.!?;])\s+")


@dataclass(frozen=True, slots=True)
class JdSkill:
    skill: Skill
    importance: Importance
    terms: tuple[str, ...]
    """How the job description spells the skill (for exact-term advice)."""


@dataclass(frozen=True, slots=True)
class SkillMatch:
    requirement: JdSkill
    resume_mentions: tuple[SkillMention, ...]

    @property
    def found(self) -> bool:
        return bool(self.resume_mentions)

    @property
    def exact_term(self) -> bool:
        """True if the resume uses one of the job description's own spellings."""
        wanted = {_fold(term) for term in self.requirement.terms}
        return any(_fold(m.text) in wanted for m in self.resume_mentions)


@dataclass(frozen=True, slots=True)
class KeywordReport:
    matches: tuple[SkillMatch, ...]
    resume_only: tuple[Skill, ...]
    """Skills on the resume that the job description does not ask for."""

    def _of(self, importance: Importance) -> tuple[SkillMatch, ...]:
        return tuple(m for m in self.matches if m.requirement.importance is importance)

    @property
    def required(self) -> tuple[SkillMatch, ...]:
        return self._of(Importance.REQUIRED)

    @property
    def preferred(self) -> tuple[SkillMatch, ...]:
        return self._of(Importance.PREFERRED)

    @staticmethod
    def _coverage(matches: Sequence[SkillMatch]) -> float | None:
        return sum(m.found for m in matches) / len(matches) if matches else None

    @property
    def required_coverage(self) -> float | None:
        """Share of required skills found; ``None`` if the JD names none we recognise."""
        return self._coverage(self.required)

    @property
    def preferred_coverage(self) -> float | None:
        return self._coverage(self.preferred)

    @property
    def missing(self) -> tuple[SkillMatch, ...]:
        return tuple(m for m in self.matches if not m.found)

    @property
    def synonym_only(self) -> tuple[SkillMatch, ...]:
        """Found, but not with the job description's wording."""
        return tuple(m for m in self.matches if m.found and not m.exact_term)


def _fold(text: str) -> str:
    return re.sub(r"[\s\-]+", " ", unicodedata.normalize("NFC", text).casefold()).strip()


def _heading_kind(line: str) -> _Section | None:
    if len(line) > _MAX_HEADING_LENGTH:
        return None
    text = _HEADING_DECORATION.sub("", unicodedata.normalize("NFC", line)).strip()
    if not text:
        return None
    # Bilingual headings: "Qualifications / คุณสมบัติ".
    first = re.split(r"\s*[/(]\s*", text)[0]
    for pattern, kind in (
        (_PREFERRED_HEADINGS, _Section.PREFERRED),
        (_IGNORED_HEADINGS, _Section.IGNORED),
        (_REQUIRED_HEADINGS, _Section.REQUIRED),
        (_NEUTRAL_HEADINGS, _Section.NEUTRAL),
    ):
        match = pattern.match(first)
        # The heading word must be (almost) the whole line, not the start of a sentence.
        if match and len(first) - match.end() <= 3:
            return kind
    return None


def _jd_lines(jd_text: str) -> Iterator[tuple[str, _Section]]:
    section = _Section.NEUTRAL
    for line in jd_text.splitlines():
        kind = _heading_kind(line)
        if kind is not None:
            section = kind
            continue
        yield line, section


def extract_jd_skills(jd_text: str, taxonomy: SkillTaxonomy | None = None) -> list[JdSkill]:
    """Skills a job description asks for, with their importance, in order of first mention."""
    taxonomy = taxonomy or default_taxonomy()
    importance: dict[str, Importance] = {}
    terms: dict[str, list[str]] = {}
    for line, section in _jd_lines(jd_text):
        if section is _Section.IGNORED:
            continue
        # A cue only applies to its own sentence: "We need Python. Docker is a plus."
        for clause in _CLAUSE_SPLIT.split(line):
            preferred = section is _Section.PREFERRED or bool(_PREFERENCE_CUE.search(clause))
            level = Importance.PREFERRED if preferred else Importance.REQUIRED
            for mention in taxonomy.find(clause):
                # A skill required anywhere is required.
                if importance.get(mention.skill_id) is not Importance.REQUIRED:
                    importance[mention.skill_id] = level
                spellings = terms.setdefault(mention.skill_id, [])
                if mention.text not in spellings:
                    spellings.append(mention.text)
    return [
        JdSkill(taxonomy.get(skill_id), importance[skill_id], tuple(terms[skill_id]))
        for skill_id in importance
    ]


def match_keywords(
    resume_text: str, jd_text: str, taxonomy: SkillTaxonomy | None = None
) -> KeywordReport:
    """Compare the skills a job description asks for with those on a resume."""
    taxonomy = taxonomy or default_taxonomy()
    requirements = extract_jd_skills(jd_text, taxonomy)
    mentions = taxonomy.find(resume_text)
    by_skill: dict[str, list[SkillMention]] = {}
    for mention in mentions:
        by_skill.setdefault(mention.skill_id, []).append(mention)
    wanted = {r.skill.id for r in requirements}
    resume_only = tuple(
        taxonomy.get(skill_id)
        for skill_id in dict.fromkeys(m.skill_id for m in mentions)
        if skill_id not in wanted
    )
    matches = tuple(
        SkillMatch(requirement, tuple(by_skill.get(requirement.skill.id, ())))
        for requirement in requirements
    )
    return KeywordReport(matches=matches, resume_only=resume_only)
