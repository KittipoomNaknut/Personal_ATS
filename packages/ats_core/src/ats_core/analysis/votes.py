"""Combine repeated judgments of the same requirement (self-consistency).

Required requirements are judged twice independently. If both agree, that is
the verdict. If not, a third judgment breaks the tie and the result is marked
low-confidence so the report can say so.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from ats_core.analysis.evidence import VerifiedJudgment
from ats_core.analysis.models import Verdict

__all__ = ["Consensus", "combine", "needs_tiebreak"]


@dataclass(frozen=True, slots=True)
class Consensus:
    verdict: Verdict
    low_confidence: bool
    chosen: VerifiedJudgment
    """The judgment whose evidence and rationale are reported."""
    votes: tuple[Verdict, ...]


def needs_tiebreak(judgments: Sequence[VerifiedJudgment]) -> bool:
    return len(judgments) == 2 and judgments[0].verdict is not judgments[1].verdict


def combine(judgments: Sequence[VerifiedJudgment]) -> Consensus:
    """Majority of the verified verdicts; with no majority, the most cautious one."""
    if not judgments:
        raise ValueError("no judgments to combine")
    ids = {j.requirement_id for j in judgments}
    if len(ids) != 1:
        raise ValueError(f"judgments are for different requirements: {sorted(ids)}")
    votes = tuple(j.verdict for j in judgments)
    counts = Counter(votes)
    top = max(counts.values())
    leaders = [verdict for verdict in Verdict if counts.get(verdict) == top]
    # Verdict order is MET, PARTIAL, NOT_MET: the last leader is the most cautious.
    verdict = leaders[-1]
    chosen = next(j for j in judgments if j.verdict is verdict)
    return Consensus(verdict=verdict, low_confidence=len(counts) > 1, chosen=chosen, votes=votes)
