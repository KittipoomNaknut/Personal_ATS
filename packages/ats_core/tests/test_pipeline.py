from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ats_core.analysis.models import Bullet, Verdict
from ats_core.analysis.scoring import score_match
from ats_core.billing.ledger import CallRecord, Ledger
from ats_core.billing.pricing import Usage, default_price_table
from ats_core.gateway_client import GatewayError, TaskResult
from ats_core.pipeline import run_ai_analysis

TODAY = date(2026, 11, 27)
PRICES = default_price_table()
CALL_COST = PRICES.cost("claude-opus-5-5", Usage(input_tokens=2_500))  # $0.01

RESUME = """Somchai Example
somchai@example.com | 081-234-5678
Experience
Backend Engineer, Example Co. Jan 2021 - Present
- Built FastAPI services in Python for payments.
Data Analyst, Sample Ltd. Jun 2018 - Dec 2020
- Wrote SQL reports in PostgreSQL.
Education
B.Eng. Computer Engineering, Example University 2014 - 2018
"""
JD = (
    "Requirements\n- Python 3+ years\n- Bachelor in Computer Engineering\n- Valid work permit\n"
    "Nice to have\n- Kafka\nContact hr@company.example"
)


def requirement(
    text: str, kind: str, importance: str, min_years: float | None = None, knockout: bool = False
) -> dict[str, Any]:
    return {
        "text": text,
        "kind": kind,
        "importance": importance,
        "min_years": min_years,
        "knockout": knockout,
    }


PARSED_JD = {
    "job_title": "Backend Engineer",
    "requirements": [
        requirement("Python", "skill", "required", 3),
        requirement("Bachelor in Computer Engineering", "education", "required"),
        requirement("Valid work permit", "other", "required", knockout=True),
        requirement("Kafka", "skill", "preferred"),
    ],
}
STRUCTURE = {
    "jobs": [
        {
            "title": "Backend Engineer",
            "organization": "Example Co.",
            "start": "Jan 2021",
            "end": "Present",
            "bullet_ids": ["b4", "b5"],
        },
        {
            "title": "Data Analyst",
            "organization": "Sample Ltd.",
            "start": "Jun 2018",
            "end": "Dec 2020",
            "bullet_ids": ["b6", "b7"],
        },
    ],
    "education": [],
    "certifications": [],
    "languages": [],
}


def met(bullet: str, quote: str, rationale: str = "shown") -> dict[str, Any]:
    return {
        "verdict": "met",
        "evidence": [{"bullet_id": bullet, "quote": quote}],
        "rationale": rationale,
    }


NOT_MET = {"verdict": "not_met", "evidence": [], "rationale": "not shown"}
ANSWERS: dict[str, list[dict[str, Any]]] = {
    "Python": [
        met("b5", "in Python", "[NAME_1] uses Python"),
        met("b5", "FastAPI services in Python"),
    ],
    "Bachelor in Computer Engineering": [met("b9", "Computer Engineering")] * 2,
    "Valid work permit": [NOT_MET, NOT_MET],
    "Kafka": [NOT_MET],
}


@dataclass
class FakeGateway:
    ledger: Ledger
    jd: Mapping[str, Any] = field(default_factory=lambda: PARSED_JD)
    structure: Mapping[str, Any] = field(default_factory=lambda: STRUCTURE)
    answers: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {k: list(v) for k, v in ANSWERS.items()}
    )
    fail_on: str | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def _charge(self, task: str, analysis_id: str | None, client_id: str | None) -> TaskResult:
        if self.fail_on == task:
            raise GatewayError("budget", "daily budget would be exceeded", status=429)
        self.ledger.record_call(
            CallRecord(
                task=task,
                requested_model="claude-opus-5-5",
                usage=Usage(input_tokens=2_500),
                cost=CALL_COST,
                analysis_id=analysis_id,
                client_id=client_id,
            )
        )
        return TaskResult(result={}, cost_usd=CALL_COST.total, attempts=1, prompt_version="v1")

    def parse_jd(
        self, jd_text: str, *, analysis_id: str | None, client_id: str | None
    ) -> TaskResult:
        self.calls.append(("parse_jd", jd_text))
        base = self._charge("parse_jd", analysis_id, client_id)
        return TaskResult(self.jd, base.cost_usd, 1, "v1")

    def structure_resume(
        self, bullets: Sequence[Bullet], *, analysis_id: str | None, client_id: str | None
    ) -> TaskResult:
        self.calls.append(("structure", list(bullets)))
        base = self._charge("structure", analysis_id, client_id)
        return TaskResult(self.structure, base.cost_usd, 1, "v1")

    def judge(
        self,
        requirement: Mapping[str, Any],
        bullets: Sequence[Bullet],
        *,
        total_years: float | None,
        language: str,
        analysis_id: str | None,
        client_id: str | None,
    ) -> TaskResult:
        self.calls.append(("judge", (dict(requirement), total_years, language)))
        base = self._charge("judge", analysis_id, client_id)
        return TaskResult(self.answers[requirement["text"]].pop(0), base.cost_usd, 1, "v1")


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "ledger.db") as opened:
        yield opened


