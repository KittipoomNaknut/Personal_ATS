"""Overall match score (ARCHITECTURE §3.8).

Components, each in 0..1 and only when there is data for it:

* ``required`` / ``preferred`` - mean verdict score of those requirements
* ``experience`` - min(1, actual / required years), averaged over requirements
  that state a minimum
* ``keywords`` - share of required JD keywords found verbatim or by synonym
* ``education`` - mean verdict score of education and certification requirements

Missing components are left out and the remaining weights renormalised, so a
JD without preferred skills is not penalised. ``final = match x (0.6 + 0.4 x
parseability)``. Failing any knockout requirement is reported separately and
never hidden by a high score. Weights are initial values; phase 3 calibrates
them against the labelled eval set.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ats_core.analysis.models import Requirement, RequirementKind, Verdict

__all__ = ["DEFAULT_WEIGHTS", "MatchScore", "score_match"]

DEFAULT_WEIGHTS: Final[Mapping[str, float]] = {
    "required": 0.30,
    "preferred": 0.15,
    "experience": 0.15,
    "keywords": 0.15,
    "education": 0.10,
}
_EDUCATION_KINDS: Final = frozenset({RequirementKind.EDUCATION, RequirementKind.CERTIFICATION})


@dataclass(frozen=True, slots=True)
class MatchScore:
    final: float
    match: float
    components: Mapping[str, float]
    weights_used: Mapping[str, float]
    failed_knockouts: tuple[str, ...]
    """Ids of knockout requirements that are not met."""


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def score_match(
    requirements: Sequence[Requirement],
    verdicts: Mapping[str, Verdict],
    *,
    years: Mapping[str, float] | None = None,
    keyword_coverage: float | None = None,
    parseability: float,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> MatchScore:
    """Combine verified verdicts and deterministic facts into one score.

    ``years`` maps a requirement id to the candidate's computed years for it.
    """
    missing = [r.id for r in requirements if r.id not in verdicts]
    if missing:
        raise ValueError(f"no verdict for requirements {missing}")
    if not 0 <= parseability <= 1:
        raise ValueError("parseability must be between 0 and 1")
    years = years or {}

    def scores(selected: Sequence[Requirement]) -> list[float]:
        return [verdicts[r.id].score for r in selected]

    experience = [
        min(1.0, years.get(r.id, 0.0) / r.min_years)
        for r in requirements
        if r.min_years is not None and r.min_years > 0
    ]
    raw: dict[str, float | None] = {
        "required": _mean(scores([r for r in requirements if r.required])),
        "preferred": _mean(scores([r for r in requirements if not r.required])),
        "experience": _mean(experience),
        "keywords": keyword_coverage,
        "education": _mean(scores([r for r in requirements if r.kind in _EDUCATION_KINDS])),
    }
    components = {name: value for name, value in raw.items() if value is not None}
    total_weight = sum(weights[name] for name in components)
    match = (
        sum(components[name] * weights[name] for name in components) / total_weight
        if total_weight
        else 0.0
    )
    final = match * (0.6 + 0.4 * parseability)
    return MatchScore(
        final=round(final, 4),
        match=round(match, 4),
        components={k: round(v, 4) for k, v in components.items()},
        weights_used={name: round(weights[name] / total_weight, 4) for name in components}
        if total_weight
        else {},
        failed_knockouts=tuple(
            r.id for r in requirements if r.knockout and verdicts[r.id] is not Verdict.MET
        ),
    )
