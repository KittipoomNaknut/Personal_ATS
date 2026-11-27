"""HTTP interface of the gateway, served only on a private unix socket.

There is deliberately no generic "forward this prompt" endpoint: task endpoints
(JD parsing, resume structuring, requirement judging) are added with their
prompts and schemas in PR 2.4, so callers cannot send arbitrary text to Claude.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from ats_core.billing.budget import DailyBudget
from ats_core.billing.ledger import Ledger
from ats_gateway import __version__
from ats_gateway.caller import ClaudeCaller

__all__ = ["GatewayState", "create_app"]


@dataclass
class GatewayState:
    caller: ClaudeCaller
    ledger: Ledger
    budget: DailyBudget


def create_app(state: GatewayState) -> FastAPI:
    app = FastAPI(
        title="ats-gateway", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.gateway = state

    @app.get("/health")
    def health() -> dict[str, Any]:
        spent = state.ledger.spent_today()
        return {
            "status": "ok",
            "version": __version__,
            "model": state.caller.model,
            "fallback_model": state.caller.fallback_model,
            "pricing_version": state.caller.prices.version,
            "budget": {
                "limit_usd": str(state.budget.limit),
                "spent_today_usd": str(spent),
                "remaining_usd": str(max(state.budget.limit - spent, 0)),
            },
        }

    return app
