"""The single path from the gateway to the Claude API.

Every call goes through the same steps, in this order:

1. **PII guard (fail closed):** if any outgoing text still contains an e-mail,
   phone number, national ID or profile URL, nothing is sent.
2. **Worst-case budget check:** input tokens are counted exactly
   (``count_tokens``, free); the maximum possible cost is computed for the
   requested model *and* the fallback model, assuming both run to
   ``max_tokens``. If that could exceed today's remaining budget, nothing is sent.
3. **Send** with a single, explicitly priced server-side fallback model, so the
   number of attempts and their prices are bounded.
4. **Record** every attempt in the ledger, billed or not, before the response
   is inspected; refusals and truncated answers are recorded and then raised.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, cast

import anthropic

from ats_core.billing.budget import BudgetDecision, DailyBudget
from ats_core.billing.ledger import CallRecord, Ledger
from ats_core.billing.pricing import PriceTable, Usage
from ats_core.pii import PiiKind, find_residual_pii
from ats_gateway.attempts import billed_attempts

__all__ = [
    "DEFAULT_FALLBACK_MODEL",
    "DEFAULT_MODEL",
    "BudgetExceededError",
    "CallResult",
    "ClaudeCaller",
    "ClaudeRequest",
    "GatewayError",
    "PiiLeakError",
    "RefusedError",
    "TruncatedError",
    "UpstreamError",
]

DEFAULT_MODEL: Final = "claude-opus-5-5"
DEFAULT_FALLBACK_MODEL: Final = "claude-opus-5"
FALLBACK_BETA: Final = "server-side-fallback-2026-07-01"
_EFFORTS: Final = frozenset({"low", "medium", "high", "xhigh", "max"})


class GatewayError(Exception):
    """Base class for errors the gateway reports to its callers."""


class PiiLeakError(GatewayError):
    def __init__(self, kinds: Sequence[PiiKind]) -> None:
        super().__init__("personal data still present: " + ", ".join(k.value for k in kinds))
        self.kinds = tuple(kinds)


class BudgetExceededError(GatewayError):
    def __init__(self, decision: BudgetDecision) -> None:
        super().__init__(
            f"daily budget would be exceeded: spent {decision.spent} of {decision.limit} USD, "
            f"this call could cost up to {decision.requested} USD"
        )
        self.decision = decision


class RefusedError(GatewayError):
    def __init__(self, category: str | None, cost: Decimal) -> None:
        super().__init__(f"the model declined the request (category: {category or 'unspecified'})")
        self.category = category
        self.cost = cost


class TruncatedError(GatewayError):
    def __init__(self, cost: Decimal) -> None:
        super().__init__("the answer was cut off at max_tokens")
        self.cost = cost


class UpstreamError(GatewayError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ClaudeRequest:
    task: str
    system: str
    messages: Sequence[Mapping[str, Any]]
    max_tokens: int
    effort: str = "high"
    output_format: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.task:
            raise ValueError("task is required")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.effort not in _EFFORTS:
            raise ValueError(f"unknown effort {self.effort!r}")
        if not self.messages:
            raise ValueError("at least one message is required")


@dataclass(frozen=True, slots=True)
class CallResult:
    message: Any
    cost_usd: Decimal
    request_id: str | None
    attempts: int


def _texts(value: Any) -> Iterator[str]:
    """Every string inside a message structure (content blocks, nested lists...)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _texts(item)


