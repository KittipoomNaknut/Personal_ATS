"""Deterministic evidence checks for AI judgments.

The model must cite each piece of evidence by bullet id *and* quote the text it
relies on. A citation counts only if that bullet exists and actually contains
the quote. A "met" or "partial" verdict without at least one valid citation is
downgraded to "not met": the model may not claim what the resume does not say.
This also blunts prompt injection, because a planted instruction cannot create
resume text that is not there.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from ats_core.analysis.models import Bullet, Evidence, Judgment, Verdict

__all__ = ["VerifiedJudgment", "index_bullets", "verify_judgment"]

_MIN_QUOTE_CHARS: Final = 3
_SPACE: Final = re.compile(r"\s+")
# Characters models commonly normalise when quoting.
_EQUIVALENT: Final = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2022": " ",
        "\u00a0": " ",
    }
)


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFC", text).translate(_EQUIVALENT).casefold()
    return _SPACE.sub(" ", text).strip()


def index_bullets(text: str) -> list[Bullet]:
    """Number the non-empty lines of a resume: ``b1``, ``b2``, ..."""
    lines = (line.strip() for line in text.splitlines())
    return [
        Bullet(f"b{n}", line) for n, line in enumerate((line for line in lines if line), start=1)
    ]


@dataclass(frozen=True, slots=True)
class VerifiedJudgment:
    requirement_id: str
    claimed: Verdict
    verdict: Verdict
    """The verdict after verification (may be downgraded)."""
    evidence: tuple[Evidence, ...]
    """Citations that check out."""
    rejected: tuple[Evidence, ...]
    """Citations whose bullet does not exist or does not contain the quote."""
    rationale: str

    @property
    def downgraded(self) -> bool:
        return self.verdict is not self.claimed


def _valid(evidence: Evidence, bullets: dict[str, str]) -> bool:
    line = bullets.get(evidence.bullet_id)
    quote = _normalise(evidence.quote)
    return line is not None and len(quote) >= _MIN_QUOTE_CHARS and quote in line


def verify_judgment(judgment: Judgment, bullets: Sequence[Bullet]) -> VerifiedJudgment:
    by_id = {bullet.id: _normalise(bullet.text) for bullet in bullets}
    valid: list[Evidence] = []
    rejected: list[Evidence] = []
    for item in judgment.evidence:
        (valid if _valid(item, by_id) else rejected).append(item)
    verdict = judgment.verdict
    if verdict is not Verdict.NOT_MET and not valid:
        verdict = Verdict.NOT_MET
    return VerifiedJudgment(
        requirement_id=judgment.requirement_id,
        claimed=judgment.verdict,
        verdict=verdict,
        evidence=tuple(_dedupe(valid)),
        rejected=tuple(rejected),
        rationale=judgment.rationale,
    )


def _dedupe(items: Iterable[Evidence]) -> list[Evidence]:
    seen: set[tuple[str, str]] = set()
    unique = []
    for item in items:
        key = (item.bullet_id, _normalise(item.quote))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique
