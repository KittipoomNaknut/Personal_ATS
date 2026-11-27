from __future__ import annotations

from decimal import Decimal

import pytest

from ats_core.billing.pricing import PriceTable, PricingError, Usage, default_price_table

TABLE = default_price_table()

# Copied by hand from https://platform.claude.com/docs/en/about-claude/pricing.md
# (2026-11-27): input, 5m cache write, 1h cache write, cache read, output per MTok.
PUBLISHED = {
    "claude-opus-5-5": ("4", "5", "8", "0.20", "20"),
    "claude-opus-5": ("5", "6.25", "10", "0.50", "25"),
    "claude-opus-4-8": ("5", "6.25", "10", "0.50", "25"),
    "claude-sonnet-5-5": ("2", "2.50", "4", "0.10", "10"),
    "claude-fable-5-2": ("8", "10", "16", "0.20", "40"),
    "claude-haiku-5-5": ("0.10", "0.125", "0.20", "0.01", "0.50"),
}
MILLION = 1_000_000


@pytest.mark.parametrize("model", PUBLISHED)
def test_table_matches_published_prices(model: str) -> None:
    # Probe with 100k tokens per category: at, not over, Haiku's long-context
    # threshold, so every model is billed at its standard tier (price / 10).
    tokens = 100_000
    expected = [Decimal(v) / 10 for v in PUBLISHED[model]]
    probes = [
        Usage(input_tokens=tokens),
        Usage(cache_write_5m_tokens=tokens),
        Usage(cache_write_1h_tokens=tokens),
        Usage(cache_read_tokens=tokens),
        Usage(output_tokens=tokens),
    ]
    assert [TABLE.cost(model, u).total for u in probes] == expected


def test_table_has_exactly_the_published_models() -> None:
    assert set(TABLE.models) == set(PUBLISHED)
    assert TABLE.version == "2026-11-27"


def test_opus_cost_matches_hand_calculation() -> None:
    usage = Usage(
        input_tokens=10_000,
        output_tokens=2_000,
        cache_write_5m_tokens=4_000,
        cache_read_tokens=50_000,
    )
    cost = TABLE.cost("claude-opus-5-5", usage)
    # 10k x $4 + 4k x $5 + 50k x $0.20 + 2k x $20, per million tokens
    assert cost.input == Decimal("0.04")
    assert cost.cache_write == Decimal("0.02")
    assert cost.cache_read == Decimal("0.01")
    assert cost.output == Decimal("0.04")
    assert cost.total == Decimal("0.11")
    assert not cost.long_context
    assert cost.model == "claude-opus-5-5"
    assert cost.pricing_version == "2026-11-27"


def test_mixed_cache_write_durations() -> None:
    cost = TABLE.cost(
        "claude-sonnet-5-5", Usage(cache_write_5m_tokens=MILLION, cache_write_1h_tokens=MILLION)
    )
    assert cost.cache_write == Decimal("6.50")


def test_haiku_long_context_tier_boundary() -> None:
    at_threshold = TABLE.cost("claude-haiku-5-5", Usage(input_tokens=100_000))
    over = TABLE.cost("claude-haiku-5-5", Usage(input_tokens=100_001))
    assert at_threshold.total == Decimal("0.01")  # 100k x $0.10
    assert not at_threshold.long_context
    assert over.total == Decimal("0.0500005")  # whole request at $0.50
    assert over.long_context


def test_haiku_tier_counts_cached_prompt_tokens() -> None:
    usage = Usage(input_tokens=1_000, cache_read_tokens=99_001, output_tokens=1_000)
    cost = TABLE.cost("claude-haiku-5-5", usage)
    assert cost.long_context
    assert cost.output == Decimal("0.0025")  # 1k x $2.50, the long-context output price


def test_other_models_have_no_long_context_surcharge() -> None:
    cost = TABLE.cost("claude-opus-5-5", Usage(input_tokens=900_000))
    assert cost.total == Decimal("3.6")
    assert not cost.long_context


