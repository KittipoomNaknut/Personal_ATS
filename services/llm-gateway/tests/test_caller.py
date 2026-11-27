from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from ats_gateway.caller import (
    BudgetExceededError,
    ClaudeCaller,
    ClaudeRequest,
    PiiLeakError,
    RefusedError,
    TruncatedError,
    UpstreamError,
)
from fakes import FAKE_KEY, FakeApi, fallback_block, iteration, message_json, usage

from ats_core.billing.budget import DailyBudget
from ats_core.billing.ledger import CallRecord, Ledger
from ats_core.billing.pricing import Usage, default_price_table
from ats_core.pii import PiiKind

PRICES = default_price_table()
REQUEST = ClaudeRequest(
    task="judge",
    system="You judge resume requirements.",
    messages=[{"role": "user", "content": "Requirement: Python. Resume: [NAME_1] used Python."}],
    max_tokens=2_000,
    output_format={"type": "json_schema", "schema": {"type": "object"}},
)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "ledger.db") as opened:
        yield opened


def caller_for(api: FakeApi, ledger: Ledger, **kwargs: object) -> ClaudeCaller:
    return ClaudeCaller(client=api.client(), ledger=ledger, prices=PRICES, **kwargs)  # type: ignore[arg-type]


def rows(ledger: Ledger) -> list[dict[str, object]]:
    return [dict(r) for r in ledger._conn.execute("SELECT * FROM llm_calls ORDER BY id")]


# ── Happy path ───────────────────────────────────────────────────────────


def test_successful_call_is_recorded(ledger: Ledger) -> None:
    api = FakeApi()
    api.reply(message_json(usage_json=usage(10_000, 2_000, cache_write=4_000, cache_read=50_000)))
    result = caller_for(api, ledger).call(REQUEST, client_id=None)

    assert result.cost_usd == Decimal("0.11")
    assert result.request_id == "req_fake"
    assert result.attempts == 1
    (row,) = rows(ledger)
    assert row["task"] == "judge"
    assert row["model"] == "claude-opus-5-5"
    assert row["cost_usd"] == "0.11"
    assert row["billed"] == 1
    assert row["request_id"] == "req_fake"
    assert row["stop_reason"] == "end_turn"
    assert ledger.spent_today() == Decimal("0.11")


