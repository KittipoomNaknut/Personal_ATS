from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ats_core.billing.budget import DailyBudget
from ats_core.billing.ledger import _MIGRATIONS, CallRecord, Ledger, LedgerError
from ats_core.billing.pricing import Usage, default_price_table

PRICES = default_price_table()
NOON = datetime(2026, 12, 1, 5, 0, tzinfo=UTC)  # 12:00 in Bangkok
LATEST = len(_MIGRATIONS)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "data" / "ledger.db") as opened:
        yield opened


def call(
    usage: Usage = Usage(input_tokens=10_000, output_tokens=2_000),  # noqa: B008 - frozen
    *,
    analysis_id: str | None = None,
    client_id: str | None = None,
    model: str = "claude-opus-5-5",
    attempt: int = 1,
) -> CallRecord:
    return CallRecord(
        task="judge",
        requested_model="claude-opus-5-5",
        usage=usage,
        cost=PRICES.cost(model, usage),
        analysis_id=analysis_id,
        client_id=client_id,
        effort="high",
        latency_ms=1234,
        stop_reason="end_turn",
        request_id="req_test",
        attempt=attempt,
    )


# ── File and schema ──────────────────────────────────────────────────────


def test_database_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "data" / "ledger.db"
    Ledger(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_world_readable_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    Ledger(path).close()
    os.chmod(path, 0o644)
    with pytest.raises(LedgerError, match="readable by other users"):
        Ledger(path)


def test_migrations_are_applied_once(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with Ledger(path) as first:
        assert first.schema_version == LATEST
        first.add_client("A")
    with Ledger(path) as second:  # reopening must not re-run or wipe anything
        assert second.schema_version == LATEST
        assert [c.nickname for c in second.clients()] == ["A"]


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    Ledger(path).close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(LedgerError, match="newer than this program"):
        Ledger(path)


def test_in_memory_ledger() -> None:
    with Ledger(":memory:") as memory:
        assert memory.schema_version == LATEST


# ── Clients ──────────────────────────────────────────────────────────────


def test_client_nicknames_are_normalised_and_unique(ledger: Ledger) -> None:
    client = ledger.add_client("  น้องเอ   Smith ")
    assert client.nickname == "น้องเอ Smith"
    assert ledger.find_client("น้องเอ smith") == client
    with pytest.raises(LedgerError, match="already exists"):
        ledger.add_client("น้องเอ SMITH")


def test_client_for_creates_once(ledger: Ledger) -> None:
    first = ledger.client_for("Bee")
    assert ledger.client_for("bee") == first
    assert len(ledger.clients()) == 1


@pytest.mark.parametrize("nickname", ["", "   "])
def test_empty_nickname_is_refused(ledger: Ledger, nickname: str) -> None:
    with pytest.raises(LedgerError, match="empty"):
        ledger.add_client(nickname)


def test_unknown_client(ledger: Ledger) -> None:
    with pytest.raises(LedgerError, match="unknown client"):
        ledger.get_client("nope")
    assert ledger.find_client("nobody") is None


def test_naive_timestamps_are_refused(ledger: Ledger) -> None:
    with pytest.raises(LedgerError, match="timezone-aware"):
        ledger.add_client("A", now=datetime(2026, 1, 1))


# ── Calls and spending ───────────────────────────────────────────────────


def test_call_cost_is_stored_exactly(ledger: Ledger) -> None:
    analysis = ledger.start_analysis()
    ledger.record_call(call(analysis_id=analysis), now=NOON)
    ledger.record_call(call(analysis_id=analysis, attempt=2), now=NOON)
    # Each call: 10k x $4 + 2k x $20 per million = $0.08. Retries are billed too.
    assert ledger.analysis_cost(analysis) == Decimal("0.16")


def test_spending_uses_bangkok_days(ledger: Ledger) -> None:
    late_evening = datetime(2026, 12, 1, 16, 59, tzinfo=UTC)  # 23:59 Bangkok, 1 Dec
    after_midnight = datetime(2026, 12, 1, 17, 0, tzinfo=UTC)  # 00:00 Bangkok, 2 Dec
    ledger.record_call(call(), now=late_evening)
    ledger.record_call(call(), now=after_midnight)
    assert ledger.spent_on(date(2026, 12, 1)) == Decimal("0.08")
    assert ledger.spent_on(date(2026, 12, 2)) == Decimal("0.08")
    assert ledger.spent_today(now=after_midnight) == Decimal("0.08")


def test_fallback_model_is_priced_and_recorded(ledger: Ledger) -> None:
    usage = Usage(input_tokens=MILLION_TOKENS, output_tokens=0)
    record = call(usage, model="claude-opus-4-8")  # answered by the fallback model
    ledger.record_call(record, now=NOON)
    assert ledger.spent_on(date(2026, 12, 1)) == Decimal("5")


MILLION_TOKENS = 1_000_000


def test_invalid_attempt(ledger: Ledger) -> None:
    with pytest.raises(LedgerError, match="attempt"):
        ledger.record_call(call(attempt=0))


def test_analysis_status(ledger: Ledger) -> None:
    analysis = ledger.start_analysis()
    ledger.finish_analysis(analysis, "failed")
    with pytest.raises(LedgerError, match="invalid analysis status"):
        ledger.finish_analysis(analysis, "exploded")
    with pytest.raises(LedgerError, match="unknown analysis"):
        ledger.finish_analysis("nope")


def test_analysis_for_unknown_client_is_refused(ledger: Ledger) -> None:
    with pytest.raises(LedgerError, match="unknown client"):
        ledger.start_analysis(client_id="nope")


def test_costs_by_client(ledger: Ledger) -> None:
    a = ledger.add_client("A")
    b = ledger.add_client("B")
    a1 = ledger.start_analysis(client_id=a.id)
    a2 = ledger.start_analysis(client_id=a.id)
    for analysis in (a1, a1, a2):
        ledger.record_call(call(analysis_id=analysis, client_id=a.id), now=NOON)
    ledger.record_call(call(client_id=b.id), now=NOON)
    ledger.record_call(call(), now=NOON)  # own use, no client
    ledger.record_call(call(client_id=a.id), now=datetime(2026, 11, 1, tzinfo=UTC))  # out of range
    totals = {c.nickname: c for c in ledger.costs_by_client(date(2026, 12, 1), date(2026, 12, 31))}
    assert totals["A"].calls == 3
    assert totals["A"].analyses == 2
    assert totals["A"].cost_usd == Decimal("0.24")
    assert totals["B"].cost_usd == Decimal("0.08")
    assert totals[None].client_id is None
    with pytest.raises(LedgerError, match="before start"):
        ledger.costs_by_client(date(2026, 12, 2), date(2026, 12, 1))


# ── Forgetting ───────────────────────────────────────────────────────────


def test_forget_removes_identity_but_keeps_cost(ledger: Ledger) -> None:
    client = ledger.add_client("น้องเอ")
    analysis = ledger.start_analysis(
        client_id=client.id, resume_sha256="ab" * 32, jd_sha256="cd" * 32
    )
    ledger.record_call(call(analysis_id=analysis, client_id=client.id), now=NOON)
    ledger.forget_client(client.id)

    forgotten = ledger.get_client(client.id)
    assert forgotten.forgotten
    assert forgotten.nickname == "[forgotten]"
    assert ledger.find_client("น้องเอ") is None
    row = ledger._conn.execute(
        "SELECT resume_sha256, jd_sha256 FROM analyses WHERE id = ?", (analysis,)
    ).fetchone()
    assert tuple(row) == (None, None)
    (total,) = ledger.costs_by_client(date(2026, 12, 1), date(2026, 12, 1))
    assert total.nickname is None
    assert total.cost_usd == Decimal("0.08")
    # The nickname is free again for a new, unrelated client.
    assert ledger.add_client("น้องเอ").id != client.id


def test_forget_unknown_client(ledger: Ledger) -> None:
    with pytest.raises(LedgerError, match="unknown client"):
        ledger.forget_client("nope")


# ── Invoices ─────────────────────────────────────────────────────────────


def test_invoice_snapshot(ledger: Ledger) -> None:
    client = ledger.add_client("A")
    ledger.record_call(call(client_id=client.id), now=NOON)
    invoice = ledger.create_invoice(
        client.id,
        date(2026, 12, 1),
        date(2026, 12, 31),
        fx_rate=Decimal("36.25"),
        markup_pct=Decimal(50),
    )
    assert invoice.cost_usd == Decimal("0.08")
    assert invoice.total_thb == Decimal("4.35")  # 0.08 x 36.25 x 1.5
    # Later calls must not change an issued invoice.
    ledger.record_call(call(client_id=client.id), now=NOON)
    (stored,) = ledger.invoices(client.id)
    assert stored == invoice


def test_invoice_without_calls_is_zero(ledger: Ledger) -> None:
    client = ledger.add_client("A")
    invoice = ledger.create_invoice(
        client.id, date(2026, 12, 1), date(2026, 12, 1), fx_rate=Decimal(36)
    )
    assert invoice.total_thb == 0


@pytest.mark.parametrize(("fx", "markup"), [(Decimal(0), Decimal(0)), (Decimal(36), Decimal(-1))])
def test_invoice_validation(ledger: Ledger, fx: Decimal, markup: Decimal) -> None:
    client = ledger.add_client("A")
    with pytest.raises(LedgerError, match="fx_rate"):
        ledger.create_invoice(
            client.id, date(2026, 12, 1), date(2026, 12, 1), fx_rate=fx, markup_pct=markup
        )


# ── Budget ───────────────────────────────────────────────────────────────


def test_daily_budget(ledger: Ledger) -> None:
    budget = DailyBudget()  # $2.00
    for _ in range(24):  # 24 x $0.08 = $1.92
        ledger.record_call(call(), now=NOON)
    fits = budget.check(ledger, Decimal("0.08"), now=NOON)
    assert fits.allowed  # exactly reaching the limit is allowed
    assert fits.spent == Decimal("1.92")
    assert fits.remaining == Decimal("0.08")
    too_much = budget.check(ledger, Decimal("0.09"), now=NOON)
    assert not too_much.allowed
    tomorrow = datetime(2026, 12, 2, 5, 0, tzinfo=UTC)
    assert budget.check(ledger, Decimal("1.99"), now=tomorrow).allowed


def test_budget_remaining_never_negative(ledger: Ledger) -> None:
    for _ in range(30):
        ledger.record_call(call(), now=NOON)
    decision = DailyBudget(Decimal(1)).check(ledger, Decimal(0), now=NOON)
    assert not decision.allowed
    assert decision.remaining == 0


def test_budget_validation(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        DailyBudget(Decimal(-1))
    with pytest.raises(ValueError, match="must not be negative"):
        DailyBudget().check(ledger, Decimal(-1))


def test_v1_database_upgrades_without_losing_data(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    path.touch(mode=0o600)
    conn = sqlite3.connect(path)
    for statement in filter(str.strip, _MIGRATIONS[0].split(";")):
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        "INSERT INTO llm_calls (task, requested_model, model, input_tokens, output_tokens, "
        "cache_write_5m_tokens, cache_write_1h_tokens, cache_read_tokens, cost_usd, "
        "pricing_version, created_at) VALUES ('judge', 'm', 'm', 1, 1, 0, 0, 0, '0.08', 'v', ?)",
        (NOON.isoformat(),),
    )
    conn.commit()
    conn.close()
    with Ledger(path) as upgraded:
        assert upgraded.schema_version == LATEST
        assert upgraded.spent_on(date(2026, 12, 1)) == Decimal("0.08")
        row = upgraded._conn.execute("SELECT iteration_type, billed FROM llm_calls").fetchone()
        assert tuple(row) == ("message", 1)


def test_unbilled_attempt_is_recorded_at_zero_cost(ledger: Ledger) -> None:
    usage = Usage(input_tokens=5_000)
    zero = PRICES.cost("claude-opus-5-5", Usage())
    record = CallRecord(
        task="judge",
        requested_model="claude-opus-5-5",
        usage=usage,
        cost=zero,
        billed=False,
        refusal_category="cyber",
        iteration_type="message",
    )
    ledger.record_call(record, now=NOON)
    assert ledger.spent_on(date(2026, 12, 1)) == 0
    row = ledger._conn.execute(
        "SELECT input_tokens, billed, refusal_category FROM llm_calls"
    ).fetchone()
    assert tuple(row) == (5_000, 0, "cyber")  # tokens kept for analysis, not charged


def test_unbilled_attempt_must_have_zero_cost() -> None:
    with pytest.raises(LedgerError, match="zero cost"):
        CallRecord(
            task="judge",
            requested_model="claude-opus-5-5",
            usage=Usage(input_tokens=1),
            cost=PRICES.cost("claude-opus-5-5", Usage(input_tokens=1)),
            billed=False,
        )


def test_ledger_is_safe_across_threads(ledger: Ledger) -> None:
    # The gateway serves requests from a thread pool that shares one ledger.
    import threading

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(25):
                ledger.record_call(call(), now=NOON)
                ledger.spent_on(date(2026, 12, 1))
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert ledger.spent_on(date(2026, 12, 1)) == Decimal("0.08") * 200
