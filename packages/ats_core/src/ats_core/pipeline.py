"""The full AI analysis of one resume against one job description.

Steps, in order:

1. Redact personal data from the *visible* resume text and from the job
   description (hidden text earns no credit; nothing identifying is sent).
2. Number the resume lines (``b1``, ``b2``...) for citations.
3. Ask the gateway to split the job description into requirements.
4. Ask the gateway for the resume's jobs; compute years in code.
5. Judge every requirement: required ones twice, with a third judgment when the
   two disagree; verify every citation against the numbered lines.
6. Score the match.

Every step's cost is recorded by the gateway under one analysis id in the
ledger, attributed to the client when one is given.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from ats_core.analysis.evidence import VerifiedJudgment, index_bullets, verify_judgment
from ats_core.analysis.experience import DateParseError, Job, total_years
from ats_core.analysis.models import (
    Bullet,
    Evidence,
    Judgment,
    Requirement,
    RequirementKind,
    Verdict,
)
from ats_core.analysis.scoring import MatchScore, score_match
from ats_core.analysis.votes import Consensus, combine, needs_tiebreak
from ats_core.billing.ledger import Ledger
from ats_core.gateway_client import TaskResult
from ats_core.pii import RedactionResult, redact_pii
from ats_core.taxonomy import SkillTaxonomy, default_taxonomy

__all__ = ["AiAnalysis", "AnalysisGateway", "RequirementResult", "run_ai_analysis"]


class AnalysisGateway(Protocol):
    def parse_jd(
        self, jd_text: str, *, analysis_id: str | None, client_id: str | None
    ) -> TaskResult: ...

    def structure_resume(
        self, bullets: Sequence[Bullet], *, analysis_id: str | None, client_id: str | None
    ) -> TaskResult: ...

    def judge(
        self,
        requirement: Mapping[str, Any],
        bullets: Sequence[Bullet],
        *,
        total_years: float | None,
        language: str,
        analysis_id: str | None,
        client_id: str | None,
    ) -> TaskResult: ...


@dataclass(frozen=True, slots=True)
class RequirementResult:
    requirement: Requirement
    consensus: Consensus
    years: float | None
    """Computed years relevant to this requirement (only when it states a minimum)."""
    rationale: str
    """The chosen judgment's rationale with placeholders restored (local display only)."""
    evidence_text: tuple[str, ...]
    """Cited resume lines with placeholders restored."""


@dataclass(frozen=True, slots=True)
class AiAnalysis:
    analysis_id: str
    job_title: str
    results: tuple[RequirementResult, ...]
    total_years: float
    score: MatchScore
    cost_usd: Decimal
    warnings: tuple[str, ...] = field(default=())


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _requirements(parsed: Mapping[str, Any]) -> list[Requirement]:
    # Ids are assigned here, not taken from the model.
    return [
        Requirement(
            id=f"R{number}",
            text=str(item["text"]),
            kind=RequirementKind(item["kind"]),
            required=item["importance"] == "required",
            min_years=float(item["min_years"]) if item["min_years"] is not None else None,
            knockout=bool(item["knockout"]),
        )
        for number, item in enumerate(parsed["requirements"], start=1)
    ]


def _jobs(structure: Mapping[str, Any], known_ids: set[str]) -> list[dict[str, Any]]:
    jobs = []
    for job in structure["jobs"]:
        ids = [i for i in job["bullet_ids"] if i in known_ids]
        jobs.append({**job, "bullet_ids": ids})
    return jobs


def _years(
    requirements: Sequence[Requirement],
    jobs: Sequence[Mapping[str, Any]],
    bullets: Mapping[str, str],
    *,
    today: date,
    taxonomy: SkillTaxonomy,
    warnings: list[str],
) -> tuple[float, dict[str, float]]:
    dated: list[tuple[Job, set[str]]] = []
    for job in jobs:
        candidate = Job(str(job["start"]), str(job["end"]))
        try:
            total_years([candidate], today=today)
        except DateParseError:
            warnings.append(f"could not read the dates of job {job['title']!r}; it is not counted")
            continue
        text = " ".join(bullets.get(i, "") for i in job["bullet_ids"])
        dated.append((candidate, taxonomy.skill_ids(text)))
    overall = total_years([job for job, _ in dated], today=today)
    per_requirement: dict[str, float] = {}
    for requirement in requirements:
        if requirement.min_years is None:
            continue
        skills = taxonomy.skill_ids(requirement.text)
        if requirement.kind is RequirementKind.SKILL and skills:
            relevant = [job for job, found in dated if found & skills]
            per_requirement[requirement.id] = total_years(relevant, today=today)
        else:
            per_requirement[requirement.id] = overall
    return overall, per_requirement