def test_us_inference_geo_multiplier() -> None:
    assert TABLE.cost(
        "claude-opus-5-5", Usage(input_tokens=MILLION), inference_geo="us"
    ).total == Decimal("4.4")


def test_decimal_sums_are_exact() -> None:
    # 10,000 tiny calls must add up exactly; binary floats would drift.
    one = TABLE.cost("claude-haiku-5-5", Usage(input_tokens=7, output_tokens=3)).total
    assert sum((one for _ in range(10_000)), Decimal(0)) == one * 10_000
    assert one == Decimal("0.0000022")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"speed": "fast"}, "speed"),
        ({"batch": True}, "Batch"),
        ({"inference_geo": "eu"}, "inference_geo"),
    ],
)
def test_unpublished_prices_are_refused(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(PricingError, match=message):
        TABLE.cost("claude-opus-5-5", Usage(input_tokens=1), **kwargs)  # type: ignore[arg-type]


def test_unknown_model_is_refused() -> None:
    with pytest.raises(PricingError, match="no price for model"):
        TABLE.cost("claude-opus-9", Usage(input_tokens=1))
    assert "claude-opus-9" not in TABLE
    assert "claude-opus-5-5" in TABLE


@pytest.mark.parametrize("value", [-1, 1.5, True, "10"])
def test_usage_rejects_invalid_counts(value: object) -> None:
    with pytest.raises(PricingError, match="non-negative integer"):
        Usage(input_tokens=value)  # type: ignore[arg-type]


# ── Table validation ─────────────────────────────────────────────────────

VALID_MODEL = (
    '[[model]]\nid = "m"\ninput = "1"\ncache_write_5m = "1"\ncache_write_1h = "1"\n'
    'cache_read = "1"\noutput = "1"\n'
)


def table(body: str, header: str = 'version = "v1"\ncurrency = "USD"\n') -> PriceTable:
    return PriceTable.from_toml(header + body)


def test_minimal_table() -> None:
    t = table(VALID_MODEL)
    assert t.cost("m", Usage(output_tokens=MILLION)).total == Decimal(1)
    assert t.prices("m").name == "m"


@pytest.mark.parametrize(
    ("body", "header", "message"),
    [
        (VALID_MODEL, 'version = "v1"\ncurrency = "THB"\n', "currency"),
        (VALID_MODEL, 'currency = "USD"\n', "version"),
        (VALID_MODEL.replace('input = "1"', "input = 1.0"), None, "quoted string"),
        (VALID_MODEL.replace('input = "1"', 'input = "-1"'), None, "non-negative"),
        (VALID_MODEL.replace('input = "1"', 'input = "NaN"'), None, "finite"),
        (VALID_MODEL.replace('input = "1"', 'input = "abc"'), None, "not a number"),
        (VALID_MODEL.replace('output = "1"\n', ""), None, "missing price"),
        (VALID_MODEL + VALID_MODEL, None, "duplicate model"),
        (VALID_MODEL.replace('id = "m"\n', ""), None, "without id"),
    ],
)
def test_invalid_tables_are_rejected(body: str, header: str | None, message: str) -> None:
    with pytest.raises(PricingError, match=message):
        table(body, header) if header is not None else table(body)


@pytest.mark.parametrize(
    ("category", "output_tokens", "billed"),
    [
        ("bio", 0, True),
        ("frontier_llm", 0, True),
        ("reasoning_extraction", 0, True),
        ("cyber", 0, False),
        ("general_harms", 0, False),
        (None, 0, False),
        ("cyber", 12, True),  # mid-stream: input and streamed output are billed
    ],
)
def test_refusal_billing_rules(category: str | None, output_tokens: int, billed: bool) -> None:
    assert TABLE.is_refusal_billed(category, output_tokens=output_tokens) is billed