@dataclass
class ClaudeCaller:
    client: anthropic.Anthropic
    ledger: Ledger
    prices: PriceTable
    budget: DailyBudget = field(default_factory=DailyBudget)
    model: str = DEFAULT_MODEL
    fallback_model: str = DEFAULT_FALLBACK_MODEL
    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        for name in (self.model, self.fallback_model):
            if name not in self.prices:
                raise ValueError(f"model {name!r} has no price; refusing to call it")

    # ── Budget ──────────────────────────────────────────────────────────

    def worst_case_cost(self, input_tokens: int, max_tokens: int) -> Decimal:
        """Upper bound for one call: both attempts, every input token at the
        highest input-side rate, and the full ``max_tokens`` of output each."""
        total = Decimal(0)
        for model in (self.model, self.fallback_model):
            rates = self.prices.prices(model).rates_for(Usage(input_tokens=input_tokens))
            input_rate = max(rates.input, rates.cache_write_5m)
            total += (
                Decimal(input_tokens) * input_rate + Decimal(max_tokens) * rates.output
            ) / Decimal(1_000_000)
        return total

    # ── Call ────────────────────────────────────────────────────────────

    def _guard(self, request: ClaudeRequest) -> None:
        found: list[PiiKind] = []
        for text in (request.system, *_texts(list(request.messages))):
            for kind in find_residual_pii(text):
                if kind not in found:
                    found.append(kind)
        if found:
            raise PiiLeakError(found)

    def _params(self, request: ClaudeRequest) -> dict[str, Any]:
        output_config: dict[str, Any] = {"effort": request.effort}
        if request.output_format is not None:
            output_config["format"] = dict(request.output_format)
        return {
            "model": self.model,
            "max_tokens": request.max_tokens,
            # The system prompt is the stable prefix; cache it (5-minute TTL).
            "system": [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [dict(m) for m in request.messages],
            "output_config": output_config,
        }

    def call(
        self,
        request: ClaudeRequest,
        *,
        analysis_id: str | None = None,
        client_id: str | None = None,
    ) -> CallResult:
        self._guard(request)
        params = self._params(request)
        with self._lock:  # budget check and recording must not interleave
            try:
                counted = self.client.beta.messages.count_tokens(
                    model=params["model"],
                    system=cast(Any, params["system"]),
                    messages=cast(Any, params["messages"]),
                    output_config=cast(Any, params["output_config"]),
                )
            except anthropic.APIError as exc:
                raise UpstreamError(f"token counting failed: {type(exc).__name__}") from exc
            worst = self.worst_case_cost(int(counted.input_tokens), request.max_tokens)
            decision = self.budget.check(self.ledger, worst)
            if not decision.allowed:
                raise BudgetExceededError(decision)

            started = self.clock()
            try:
                message = self.client.beta.messages.create(
                    **cast(Any, params),
                    betas=[FALLBACK_BETA],
                    fallbacks=[{"model": self.fallback_model}],
                )
            except anthropic.APIStatusError as exc:
                raise UpstreamError(
                    f"Claude API error {exc.status_code}", status_code=exc.status_code
                ) from exc
            except anthropic.APIError as exc:
                raise UpstreamError(f"Claude API unreachable: {type(exc).__name__}") from exc
            latency_ms = int((self.clock() - started) * 1000)

            request_id = getattr(message, "_request_id", None)
            total = Decimal(0)
            attempts = billed_attempts(message, self.model, self.prices)
            for number, attempt in enumerate(attempts, start=1):
                cost = self.prices.cost(attempt.model, attempt.usage if attempt.billed else Usage())
                total += cost.total
                self.ledger.record_call(
                    CallRecord(
                        task=request.task,
                        requested_model=self.model,
                        usage=attempt.usage,
                        cost=cost,
                        analysis_id=analysis_id,
                        client_id=client_id,
                        effort=request.effort,
                        latency_ms=latency_ms,
                        stop_reason=message.stop_reason if number == len(attempts) else "refusal",
                        request_id=request_id,
                        attempt=number,
                        iteration_type=attempt.iteration_type,
                        billed=attempt.billed,
                        refusal_category=attempt.refusal_category,
                        note=attempt.note,
                    )
                )

        if message.stop_reason == "refusal":
            raise RefusedError(attempts[-1].refusal_category, total)
        if message.stop_reason == "max_tokens":
            raise TruncatedError(total)
        return CallResult(
            message=message, cost_usd=total, request_id=request_id, attempts=len(attempts)
        )
