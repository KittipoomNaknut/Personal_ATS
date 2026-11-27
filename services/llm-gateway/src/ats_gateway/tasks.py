"""The three AI tasks: prompts, output schemas, request builders and output parsing.

Each task has a versioned prompt file (``prompts/<task>.v1.md``), a JSON schema
enforced by the API (structured outputs) and a pydantic model that validates the
result again, including constraints the API cannot enforce (numeric ranges,
list lengths).

Untrusted text (job descriptions, resumes) never goes into the system prompt:
it is sent in the user message inside clearly named tags, and occurrences of
those tag names inside the text are neutralised so the data cannot close the
tag and pose as instructions.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from functools import cache
from importlib import resources
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ats_gateway.caller import ClaudeRequest

__all__ = [
    "MAX_REQUIREMENTS",
    "BulletIn",
    "InvalidOutputError",
    "JobDescriptionOutput",
    "JudgeOutput",
    "RequirementIn",
    "ResumeStructureOutput",
    "judge_request",
    "parse_jd_request",
    "parse_output",
    "structure_request",
]

PROMPT_VERSION: Final = "v1"
MAX_REQUIREMENTS: Final = 40
MAX_EVIDENCE: Final = 3

_TAGS: Final = ("job_description", "resume", "facts", "requirement")
_TAG_PATTERN: Final = re.compile(rf"<(/?\s*(?:{'|'.join(_TAGS)}))", re.IGNORECASE)

Kind = Literal[
    "skill", "experience", "education", "certification", "language", "soft_skill", "other"
]
_KINDS: Final = [
    "skill",
    "experience",
    "education",
    "certification",
    "language",
    "soft_skill",
    "other",
]


class InvalidOutputError(ValueError):
    """The model's answer did not match the expected structure."""


# ── Output models (validated again after the API's own schema check) ──────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RequirementOut(_Strict):
    text: str = Field(min_length=1)
    kind: Kind
    importance: Literal["required", "preferred"]
    min_years: float | None = Field(ge=0, le=50)
    knockout: bool


class JobDescriptionOutput(_Strict):
    job_title: str
    requirements: list[RequirementOut] = Field(max_length=MAX_REQUIREMENTS)


class JobOut(_Strict):
    title: str
    organization: str
    start: str
    end: str
    bullet_ids: list[str]


class EducationOut(_Strict):
    degree: str
    field: str
    institution: str
    year: str


class LanguageOut(_Strict):
    language: str
    level: str


class ResumeStructureOutput(_Strict):
    jobs: list[JobOut]
    education: list[EducationOut]
    certifications: list[str]
    languages: list[LanguageOut]


class EvidenceOut(_Strict):
    bullet_id: str
    quote: str


class JudgeOutput(_Strict):
    verdict: Literal["met", "partial", "not_met"]
    evidence: list[EvidenceOut] = Field(max_length=MAX_EVIDENCE)
    rationale: str


# ── JSON schemas sent to the API ─────────────────────────────────────────


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_STRING: Final = {"type": "string"}

PARSE_JD_SCHEMA: Final = _object(
    {
        "job_title": _STRING,
        "requirements": {
            "type": "array",
            "items": _object(
                {
                    "text": _STRING,
                    "kind": {"type": "string", "enum": _KINDS},
                    "importance": {"type": "string", "enum": ["required", "preferred"]},
                    "min_years": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "knockout": {"type": "boolean"},
                }
            ),
        },
    }
)

STRUCTURE_SCHEMA: Final = _object(
    {
        "jobs": {
            "type": "array",
            "items": _object(
                {
                    "title": _STRING,
                    "organization": _STRING,
                    "start": _STRING,
                    "end": _STRING,
                    "bullet_ids": {"type": "array", "items": _STRING},
                }
            ),
        },
        "education": {
            "type": "array",
            "items": _object(
                {"degree": _STRING, "field": _STRING, "institution": _STRING, "year": _STRING}
            ),
        },
        "certifications": {"type": "array", "items": _STRING},
        "languages": {"type": "array", "items": _object({"language": _STRING, "level": _STRING})},
    }
)

JUDGE_SCHEMA: Final = _object(
    {
        "verdict": {"type": "string", "enum": ["met", "partial", "not_met"]},
        "evidence": {
            "type": "array",
            "items": _object({"bullet_id": _STRING, "quote": _STRING}),
        },
        "rationale": _STRING,
    }
)


