"""HTTP interface of the gateway, served only on a private unix socket.

There is deliberately no generic "forward this prompt" endpoint: each endpoint
runs one fixed task with its own versioned prompt and output schema, so a caller
cannot send arbitrary instructions to Claude.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ats_core.billing.budget import DailyBudget
from ats_core.billing.ledger import Ledger, LedgerError
from ats_gateway import __version__
from ats_gateway.caller import (
    BudgetExceededError,
    CallResult,
    ClaudeCaller,
    ClaudeRequest,
    GatewayError,
    PiiLeakError,
    RefusedError,
    TruncatedError,
    UpstreamError,
)
from ats_gateway.tasks import (
    PROMPT_VERSION,
    BulletIn,
    InvalidOutputError,
    JobDescriptionOutput,
    JudgeOutput,
    RequirementIn,
    ResumeStructureOutput,
    judge_request,
    parse_jd_request,
    parse_output,
    structure_request,
)

__all__ = ["GatewayState", "create_app"]

MAX_JD_CHARS = 50_000
MAX_BULLETS = 400


@dataclass
class GatewayState:
    caller: ClaudeCaller
    ledger: Ledger
    budget: DailyBudget


class _Attribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: str | None = None
    client_id: str | None = None


class ParseJdIn(_Attribution):
    jd_text: str = Field(min_length=1, max_length=MAX_JD_CHARS)


class StructureIn(_Attribution):
    bullets: list[BulletIn] = Field(min_length=1, max_length=MAX_BULLETS)


class JudgeIn(_Attribution):
    requirement: RequirementIn
    bullets: list[BulletIn] = Field(min_length=1, max_length=MAX_BULLETS)
    total_years: float | None = Field(default=None, ge=0, le=80)
    language: Literal["th", "en"] = "th"


class _UnknownReferenceError(GatewayError):
    pass


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message, **extra})


def create_app(state: GatewayState) -> FastAPI:
    app = FastAPI(
        title="ats-gateway", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.gateway = state

    @app.exception_handler(GatewayError)
    def gateway_error(_request: Request, exc: GatewayError) -> JSONResponse:
        if isinstance(exc, PiiLeakError):
            return _error(422, "pii", str(exc), kinds=[k.value for k in exc.kinds])
        if isinstance(exc, BudgetExceededError):
            d = exc.decision
            return _error(
                429,
                "budget",
                str(exc),
                spent_usd=str(d.spent),
                limit_usd=str(d.limit),
                needed_usd=str(d.requested),
            )
        if isinstance(exc, RefusedError):
            return _error(422, "refused", str(exc), category=exc.category, cost_usd=str(exc.cost))
        if isinstance(exc, TruncatedError):
            return _error(502, "truncated", str(exc), cost_usd=str(exc.cost))
        if isinstance(exc, _UnknownReferenceError):
            return _error(404, "unknown_reference", str(exc))
        if isinstance(exc, UpstreamError):
            return _error(503 if exc.status_code is None else 502, "upstream", str(exc))
        return _error(500, "gateway", str(exc))  # pragma: no cover - every subclass is handled

    def check_references(body: _Attribution) -> None:
        # Validate before spending money: a bad id would only fail when the
        # call's cost is recorded, after the API has already charged for it.
        if body.client_id is not None:
            try:
                state.ledger.get_client(body.client_id)
            except LedgerError:
                raise _UnknownReferenceError(f"unknown client {body.client_id}") from None
        if body.analysis_id is not None and not state.ledger.analysis_exists(body.analysis_id):
            raise _UnknownReferenceError(f"unknown analysis {body.analysis_id}")

    def run[T: BaseModel](
        body: _Attribution, request: ClaudeRequest, model: type[T]
    ) -> JSONResponse:
        check_references(body)
        result: CallResult = state.caller.call(
            request, analysis_id=body.analysis_id, client_id=body.client_id
        )
        meta = {
            "cost_usd": str(result.cost_usd),
            "attempts": result.attempts,
            "request_id": result.request_id,
            "prompt_version": PROMPT_VERSION,
        }
        try:
            parsed = parse_output(result.message, model)
        except InvalidOutputError as exc:
            return _error(502, "invalid_output", str(exc), **meta)
        return JSONResponse({"result": parsed.model_dump(), **meta})

    @app.get("/health")
    def health() -> dict[str, Any]:
        spent = state.ledger.spent_today()
        return {
            "status": "ok",
            "version": __version__,
            "model": state.caller.model,
            "fallback_model": state.caller.fallback_model,
            "pricing_version": state.caller.prices.version,
            "prompt_version": PROMPT_VERSION,
            "budget": {
                "limit_usd": str(state.budget.limit),
                "spent_today_usd": str(spent),
                "remaining_usd": str(max(state.budget.limit - spent, 0)),
            },
        }

    @app.post("/v1/tasks/parse-jd")
    def parse_jd(body: ParseJdIn) -> JSONResponse:
        return run(body, parse_jd_request(body.jd_text), JobDescriptionOutput)

    @app.post("/v1/tasks/structure-resume")
    def structure_resume(body: StructureIn) -> JSONResponse:
        return run(body, structure_request(body.bullets), ResumeStructureOutput)

    @app.post("/v1/tasks/judge")
    def judge(body: JudgeIn) -> JSONResponse:
        request = judge_request(
            body.requirement, body.bullets, total_years=body.total_years, language=body.language
        )
        return run(body, request, JudgeOutput)

    return app