def run(gateway: FakeGateway, ledger: Ledger, **kwargs: Any) -> Any:
    options: dict[str, Any] = {
        "parseability": 1.0,
        "keyword_coverage": 0.5,
        "today": TODAY,
        **kwargs,
    }
    return run_ai_analysis(RESUME, JD, gateway, ledger, **options)


# ── Privacy of what is sent ──────────────────────────────────────────────


def test_only_redacted_text_reaches_the_gateway(ledger: Ledger) -> None:
    gateway = FakeGateway(ledger)
    run(gateway, ledger)
    sent = repr(gateway.calls)
    for secret in ("Somchai", "somchai@example.com", "081-234-5678", "hr@company.example"):
        assert secret not in sent
    jd_text = gateway.calls[0][1]
    assert "[EMAIL_1]" in jd_text
    bullets = gateway.calls[1][1]
    assert bullets[0] == Bullet("b1", "[NAME_1]")


# ── Results ──────────────────────────────────────────────────────────────


def test_full_analysis(ledger: Ledger) -> None:
    gateway = FakeGateway(ledger)
    analysis = run(gateway, ledger, language="en")
    verdicts = {r.requirement.text: r.consensus.verdict for r in analysis.results}
    assert verdicts == {
        "Python": Verdict.MET,
        "Bachelor in Computer Engineering": Verdict.MET,
        "Valid work permit": Verdict.NOT_MET,
        "Kafka": Verdict.NOT_MET,
    }
    assert [r.requirement.id for r in analysis.results] == ["R1", "R2", "R3", "R4"]
    assert analysis.job_title == "Backend Engineer"
    assert analysis.total_years == 8.5  # 31 + 71 months, computed in code
    python = analysis.results[0]
    assert python.years == 5.9  # only the job whose lines mention Python: Jan 2021 - Nov 2026
    assert (
        python.rationale == "[NAME_1] uses Python"
    )  # kept redacted: reports carry no personal data
    assert python.evidence_text == ("- Built FastAPI services in Python for payments.",)
    assert analysis.score.failed_knockouts == ("R3",)
    expected = score_match(
        [r.requirement for r in analysis.results],
        {r.requirement.id: r.consensus.verdict for r in analysis.results},
        years={"R1": 5.9},
        keyword_coverage=0.5,
        parseability=1.0,
    )
    assert analysis.score == expected
    judge_calls = [c for c in gateway.calls if c[0] == "judge"]
    assert len(judge_calls) == 7  # 3 required x 2 + 1 preferred
    assert {c[1][2] for c in judge_calls} == {"en"}
    assert {c[1][1] for c in judge_calls} == {8.5}
    assert analysis.cost_usd == CALL_COST.total * 9
    assert analysis.warnings == ()


def test_disagreement_gets_a_tiebreak(ledger: Ledger) -> None:
    gateway = FakeGateway(ledger)
    gateway.answers["Python"] = [met("b5", "in Python"), NOT_MET, met("b5", "Python")]
    analysis = run(gateway, ledger)
    python = analysis.results[0]
    assert python.consensus.verdict is Verdict.MET
    assert python.consensus.low_confidence
    assert python.consensus.votes == (Verdict.MET, Verdict.NOT_MET, Verdict.MET)


def test_invented_evidence_is_downgraded_and_reported(ledger: Ledger) -> None:
    gateway = FakeGateway(ledger)
    gateway.answers["Kafka"] = [met("b5", "Kafka streaming pipelines")]
    analysis = run(gateway, ledger)
    kafka = analysis.results[3]
    assert kafka.consensus.verdict is Verdict.NOT_MET
    assert kafka.consensus.chosen.claimed is Verdict.MET
    assert analysis.warnings == ("R4: a claim without valid evidence was downgraded to not met",)


def test_unreadable_job_dates_are_skipped_with_a_warning(ledger: Ledger) -> None:
    structure = {
        **STRUCTURE,
        "jobs": [
            *STRUCTURE["jobs"],
            {"title": "Freelance", "organization": "", "start": "", "end": "", "bullet_ids": []},
        ],
    }
    analysis = run(FakeGateway(ledger, structure=structure), ledger)
    assert analysis.total_years == 8.5
    assert "could not read the dates of job 'Freelance'" in analysis.warnings[0]


def test_unknown_bullet_ids_from_the_model_are_ignored(ledger: Ledger) -> None:
    structure = {
        **STRUCTURE,
        "jobs": [{**STRUCTURE["jobs"][0], "bullet_ids": ["b4", "b5", "b999"]}],
    }
    analysis = run(FakeGateway(ledger, structure=structure), ledger)
    assert analysis.results[0].years == 5.9


def test_experience_requirement_uses_total_years(ledger: Ledger) -> None:
    jd = {
        "job_title": "",
        "requirements": [requirement("5+ years in software", "experience", "required", 5)],
    }
    gateway = FakeGateway(ledger, jd=jd, answers={"5+ years in software": [NOT_MET, NOT_MET]})
    analysis = run(gateway, ledger)
    assert analysis.results[0].years == 8.5
    assert analysis.score.components["experience"] == 1.0


