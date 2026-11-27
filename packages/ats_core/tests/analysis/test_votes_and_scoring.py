from __future__ import annotations

import pytest

from ats_core.analysis.evidence import VerifiedJudgment
from ats_core.analysis.models import Evidence, Requirement, RequirementKind, Verdict
from ats_core.analysis.scoring import DEFAULT_WEIGHTS, score_match
from ats_core.analysis.votes import combine, needs_tiebreak

MET, PARTIAL, NOT = Verdict.MET, Verdict.PARTIAL, Verdict.NOT_MET


def verified(verdict: Verdict, requirement_id: str = "R1", note: str = "") -> VerifiedJudgment:
    evidence = (Evidence("b1", "x" * 5),) if verdict is not NOT else ()
    return VerifiedJudgment(requirement_id, verdict, verdict, evidence, (), note or verdict.value)


# ── Votes ────────────────────────────────────────────────────────────────


def test_agreeing_votes() -> None:
    consensus = combine([verified(MET), verified(MET)])
    assert consensus.verdict is MET
    assert not consensus.low_confidence
    assert consensus.votes == (MET, MET)
    assert not needs_tiebreak([verified(MET), verified(MET)])


def test_disagreement_needs_tiebreak_and_majority_wins() -> None:
    first, second = verified(MET), verified(NOT)
    assert needs_tiebreak([first, second])
    consensus = combine([first, second, verified(MET, note="tiebreak")])
    assert consensus.verdict is MET
    assert consensus.low_confidence
    assert consensus.chosen.rationale == "met"


def test_three_way_split_takes_most_cautious() -> None:
    assert combine([verified(MET), verified(PARTIAL), verified(NOT)]).verdict is NOT


def test_two_way_tie_takes_most_cautious() -> None:
    assert combine([verified(MET), verified(PARTIAL)]).verdict is PARTIAL


def test_single_judgment() -> None:
    consensus = combine([verified(PARTIAL)])
    assert consensus.verdict is PARTIAL
    assert not consensus.low_confidence


def test_combine_validation() -> None:
    with pytest.raises(ValueError, match="no judgments"):
        combine([])
    with pytest.raises(ValueError, match="different requirements"):
        combine([verified(MET, "R1"), verified(MET, "R2")])


# ── Scoring ──────────────────────────────────────────────────────────────


def req(
    rid: str,
    *,
    required: bool = True,
    kind: RequirementKind = RequirementKind.SKILL,
    min_years: float | None = None,
    knockout: bool = False,
) -> Requirement:
    return Requirement(rid, f"text {rid}", kind, required, min_years, knockout)


REQUIREMENTS = [
    req("R1"),
    req("R2"),
    req("R3", min_years=5),
    req("P1", required=False),
    req("E1", kind=RequirementKind.EDUCATION),
]


def test_full_score_by_hand() -> None:
    verdicts = {"R1": MET, "R2": PARTIAL, "R3": MET, "P1": NOT, "E1": MET}
    result = score_match(
        REQUIREMENTS, verdicts, years={"R3": 4.0}, keyword_coverage=0.8, parseability=0.9
    )
    # required = (1 + 0.5 + 1 + 1) / 4 = 0.875 (R1, R2, R3, E1 are required)
    assert result.components == {
        "required": 0.875,
        "preferred": 0.0,
        "experience": 0.8,  # 4 of 5 years
        "keywords": 0.8,
        "education": 1.0,
    }
    match = 0.30 * 0.875 + 0.15 * 0 + 0.15 * 0.8 + 0.15 * 0.8 + 0.10 * 1.0
    match /= 0.85  # weights of the components present
    assert result.match == round(match, 4)
    assert result.final == round(match * (0.6 + 0.4 * 0.9), 4)
    assert sum(result.weights_used.values()) == pytest.approx(1.0, abs=1e-3)
    assert result.failed_knockouts == ()


def test_missing_components_are_renormalised_not_penalised() -> None:
    result = score_match([req("R1")], {"R1": MET}, parseability=1.0)
    assert result.components == {"required": 1.0}
    assert result.weights_used == {"required": 1.0}
    assert result.match == 1.0
    assert result.final == 1.0


def test_parseability_scales_the_final_score() -> None:
    result = score_match([req("R1")], {"R1": MET}, parseability=0.0)
    assert result.match == 1.0
    assert result.final == 0.6


def test_experience_is_capped_and_missing_years_count_as_zero() -> None:
    requirements = [req("R1", min_years=2), req("R2", min_years=4)]
    result = score_match(requirements, {"R1": MET, "R2": MET}, years={"R1": 10.0}, parseability=1)
    assert result.components["experience"] == 0.5  # min(1, 10/2)=1 and 0/4=0


def test_failed_knockouts_are_reported() -> None:
    requirements = [req("K1", knockout=True), req("K2", knockout=True), req("R1")]
    result = score_match(requirements, {"K1": PARTIAL, "K2": MET, "R1": MET}, parseability=1)
    assert result.failed_knockouts == ("K1",)


def test_no_requirements_at_all() -> None:
    result = score_match([], {}, parseability=1.0)
    assert result.match == 0.0
    assert result.components == {}
    assert result.weights_used == {}


def test_scoring_validation() -> None:
    with pytest.raises(ValueError, match="no verdict"):
        score_match([req("R1")], {}, parseability=1)
    with pytest.raises(ValueError, match="parseability"):
        score_match([req("R1")], {"R1": MET}, parseability=1.5)


def test_default_weights_leave_room_for_semantic_similarity() -> None:
    # 1.0 minus the 0.15 reserved for embedding similarity, which arrives later.
    assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(0.85)
    assert Verdict.MET.score == 1.0
    assert Verdict.PARTIAL.score == 0.5
    assert Verdict.NOT_MET.score == 0.0