def _json_format(schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "json_schema", "schema": schema}


# ── Inputs ───────────────────────────────────────────────────────────────


class BulletIn(_Strict):
    id: str = Field(pattern=r"^b\d{1,4}$")
    text: str = Field(max_length=2_000)


class RequirementIn(_Strict):
    id: str = Field(min_length=1, max_length=20)
    text: str = Field(min_length=1, max_length=1_000)
    kind: Kind
    importance: Literal["required", "preferred"]
    min_years: float | None = Field(default=None, ge=0, le=50)
    knockout: bool = False


@cache
def system_prompt(task: str) -> str:
    path = resources.files("ats_gateway").joinpath(f"prompts/{task}.{PROMPT_VERSION}.md")
    return path.read_text(encoding="utf-8").strip()


def _neutralise(text: str) -> str:
    """Stop untrusted text from opening or closing our tags (``<`` becomes U+2039)."""
    return _TAG_PATTERN.sub("\u2039\\1", text)


def _resume_block(bullets: Sequence[BulletIn]) -> str:
    lines = "\n".join(f"{b.id}: {_neutralise(b.text)}" for b in bullets)
    return f"<resume>\n{lines}\n</resume>"


def parse_jd_request(jd_text: str) -> ClaudeRequest:
    return ClaudeRequest(
        task=f"parse_jd.{PROMPT_VERSION}",
        system=system_prompt("parse_jd"),
        messages=[
            {
                "role": "user",
                "content": f"<job_description>\n{_neutralise(jd_text)}\n</job_description>",
            }
        ],
        max_tokens=16_000,
        effort="high",
        output_format=_json_format(PARSE_JD_SCHEMA),
    )


def structure_request(bullets: Sequence[BulletIn]) -> ClaudeRequest:
    return ClaudeRequest(
        task=f"structure_resume.{PROMPT_VERSION}",
        system=system_prompt("structure_resume"),
        messages=[{"role": "user", "content": _resume_block(bullets)}],
        max_tokens=16_000,
        effort="medium",
        output_format=_json_format(STRUCTURE_SCHEMA),
    )


def judge_request(
    requirement: RequirementIn,
    bullets: Sequence[BulletIn],
    *,
    total_years: float | None,
    language: Literal["th", "en"],
) -> ClaudeRequest:
    facts = {"total_years_of_experience": total_years}
    detail = {
        "text": requirement.text,
        "kind": requirement.kind,
        "importance": requirement.importance,
        "min_years": requirement.min_years,
    }
    rationale_language = "Thai" if language == "th" else "English"
    return ClaudeRequest(
        task=f"judge.{PROMPT_VERSION}",
        system=system_prompt("judge"),
        messages=[
            {
                "role": "user",
                "content": [
                    # The resume is identical for every requirement of an analysis:
                    # cache it, and put what changes after it.
                    {
                        "type": "text",
                        "text": _resume_block(bullets),
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": (
                            f"<facts>\n{json.dumps(facts, ensure_ascii=False)}\n</facts>\n"
                            "<requirement>\n"
                            f"{_neutralise(json.dumps(detail, ensure_ascii=False))}\n"
                            "</requirement>\n"
                            f"Write the rationale in {rationale_language}."
                        ),
                    },
                ],
            }
        ],
        max_tokens=4_000,
        effort="high",
        output_format=_json_format(JUDGE_SCHEMA),
    )


# ── Output parsing ───────────────────────────────────────────────────────


def _answer_text(message: Any) -> str:
    """Text of the answer: the text blocks after the last fallback switch point."""
    blocks = list(message.content or ())
    last_switch = max(
        (i for i, b in enumerate(blocks) if getattr(b, "type", None) == "fallback"), default=-1
    )
    texts = [b.text for b in blocks[last_switch + 1 :] if getattr(b, "type", None) == "text"]
    if not texts:
        raise InvalidOutputError("the answer contains no text")
    return "".join(texts)


def parse_output[T: BaseModel](message: Any, model: type[T]) -> T:
    try:
        data = json.loads(_answer_text(message))
    except json.JSONDecodeError as exc:
        raise InvalidOutputError("the answer is not valid JSON") from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in error["loc"]) for error in exc.errors()})
        raise InvalidOutputError(f"the answer does not match the schema: {fields}") from exc
