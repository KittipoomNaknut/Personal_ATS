"""Data shapes shared by the AI tasks and the deterministic checks around them."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "Bullet",
    "Evidence",
    "Judgment",
    "Requirement",
    "RequirementKind",
    "Verdict",
]


class RequirementKind(StrEnum):
    SKILL = "skill"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    CERTIFICATION = "certification"
    LANGUAGE = "language"
    SOFT_SKILL = "soft_skill"
    OTHER = "other"


class Verdict(StrEnum):
    MET = "met"
    PARTIAL = "partial"
    NOT_MET = "not_met"

    @property
    def score(self) -> float:
        return {Verdict.MET: 1.0, Verdict.PARTIAL: 0.5, Verdict.NOT_MET: 0.0}[self]


@dataclass(frozen=True, slots=True)
class Bullet:
    """One line of the (redacted) resume, addressable by id in AI answers."""

    id: str
    text: str


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    text: str
    kind: RequirementKind
    required: bool
    min_years: float | None = None
    knockout: bool = False
    """Failing it rules the candidate out regardless of the score."""


@dataclass(frozen=True, slots=True)
class Evidence:
    bullet_id: str
    quote: str


@dataclass(frozen=True, slots=True)
class Judgment:
    """The model's verdict on one requirement, as returned (not yet verified)."""

    requirement_id: str
    verdict: Verdict
    evidence: tuple[Evidence, ...]
    rationale: str