def test_no_requirements(ledger: Ledger) -> None:
    analysis = run(FakeGateway(ledger, jd={"job_title": "", "requirements": []}), ledger)
    assert analysis.results == ()
    assert analysis.score.components == {"keywords": 0.5}  # nothing else to score
    assert analysis.score.match == 0.5
    assert "the score reflects keyword matching only" in analysis.warnings[0]


# ── Ledger and attribution ───────────────────────────────────────────────


def test_analysis_is_recorded_for_the_client(ledger: Ledger) -> None:
    client = ledger.add_client("น้องเอ")
    analysis = run(FakeGateway(ledger), ledger, client_id=client.id)
    row = ledger._conn.execute(
        "SELECT client_id, status FROM analyses WHERE id = ?", (analysis.analysis_id,)
    ).fetchone()
    assert tuple(row) == (client.id, "done")
    (total,) = ledger.costs_by_client(date.today(), date.today())
    assert total.nickname == "น้องเอ"
    assert total.cost_usd == analysis.cost_usd


@pytest.mark.parametrize("step", ["parse_jd", "structure", "judge"])
def test_failure_marks_the_analysis_failed_and_keeps_costs(ledger: Ledger, step: str) -> None:
    gateway = FakeGateway(ledger, fail_on=step)
    with pytest.raises(GatewayError) as info:
        run(gateway, ledger)
    assert info.value.code == "budget"
    (status,) = ledger._conn.execute("SELECT status FROM analyses").fetchone()
    assert status == "failed"
    calls_before = {"parse_jd": 0, "structure": 1, "judge": 2}[step]
    assert ledger.spent_today() == CALL_COST.total * calls_before


def test_progress_is_reported(ledger: Ledger) -> None:
    messages: list[str] = []
    run(FakeGateway(ledger), ledger, on_progress=messages.append)
    assert messages[:2] == ["reading the job description", "reading the resume"]
    assert messages[-1] == "judging requirement 4 of 4"


def test_extra_names_are_redacted(ledger: Ledger) -> None:
    gateway = FakeGateway(ledger)
    resume = RESUME.replace("for payments.", "for payments with Kanokwan.")
    run_ai_analysis(
        resume,
        JD,
        gateway,
        ledger,
        parseability=1,
        keyword_coverage=None,
        extra_names=["Kanokwan"],
        today=TODAY,
    )
    assert "Kanokwan" not in repr(gateway.calls)


def test_cost_matches_ledger_exactly(ledger: Ledger) -> None:
    analysis = run(FakeGateway(ledger), ledger)
    assert analysis.cost_usd == ledger.analysis_cost(analysis.analysis_id)
    assert analysis.cost_usd == Decimal("0.09")


# ── Cost estimate and confirmation ───────────────────────────────────────


def test_estimate_is_offered_after_the_cheap_steps(ledger: Ledger) -> None:
    gateway = FakeGateway(ledger)
    seen: list[Any] = []

    def accept(estimate: Any) -> bool:
        seen.append(estimate)
        return True

    run(gateway, ledger, confirm=accept)
    (estimate,) = seen
    assert estimate.spent == CALL_COST.total * 2  # parse_jd + structure
    assert (estimate.calls_min, estimate.calls_max) == (7, 10)  # 3 required x 2 + 1, +3 tiebreaks
    assert estimate.basis == "token estimate"
    assert 0 < estimate.judge_low < estimate.judge_high
    assert estimate.total_high == estimate.spent + estimate.judge_high


def test_declining_cancels_before_judging(ledger: Ledger) -> None:
    from ats_core.pipeline import AnalysisCancelled

    gateway = FakeGateway(ledger)
    with pytest.raises(AnalysisCancelled) as info:
        run(gateway, ledger, confirm=lambda _estimate: False)
    assert info.value.cost_usd == CALL_COST.total * 2
    assert not [c for c in gateway.calls if c[0] == "judge"]
    (status,) = ledger._conn.execute("SELECT status FROM analyses").fetchone()
    assert status == "cancelled"


def test_estimate_uses_history_when_available(ledger: Ledger) -> None:
    from ats_core.pipeline import estimate_judging

    for cents in range(1, 11):  # ten past judge calls costing 0.01 .. 0.10
        cost = PRICES.cost("claude-opus-5-5", Usage(output_tokens=500 * cents))
        ledger.record_call(
            CallRecord(
                task="judge.v1",
                requested_model="m",
                usage=Usage(output_tokens=500 * cents),
                cost=cost,
            )
        )
    requirements = run(FakeGateway(ledger), ledger).results
    estimate = estimate_judging([r.requirement for r in requirements], [], ledger)
    assert estimate.basis == "history"
    assert estimate.judge_low == Decimal("0.03") * 7  # 25th percentile x minimum calls
    assert estimate.judge_high == Decimal("0.09") * 10  # 90th percentile x maximum calls


def test_estimate_without_requirements(ledger: Ledger) -> None:
    from ats_core.pipeline import estimate_judging

    estimate = estimate_judging([], [], ledger, spent=Decimal("0.02"))
    assert (estimate.calls_min, estimate.judge_high, estimate.total_low) == (0, 0, Decimal("0.02"))