def _judgment(requirement_id: str, raw: Mapping[str, Any]) -> Judgment:
    return Judgment(
        requirement_id=requirement_id,
        verdict=Verdict(raw["verdict"]),
        evidence=tuple(Evidence(str(e["bullet_id"]), str(e["quote"])) for e in raw["evidence"]),
        rationale=str(raw["rationale"]),
    )


def _payload(requirement: Requirement) -> dict[str, Any]:
    return {
        "id": requirement.id,
        "text": requirement.text,
        "kind": requirement.kind.value,
        "importance": "required" if requirement.required else "preferred",
        "min_years": requirement.min_years,
        "knockout": requirement.knockout,
    }


def run_ai_analysis(
    resume_visible_text: str,
    jd_text: str,
    gateway: AnalysisGateway,
    ledger: Ledger,
    *,
    parseability: float,
    keyword_coverage: float | None,
    client_id: str | None = None,
    extra_names: Sequence[str] = (),
    language: str = "th",
    today: date | None = None,
    taxonomy: SkillTaxonomy | None = None,
    on_progress: Callable[[str], None] = lambda _message: None,
) -> AiAnalysis:
    """Run the analysis.

    Raises :class:`~ats_core.gateway_client.GatewayError` when the gateway
    refuses or fails a step; the analysis is then marked failed in the ledger.
    """
    reference = today or date.today()
    taxonomy = taxonomy or default_taxonomy()
    resume: RedactionResult = redact_pii(
        resume_visible_text, extra_names=extra_names, today=reference
    )
    jd = redact_pii(jd_text, today=reference)
    bullets = index_bullets(resume.text)
    by_id = {b.id: b.text for b in bullets}
    analysis_id = ledger.start_analysis(
        client_id=client_id, resume_sha256=_sha256(resume_visible_text), jd_sha256=_sha256(jd_text)
    )
    attribution = {"analysis_id": analysis_id, "client_id": client_id}
    warnings: list[str] = []
    try:
        on_progress("reading the job description")
        parsed = gateway.parse_jd(jd.text, **attribution)
        requirements = _requirements(parsed.result)
        if not requirements:
            warnings.append(
                "no requirements were found in the job description; "
                "the score reflects keyword matching only"
            )

        on_progress("reading the resume")
        structure = gateway.structure_resume(bullets, **attribution)
        jobs = _jobs(structure.result, set(by_id))
        overall, per_requirement = _years(
            requirements, jobs, by_id, today=reference, taxonomy=taxonomy, warnings=warnings
        )

        results: list[RequirementResult] = []
        for number, requirement in enumerate(requirements, start=1):
            on_progress(f"judging requirement {number} of {len(requirements)}")

            def judge_once(requirement: Requirement = requirement) -> VerifiedJudgment:
                answer = gateway.judge(
                    _payload(requirement),
                    bullets,
                    total_years=overall,
                    language=language,
                    **attribution,
                )
                return verify_judgment(_judgment(requirement.id, answer.result), bullets)

            judgments = [judge_once()]
            if requirement.required:
                judgments.append(judge_once())
                if needs_tiebreak(judgments):
                    judgments.append(judge_once())
            consensus = combine(judgments)
            if any(j.downgraded for j in judgments):
                warnings.append(
                    f"{requirement.id}: a claim without valid evidence was downgraded to not met"
                )
            results.append(
                RequirementResult(
                    requirement=requirement,
                    consensus=consensus,
                    years=per_requirement.get(requirement.id),
                    rationale=resume.restore(consensus.chosen.rationale),
                    evidence_text=tuple(
                        resume.restore(by_id[e.bullet_id]) for e in consensus.chosen.evidence
                    ),
                )
            )
    except BaseException:
        # Never leave an analysis "running": costs already spent stay attributed.
        ledger.finish_analysis(analysis_id, "failed")
        raise

    score = score_match(
        requirements,
        {r.requirement.id: r.consensus.verdict for r in results},
        years=per_requirement,
        keyword_coverage=keyword_coverage,
        parseability=parseability,
    )
    ledger.finish_analysis(analysis_id, "done")
    return AiAnalysis(
        analysis_id=analysis_id,
        job_title=str(parsed.result.get("job_title", "")),
        results=tuple(results),
        total_years=overall,
        score=score,
        cost_usd=ledger.analysis_cost(analysis_id),
        warnings=tuple(warnings),
    )
