from __future__ import annotations

from decimal import Decimal

from ats_gateway.attempts import billed_attempts
from fakes import fallback_block, iteration, message, usage

from ats_core.billing.pricing import Usage, default_price_table

PRICES = default_price_table()
OPUS = "claude-opus-5-5"


def test_plain_message_is_one_billed_attempt() -> None:
    (attempt,) = billed_attempts(message(), OPUS, PRICES)
    assert attempt.model == OPUS
    assert attempt.usage == Usage(input_tokens=10_000, output_tokens=2_000)
    assert attempt.billed
    assert not attempt.declined


def test_cache_ttl_breakdown_is_used() -> None:
    body = usage(
        100,
        10,
        cache_write=3_000,
        cache_read=7,
        cache_creation={"ephemeral_5m_input_tokens": 1_000, "ephemeral_1h_input_tokens": 2_000},
    )
    (attempt,) = billed_attempts(message(usage_json=body), OPUS, PRICES)
    assert attempt.usage == Usage(
        input_tokens=100,
        output_tokens=10,
        cache_write_5m_tokens=1_000,
        cache_write_1h_tokens=2_000,
        cache_read_tokens=7,
    )


def test_cache_write_without_breakdown_is_five_minutes() -> None:
    (attempt,) = billed_attempts(message(usage_json=usage(1, 1, cache_write=500)), OPUS, PRICES)
    assert attempt.usage.cache_write_5m_tokens == 500
    assert attempt.usage.cache_write_1h_tokens == 0


def fallback_message(
    category: str | None, declined_output: int = 0, with_block: bool = True
) -> object:
    content = [fallback_block(category)] if with_block else []
    return message(
        model="claude-opus-5",
        content=[*content, {"type": "text", "text": "ok"}],
        # Top-level usage repeats only the serving attempt and must not be added.
        usage_json=usage(
            412,
            264,
            iterations=[
                iteration("message", OPUS, 535, declined_output),
                iteration("fallback_message", "claude-opus-5", 412, 264),
            ],
        ),
    )


def test_pre_output_cyber_decline_is_not_billed() -> None:
    declined, served = billed_attempts(fallback_message("cyber"), OPUS, PRICES)
    assert declined.model == OPUS
    assert declined.declined
    assert declined.refusal_category == "cyber"
    assert not declined.billed
    assert declined.usage.input_tokens == 535  # still reported, for analysis
    assert served.model == "claude-opus-5"
    assert served.iteration_type == "fallback_message"
    assert served.billed
    assert served.usage == Usage(input_tokens=412, output_tokens=264)


def test_pre_output_bio_decline_is_billed() -> None:
    declined, _ = billed_attempts(fallback_message("bio"), OPUS, PRICES)
    assert declined.billed
    assert declined.refusal_category == "bio"


def test_mid_stream_decline_is_billed_in_any_category() -> None:
    declined, _ = billed_attempts(fallback_message("cyber", declined_output=40), OPUS, PRICES)
    assert declined.billed


def test_unknown_decline_category_is_billed_and_flagged() -> None:
    declined, _ = billed_attempts(fallback_message("cyber", with_block=False), OPUS, PRICES)
    assert declined.billed
    assert declined.note == "refusal category unknown; billed"


def test_null_category_is_known_and_not_billed() -> None:
    declined, _ = billed_attempts(fallback_message(None), OPUS, PRICES)
    assert not declined.billed
    assert declined.note is None


def test_whole_chain_refused() -> None:
    refused = message(
        model="claude-opus-5",
        stop_reason="refusal",
        content=[fallback_block("frontier_llm")],
        stop_details={"type": "refusal", "category": "general_harms", "explanation": None},
        usage_json=usage(
            300,
            0,
            iterations=[
                iteration("message", OPUS, 300),
                iteration("fallback_message", "claude-opus-5", 300),
            ],
        ),
    )
    first, second = billed_attempts(refused, OPUS, PRICES)
    assert (first.refusal_category, first.billed) == ("frontier_llm", True)
    assert (second.refusal_category, second.billed) == ("general_harms", False)


def test_refusal_without_iterations() -> None:
    refused = message(
        stop_reason="refusal",
        content=[],
        stop_details={"type": "refusal", "category": "reasoning_extraction"},
        usage_json=usage(200, 0),
    )
    (attempt,) = billed_attempts(refused, OPUS, PRICES)
    assert attempt.declined
    assert attempt.billed  # reasoning_extraction is billed before output


def test_iteration_without_model_uses_requested_then_served_model() -> None:
    msg = message(
        model="claude-opus-5",
        content=[fallback_block("cyber"), {"type": "text", "text": "ok"}],
        usage_json=usage(
            1,
            1,
            iterations=[iteration("message", None, 10), iteration("message", None, 20, 5)],
        ),
    )
    first, second = billed_attempts(msg, OPUS, PRICES)
    assert first.model == OPUS
    assert second.model == "claude-opus-5"


def test_fallback_billing_end_to_end_cost() -> None:
    attempts = billed_attempts(fallback_message("cyber"), OPUS, PRICES)
    total = sum(
        (PRICES.cost(a.model, a.usage if a.billed else Usage()).total for a in attempts), Decimal(0)
    )
    # Only the Opus 5 attempt: 412 x $5 + 264 x $25 per million tokens.
    assert total == Decimal("0.00866")