def test_request_shape(ledger: Ledger) -> None:
    api = FakeApi()
    api.reply(message_json())
    caller_for(api, ledger).call(REQUEST)
    (body,) = api.created
    create_request = next(r for r in api.requests if r.url.path.endswith("/v1/messages"))
    assert create_request.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert create_request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == [{"model": "claude-opus-5"}]
    assert body["system"] == [
        {
            "type": "text",
            "text": "You judge resume requirements.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert body["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    assert body["max_tokens"] == 2_000
    # Token counting happens first, without the fallback beta.
    assert api.requests[0].url.path.endswith("/count_tokens")


def test_fallback_attempts_are_recorded_separately(ledger: Ledger) -> None:
    api = FakeApi()
    api.reply(
        message_json(
            model="claude-opus-5",
            content=[fallback_block("cyber"), {"type": "text", "text": "{}"}],
            usage_json=usage(
                412,
                264,
                iterations=[
                    iteration("message", "claude-opus-5-5", 535),
                    iteration("fallback_message", "claude-opus-5", 412, 264),
                ],
            ),
        )
    )
    result = caller_for(api, ledger).call(REQUEST)
    first, second = rows(ledger)
    assert (first["model"], first["billed"], first["refusal_category"]) == (
        "claude-opus-5-5",
        0,
        "cyber",
    )
    assert Decimal(str(first["cost_usd"])) == 0
    assert first["input_tokens"] == 535
    assert (second["model"], second["iteration_type"], second["attempt"]) == (
        "claude-opus-5",
        "fallback_message",
        2,
    )
    assert result.cost_usd == Decimal("0.00866")
    assert result.attempts == 2


# ── Guards ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("content", "kind"),
    [
        ("Contact somchai@example.com", PiiKind.EMAIL),
        ([{"type": "text", "text": "call 081-234-5678"}], PiiKind.PHONE),
        ([{"type": "text", "text": "see github.com/somchai"}], PiiKind.PROFILE_URL),
    ],
)
def test_pii_is_never_sent(ledger: Ledger, content: object, kind: PiiKind) -> None:
    api = FakeApi()
    request = ClaudeRequest(
        task="judge", system="s", messages=[{"role": "user", "content": content}], max_tokens=10
    )
    with pytest.raises(PiiLeakError) as info:
        caller_for(api, ledger).call(request)
    assert kind in info.value.kinds
    assert api.requests == []  # not even token counting
    assert rows(ledger) == []


def test_pii_in_system_prompt_is_blocked(ledger: Ledger) -> None:
    api = FakeApi()
    request = ClaudeRequest(
        task="t",
        system="mail me at a@example.com",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=10,
    )
    with pytest.raises(PiiLeakError):
        caller_for(api, ledger).call(request)
    assert api.requests == []


def test_budget_uses_worst_case_and_blocks_before_sending(ledger: Ledger) -> None:
    api = FakeApi(counted_tokens=5_000)
    caller = caller_for(api, ledger)
    # Worst case: Opus 5.5 (5k x $5 + 2k x $20 = $0.065)
    #           + Opus 5   (5k x $6.25 + 2k x $25 = $0.08125), per million tokens.
    assert caller.worst_case_cost(5_000, 2_000) == Decimal("0.14625")
    spent = PRICES.cost("claude-opus-5-5", Usage(output_tokens=93_000))  # $1.86
    ledger.record_call(
        CallRecord(task="x", requested_model="m", usage=Usage(output_tokens=93_000), cost=spent)
    )
    with pytest.raises(BudgetExceededError) as info:  # 1.86 + 0.14625 > 2.00
        caller.call(REQUEST)
    assert info.value.decision.spent == Decimal("1.86")
    assert api.created == []  # counted, but never sent


def test_budget_allows_call_that_fits(ledger: Ledger) -> None:
    api = FakeApi(counted_tokens=5_000)
    api.reply(message_json())
    caller_for(api, ledger, budget=DailyBudget(Decimal("0.14625"))).call(REQUEST)
    assert len(api.created) == 1


def test_unpriced_models_are_refused(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="no price"):
        caller_for(FakeApi(), ledger, fallback_model="claude-opus-9")


# ── Failures ─────────────────────────────────────────────────────────────


def test_refusal_is_recorded_then_raised(ledger: Ledger) -> None:
    api = FakeApi()
    api.reply(
        message_json(
            stop_reason="refusal",
            content=[],
            stop_details={"type": "refusal", "category": "bio"},
            usage_json=usage(800, 0),
        )
    )
    with pytest.raises(RefusedError) as info:
        caller_for(api, ledger).call(REQUEST)
    assert info.value.category == "bio"
    assert info.value.cost == Decimal("0.0032")  # bio declines are billed: 800 x $4
    (row,) = rows(ledger)
    assert (row["billed"], row["stop_reason"]) == (1, "refusal")


def test_truncated_answer_is_recorded_then_raised(ledger: Ledger) -> None:
    api = FakeApi()
    api.reply(message_json(stop_reason="max_tokens", usage_json=usage(100, 2_000)))
    with pytest.raises(TruncatedError) as info:
        caller_for(api, ledger).call(REQUEST)
    assert info.value.cost == Decimal("0.0404")
    assert len(rows(ledger)) == 1


def test_api_error_records_nothing(ledger: Ledger) -> None:
    api = FakeApi()
    api.reply({"type": "error", "error": {"type": "api_error", "message": "boom"}}, status=500)
    with pytest.raises(UpstreamError) as info:
        caller_for(api, ledger).call(REQUEST)
    assert info.value.status_code == 500
    assert rows(ledger) == []


def test_connection_error(ledger: Ledger) -> None:
    api = FakeApi()

    def drop_create(request: httpx2.Request) -> None:
        if request.url.path.endswith("/v1/messages"):
            raise httpx2.ConnectError("no route", request=request)

    caller = ClaudeCaller(client=api.client(drop_create), ledger=ledger, prices=PRICES)
    with pytest.raises(UpstreamError, match="unreachable"):
        caller.call(REQUEST)


def test_token_counting_failure(ledger: Ledger) -> None:
    api = FakeApi()

    def fail_count(request: httpx2.Request) -> None:
        raise httpx2.ConnectError("down", request=request)

    with pytest.raises(UpstreamError, match="token counting failed"):
        ClaudeCaller(client=api.client(fail_count), ledger=ledger, prices=PRICES).call(REQUEST)


def test_error_messages_never_contain_the_key(ledger: Ledger) -> None:
    api = FakeApi()
    api.reply(
        {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}},
        status=401,
    )
    with pytest.raises(UpstreamError) as info:
        caller_for(api, ledger).call(REQUEST)
    assert FAKE_KEY not in str(info.value)
    assert FAKE_KEY not in json.dumps(rows(ledger))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"task": ""}, "task"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"effort": "extreme"}, "effort"),
        ({"messages": []}, "message"),
    ],
)
def test_request_validation(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {
        "task": "t",
        "system": "s",
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 1,
    }
    with pytest.raises(ValueError, match=message):
        ClaudeRequest(**{**base, **kwargs})  # type: ignore[arg-type]


def test_latency_is_recorded(ledger: Ledger) -> None:
    api = FakeApi()
    api.reply(message_json())
    ticks = iter([10.0, 11.5])
    ClaudeCaller(client=api.client(), ledger=ledger, prices=PRICES, clock=lambda: next(ticks)).call(
        REQUEST
    )
    assert rows(ledger)[0]["latency_ms"] == 1500


def test_calls_are_attributed(ledger: Ledger) -> None:
    client = ledger.add_client("A")
    analysis = ledger.start_analysis(client_id=client.id)
    api = FakeApi()
    api.reply(message_json())
    caller_for(api, ledger).call(REQUEST, analysis_id=analysis, client_id=client.id)
    assert ledger.analysis_cost(analysis) == Decimal("0.08")
    (total,) = ledger.costs_by_client(datetime.now(UTC).date(), datetime.now(UTC).date())
    assert total.nickname == "A"


def test_request_without_output_format_and_with_non_text_blocks(ledger: Ledger) -> None:
    api = FakeApi()
    api.reply(message_json())
    request = ClaudeRequest(
        task="t",
        system="s",
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "hello"}, {"n": 3, "flag": True}]}
        ],
        max_tokens=10,
    )
    caller_for(api, ledger).call(request)
    (body,) = api.created
    assert body["output_config"] == {"effort": "high"}
