"""Assemble an ATS check report and render it as text (Thai / English) or JSON.

The report never contains personal values: it only counts what redaction would
remove. The redacted text itself is available separately for inspection.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal

from ats_core.analysis.models import Verdict
from ats_core.extract import ExtractedDocument
from ats_core.keywords import KeywordReport, SkillMatch, match_keywords
from ats_core.parseability import ParseabilityReport, Severity, audit, render_issue
from ats_core.pii import PiiKind, RedactionResult, redact_pii
from ats_core.pipeline import AiAnalysis, RequirementResult

__all__ = ["CheckReport", "Language", "build_report", "render_json", "render_text"]

Language = Literal["th", "en"]

REPORT_VERSION: Final = 2  # 2: optional "ai" section


@dataclass(frozen=True, slots=True)
class CheckReport:
    source_name: str
    document: ExtractedDocument
    parseability: ParseabilityReport
    keywords: KeywordReport | None
    redaction: RedactionResult
    ai: AiAnalysis | None = None
    client: str | None = None


def build_report(
    document: ExtractedDocument,
    *,
    source_name: str,
    jd_text: str | None = None,
    extra_names: Sequence[str] = (),
    today: date | None = None,
) -> CheckReport:
    # Keywords count only what a reader can see; redaction covers everything,
    # because hidden text can contain personal data too.
    keywords = match_keywords(document.visible_text, jd_text) if jd_text is not None else None
    return CheckReport(
        source_name=source_name,
        document=document,
        parseability=audit(document),
        keywords=keywords,
        redaction=redact_pii(document.text, extra_names=extra_names, today=today),
    )


# ── Labels ───────────────────────────────────────────────────────────────

_LABELS: Final[dict[Language, dict[str, str]]] = {
    "en": {
        "title": "ATS resume check",
        "format": "Format",
        "page": "page",
        "pages": "pages",
        "parseability": "ATS readability",
        "no_issues": "No readability problems found.",
        "fix": "Fix",
        "critical": "CRITICAL",
        "warning": "WARNING",
        "info": "INFO",
        "sections": "Sections found",
        "keywords": "Keywords from the job description",
        "no_jd": "No job description given (use --jd) - keyword matching skipped.",
        "no_jd_skills": "No recognised skills in the job description.",
        "required": "Required",
        "preferred": "Preferred",
        "synonym": 'resume says "{resume}", job description says "{jd}" - add the exact term',
        "resume_only": "On the resume but not asked for",
        "privacy": "Personal data removed before anything is sent to AI",
        "privacy_none": "No personal data detected.",
        "ai": "AI match (Claude)",
        "ai_before": "before the readability adjustment",
        "components": "Components",
        "knockout": "Knockout requirement not met",
        "years": "{need:g}+ years required, {have:g} found",
        "evidence": "evidence",
        "reason": "reason",
        "low_confidence": "low confidence, votes",
        "notes": "Notes",
        "cost": "Cost of this analysis",
        "client": "client",
        "component_names": "required|preferred|experience|keywords|education",
    },
    "th": {
        "title": "ผลตรวจ resume สำหรับ ATS",
        "format": "รูปแบบไฟล์",
        "page": "หน้า",
        "pages": "หน้า",
        "parseability": "ความพร้อมให้ ATS อ่าน",
        "no_issues": "ไม่พบปัญหาการอ่านของ ATS",
        "fix": "วิธีแก้",
        "critical": "วิกฤต",
        "warning": "คำเตือน",
        "info": "ข้อมูล",
        "sections": "หัวข้อที่พบ",
        "keywords": "Keyword ตาม Job Description",
        "no_jd": "ไม่ได้ระบุ Job Description (ใช้ --jd) จึงข้ามการเทียบ keyword",
        "no_jd_skills": "ไม่พบ skill ที่รู้จักใน Job Description",
        "required": "จำเป็น",
        "preferred": "พิจารณาเป็นพิเศษ",
        "synonym": 'resume ใช้ "{resume}" แต่ JD ใช้ "{jd}" ควรเพิ่มคำตาม JD',
        "resume_only": "มีใน resume แต่ JD ไม่ได้ถาม",
        "privacy": "ข้อมูลส่วนตัวที่จะถูกลบก่อนส่งให้ AI",
        "privacy_none": "ไม่พบข้อมูลส่วนตัว",
        "ai": "ความเหมาะสมกับงาน (Claude)",
        "ai_before": "ก่อนปรับตามความพร้อมให้ ATS อ่าน",
        "components": "องค์ประกอบ",
        "knockout": "ไม่ผ่านคุณสมบัติขั้นต่ำ",
        "years": "ต้องการ {need:g} ปีขึ้นไป พบ {have:g} ปี",
        "evidence": "หลักฐาน",
        "reason": "เหตุผล",
        "low_confidence": "ไม่มั่นใจ ผลโหวต",
        "notes": "หมายเหตุ",
        "cost": "ค่าใช้จ่ายของการวิเคราะห์นี้",
        "client": "ลูกค้า",
        "component_names": "ข้อจำเป็น|ข้อพิจารณาพิเศษ|ประสบการณ์|keyword|การศึกษา",
    },
}

_PII_LABELS: Final[dict[Language, dict[PiiKind, str]]] = {
    "en": {
        PiiKind.NAME: "names",
        PiiKind.EMAIL: "e-mail",
        PiiKind.PHONE: "phone",
        PiiKind.NATIONAL_ID: "national ID",
        PiiKind.ADDRESS: "address",
        PiiKind.PROFILE_URL: "profile URL",
        PiiKind.MESSAGING_ID: "messaging ID",
        PiiKind.DATE_OF_BIRTH: "date of birth",
        PiiKind.RELIGION: "religion",
        PiiKind.MARITAL_STATUS: "marital status",
    },
    "th": {
        PiiKind.NAME: "ชื่อ",
        PiiKind.EMAIL: "อีเมล",
        PiiKind.PHONE: "เบอร์โทร",
        PiiKind.NATIONAL_ID: "เลขบัตรประชาชน",
        PiiKind.ADDRESS: "ที่อยู่",
        PiiKind.PROFILE_URL: "ลิงก์โปรไฟล์",
        PiiKind.MESSAGING_ID: "ไอดีแชท",
        PiiKind.DATE_OF_BIRTH: "วันเกิด",
        PiiKind.RELIGION: "ศาสนา",
        PiiKind.MARITAL_STATUS: "สถานภาพสมรส",
    },
}

_SEVERITY_ORDER: Final = (Severity.CRITICAL, Severity.WARNING, Severity.INFO)


def _pii_counts(redaction: RedactionResult) -> dict[PiiKind, int]:
    return {kind: redaction.count(kind) for kind in PiiKind if redaction.count(kind)}


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


# ── Text ─────────────────────────────────────────────────────────────────


def _match_line(match: SkillMatch, labels: dict[str, str]) -> str:
    name = match.requirement.skill.name
    if not match.found:
        return f"    [ ] {name}"
    line = f"    [x] {name}"
    if not match.exact_term:
        line += (
            "  ("
            + labels["synonym"].format(
                resume=match.resume_mentions[0].text, jd=match.requirement.terms[0]
            )
            + ")"
        )
    return line


def _keyword_lines(keywords: KeywordReport | None, labels: dict[str, str]) -> list[str]:
    lines = ["", labels["keywords"]]
    if keywords is None:
        return [*lines, f"  {labels['no_jd']}"]
    if not keywords.matches:
        return [*lines, f"  {labels['no_jd_skills']}"]
    for key, matches, coverage in (
        ("required", keywords.required, keywords.required_coverage),
        ("preferred", keywords.preferred, keywords.preferred_coverage),
    ):
        if matches:
            found = sum(m.found for m in matches)
            lines.append(f"  {labels[key]}: {found}/{len(matches)} ({_percent(coverage)})")
            lines.extend(_match_line(m, labels) for m in matches)
    if keywords.resume_only:
        names = ", ".join(s.name for s in keywords.resume_only)
        lines.append(f"  {labels['resume_only']}: {names}")
    return lines


_MARK: Final = {Verdict.MET: "[x]", Verdict.PARTIAL: "[~]", Verdict.NOT_MET: "[ ]"}
_COMPONENTS: Final = ("required", "preferred", "experience", "keywords", "education")


def _requirement_lines(result: RequirementResult, labels: dict[str, str]) -> list[str]:
    requirement, consensus = result.requirement, result.consensus
    line = f"    {_MARK[consensus.verdict]} {requirement.id} {requirement.text}"
    if requirement.min_years is not None and result.years is not None:
        line += "  (" + labels["years"].format(need=requirement.min_years, have=result.years) + ")"
    if consensus.low_confidence:
        line += f"  [{labels['low_confidence']} {'/'.join(v.value for v in consensus.votes)}]"
    lines = [line]
    lines.extend(f'        {labels["evidence"]}: "{text}"' for text in result.evidence_text)
    if result.rationale:
        lines.append(f"        {labels['reason']}: {result.rationale}")
    return lines


def _ai_lines(report: CheckReport, labels: dict[str, str]) -> list[str]:
    ai = report.ai
    if ai is None:
        return []
    score = ai.score
    names = dict(zip(_COMPONENTS, labels["component_names"].split("|"), strict=True))
    lines = [
        "",
        f"{labels['ai']}: {score.final:.2f} / 1.00  ({score.match:.2f} {labels['ai_before']})",
    ]
    if score.components:
        parts = [f"{names[k]} {v:.2f}" for k, v in score.components.items()]
        lines.append(f"  {labels['components']}: " + " | ".join(parts))
    by_id = {r.requirement.id: r for r in ai.results}
    for requirement_id in score.failed_knockouts:
        lines.append(f"  [!] {labels['knockout']}: {by_id[requirement_id].requirement.text}")
    for key, required in (("required", True), ("preferred", False)):
        group = [r for r in ai.results if r.requirement.required is required]
        if group:
            met = sum(r.consensus.verdict is Verdict.MET for r in group)
            lines.append(f"  {labels[key]}: {met}/{len(group)}")
            for result in group:
                lines.extend(_requirement_lines(result, labels))
    if ai.warnings:
        lines.append(f"  {labels['notes']}:")
        lines.extend(f"    - {warning}" for warning in ai.warnings)
    client = f" ({labels['client']}: {report.client})" if report.client else ""
    lines.append(f"  {labels['cost']}: {ai.cost_usd} USD{client}")
    return lines


def render_text(report: CheckReport, language: Language = "th") -> str:
    labels = _LABELS[language]
    doc = report.document
    unit = labels["page"] if doc.page_count == 1 else labels["pages"]
    pages = f", {doc.page_count} {unit}" if doc.page_count else ""
    lines = [
        f"{labels['title']}: {report.source_name}",
        "=" * 60,
        f"{labels['format']}: {doc.source_format.value.upper()}{pages}",
        "",
        f"{labels['parseability']}: {report.parseability.score:.2f} / 1.00",
    ]
    if not report.parseability.issues:
        lines.append(f"  {labels['no_issues']}")
    for severity in _SEVERITY_ORDER:
        for issue in report.parseability.by_severity(severity):
            problem, fix = render_issue(issue, language)
            lines.append(f"  [{labels[severity.value]}] {problem}")
            lines.append(f"      {labels['fix']}: {fix}")
    sections = sorted(s.value for s in report.parseability.sections_found)
    lines.append(f"  {labels['sections']}: {', '.join(sections) or '-'}")
    lines.extend(_keyword_lines(report.keywords, labels))
    lines.extend(_ai_lines(report, labels))
    lines.extend(["", labels["privacy"]])
    counts = _pii_counts(report.redaction)
    if counts:
        pii_labels = _PII_LABELS[language]
        lines.append("  " + ", ".join(f"{pii_labels[k]} {n}" for k, n in counts.items()))
    else:
        lines.append(f"  {labels['privacy_none']}")
    return "\n".join(lines) + "\n"


# ── JSON ─────────────────────────────────────────────────────────────────


def _match_json(match: SkillMatch) -> dict[str, Any]:
    return {
        "skill_id": match.requirement.skill.id,
        "name": match.requirement.skill.name,
        "importance": match.requirement.importance.value,
        "jd_terms": list(match.requirement.terms),
        "found": match.found,
        "exact_term": match.exact_term,
        "resume_terms": sorted({m.text for m in match.resume_mentions}),
    }


def _ai_json(ai: AiAnalysis | None) -> dict[str, Any] | None:
    if ai is None:
        return None
    return {
        "analysis_id": ai.analysis_id,
        "job_title": ai.job_title,
        "score": ai.score.final,
        "match": ai.score.match,
        "components": dict(ai.score.components),
        "weights_used": dict(ai.score.weights_used),
        "failed_knockouts": list(ai.score.failed_knockouts),
        "total_years": ai.total_years,
        "requirements": [
            {
                "id": r.requirement.id,
                "text": r.requirement.text,
                "kind": r.requirement.kind.value,
                "required": r.requirement.required,
                "min_years": r.requirement.min_years,
                "knockout": r.requirement.knockout,
                "verdict": r.consensus.verdict.value,
                "votes": [v.value for v in r.consensus.votes],
                "low_confidence": r.consensus.low_confidence,
                "downgraded": r.consensus.chosen.downgraded,
                "years": r.years,
                "evidence": list(r.evidence_text),
                "rationale": r.rationale,
            }
            for r in ai.results
        ],
        "warnings": list(ai.warnings),
        "cost_usd": str(ai.cost_usd),
    }


def to_dict(report: CheckReport, language: Language = "th") -> dict[str, Any]:
    """Stable, JSON-serialisable view of the report (no personal values)."""
    keywords = report.keywords
    return {
        "report_version": REPORT_VERSION,
        "source": report.source_name,
        "format": report.document.source_format.value,
        "pages": report.document.page_count,
        "parseability": {
            "score": report.parseability.score,
            "sections_found": sorted(s.value for s in report.parseability.sections_found),
            "issues": [
                {
                    "code": issue.code.value,
                    "severity": issue.severity.value,
                    "problem": render_issue(issue, language)[0],
                    "fix": render_issue(issue, language)[1],
                    "details": dict(issue.details),
                }
                for issue in report.parseability.issues
            ],
        },
        "keywords": None
        if keywords is None
        else {
            "required_coverage": keywords.required_coverage,
            "preferred_coverage": keywords.preferred_coverage,
            "matches": [_match_json(m) for m in keywords.matches],
            "resume_only": [s.id for s in keywords.resume_only],
        },
        "ai": _ai_json(report.ai),
        "client": report.client,
        "privacy": {k.value: n for k, n in _pii_counts(report.redaction).items()},
    }


def render_json(report: CheckReport, language: Language = "th") -> str:
    return json.dumps(to_dict(report, language), ensure_ascii=False, indent=2) + "\n"
