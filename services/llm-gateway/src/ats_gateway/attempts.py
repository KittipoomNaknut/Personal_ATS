"""Turn an API response into the attempts that were actually billed.

With server-side fallback one request can run two models. Per the Anthropic
docs (refusals-and-fallback), ``usage.iterations`` is the per-attempt record of
what is billed; the top-level ``usage`` covers only the attempt that produced
the returned message, so it must not be added on top.

A declined attempt is billed when it declined mid-stream (it produced output)
or when its refusal category is one the price table lists as billed. If the
category of a declined attempt cannot be determined, it is billed and flagged,
so client invoices err toward the real charge and monthly reconciliation can
correct it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ats_core.billing.pricing import PriceTable, Usage

__all__ = ["Attempt", "billed_attempts"]


@dataclass(frozen=True, slots=True)
class Attempt:
    model: str
    usage: Usage
    iteration_type: str
    declined: bool
    refusal_category: str | None
    billed: bool
    note: str | None = None


def _usage(source: Any) -> Usage:
    """Usage from a top-level ``usage`` or an iteration entry.

    The gateway only requests the default 5-minute cache TTL, so cache writes
    without a TTL breakdown are 5-minute writes.
    """
    breakdown = getattr(source, "cache_creation", None)
    if breakdown is not None:
        write_5m = int(breakdown.ephemeral_5m_input_tokens or 0)
        write_1h = int(breakdown.ephemeral_1h_input_tokens or 0)
    else:
        write_5m = int(source.cache_creation_input_tokens or 0)
        write_1h = 0
    return Usage(
        input_tokens=int(source.input_tokens or 0),
        output_tokens=int(source.output_tokens or 0),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        cache_read_tokens=int(source.cache_read_input_tokens or 0),
    )


def _final_refusal_category(message: Any) -> str | None:
    details = getattr(message, "stop_details", None)
    return getattr(details, "category", None) if details is not None else None


def _fallback_categories(message: Any) -> list[str | None]:
    """Refusal categories of the switch points, in order."""
    categories: list[str | None] = []
    for block in message.content or ():
        if getattr(block, "type", None) == "fallback":
            trigger = getattr(block, "trigger", None)
            categories.append(getattr(trigger, "category", None))
    return categories


def _attempt(
    prices: PriceTable,
    *,
    model: str,
    usage: Usage,
    iteration_type: str,
    declined: bool,
    category: str | None,
    category_known: bool,
) -> Attempt:
    if not declined:
        return Attempt(model, usage, iteration_type, False, None, True)
    if not category_known:
        return Attempt(
            model, usage, iteration_type, True, None, True, "refusal category unknown; billed"
        )
    billed = prices.is_refusal_billed(category, output_tokens=usage.output_tokens)
    return Attempt(model, usage, iteration_type, True, category, billed)


def billed_attempts(message: Any, requested_model: str, prices: PriceTable) -> list[Attempt]:
    """Every attempt in ``message`` with its usage and whether it is charged."""
    refused = message.stop_reason == "refusal"
    iterations: Sequence[Any] = getattr(message.usage, "iterations", None) or ()
    if not iterations:
        return [
            _attempt(
                prices,
                model=str(message.model),
                usage=_usage(message.usage),
                iteration_type="message",
                declined=refused,
                category=_final_refusal_category(message) if refused else None,
                category_known=True,
            )
        ]

    switch_categories = _fallback_categories(message)
    attempts: list[Attempt] = []
    last = len(iterations) - 1
    for index, iteration in enumerate(iterations):
        is_final = index == last
        model = getattr(iteration, "model", None) or (
            requested_model if index == 0 else str(message.model)
        )
        if is_final:
            declined = refused
            category = _final_refusal_category(message) if refused else None
            known = True
        else:
            # Every earlier attempt declined and handed over to a fallback model.
            declined = True
            known = index < len(switch_categories)
            category = switch_categories[index] if known else None
        attempts.append(
            _attempt(
                prices,
                model=str(model),
                usage=_usage(iteration),
                iteration_type=str(getattr(iteration, "type", "message")),
                declined=declined,
                category=category,
                category_known=known,
            )
        )
    return attempts
