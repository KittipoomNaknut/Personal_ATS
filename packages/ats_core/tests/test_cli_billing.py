"""ats costs / ats forget, against a ledger in a temporary data directory."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ats_core.billing.ledger import CallRecord, Ledger
from ats_core.billing.pricing import Usage, default_price_table
from ats_core.cli import EXIT_CANCELLED, EXIT_OK, EXIT_USAGE, main
from ats_core.paths import ledger_path

PRICES = default_price_table()
DEC_1 = datetime(2026, 12, 1, 5, 0, tzinfo=UTC)


def call(
    tokens: int, *, client_id: str | None = None, analysis_id: str | None = None
) -> CallRecord:
    """A judge call billed for ``tokens`` output tokens (5,000 tokens = $0.10)."""
    usage = Usage(output_tokens=tokens)
    return CallRecord(
        task="judge.v1",
        requested_model="claude-opus-5-5",
        usage=usage,
        cost=PRICES.cost("claude-opus-5-5", usage),
        client_id=client_id,
        analysis_id=analysis_id,
    )


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """December: น้องเอ $0.10 (1 analysis), Bee $0.05, own use 2 x $0.10. November: $0.02."""
    environment = {"ATS_DATA_DIR": str(tmp_path / "data"), "HOME": str(tmp_path)}
    with Ledger(ledger_path(environment)) as ledger:
        a = ledger.add_client("น้องเอ")
        b = ledger.add_client("Bee")
        analysis = ledger.start_analysis(client_id=a.id)
        ledger.record_call(call(5_000, client_id=a.id, analysis_id=analysis), now=DEC_1)
        ledger.record_call(call(2_500, client_id=b.id), now=DEC_1)
        ledger.record_call(call(5_000), now=DEC_1)
        ledger.record_call(call(5_000), now=DEC_1)
        ledger.record_call(call(1_000), now=datetime(2026, 11, 15, tzinfo=UTC))
    return environment


def run(env: dict[str, str], *argv: str, confirm: bool = True) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out=out, env=env, confirm=lambda _question: confirm)
    return code, out.getvalue()


def test_costs_table_for_a_month(env: dict[str, str]) -> None:
    code, out = run(env, "costs", "--month", "2026-12")
    assert code == EXIT_OK
    lines = out.splitlines()
    assert lines[0] == "Costs from 2026-12-01 to 2026-12-31"
    assert lines[1].split() == ["client", "analyses", "calls", "cost_usd"]
    rows = {line.split()[0]: line.split()[1:] for line in lines[2:]}
    assert rows["Bee"] == ["0", "1", "0.0500"]
    assert rows["น้องเอ"] == ["1", "1", "0.1000"]
    assert rows["(own"][-1] == "0.2000"  # two calls without a client
    assert rows["TOTAL"] == ["1", "4", "0.3500"]


def test_costs_in_baht_with_markup_as_csv(env: dict[str, str]) -> None:
    code, out = run(
        env,
        "costs",
        "--month",
        "2026-12",
        "--client",
        "น้องเอ",
        "--fx",
        "36.20",
        "--markup",
        "50",
        "--csv",
    )
    assert code == EXIT_OK
    assert out.splitlines() == [
        "client,analyses,calls,cost_usd,cost_thb,total_thb",
        "น้องเอ,1,1,0.1000,3.62,5.43",
        "TOTAL,1,1,0.1000,3.62,5.43",
    ]


def test_costs_date_range(env: dict[str, str]) -> None:
    code, out = run(env, "costs", "--from", "2026-11-01", "--to", "2026-11-30", "--csv")
    assert code == EXIT_OK
    assert out.splitlines()[-1] == "TOTAL,0,1,0.0200"


def test_invoice_snapshot(env: dict[str, str]) -> None:
    code, out = run(
        env,
        "costs",
        "--month",
        "2026-12",
        "--client",
        "Bee",
        "--fx",
        "36",
        "--markup",
        "20",
        "--invoice",
    )
    assert code == EXIT_OK
    assert "for Bee, 2026-12-01 to 2026-12-31: 0.05 USD x 36 THB/USD + 20% = 2.16 THB" in out
    with Ledger(ledger_path(env)) as ledger:
        client = ledger.find_client("Bee")
        assert client is not None
        (invoice,) = ledger.invoices(client.id)
    assert invoice.total_thb == Decimal("2.16")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["costs", "--client", "Nobody"], "no client named"),
        (["costs", "--month", "2026-13"], "invalid --month"),
        (["costs", "--month", "soon"], "invalid --month"),
        (["costs", "--to", "2026-12-01"], "--to needs --from"),
        (["costs", "--fx", "abc"], "invalid --fx"),
        (["costs", "--fx", "-1"], "invalid --fx"),
        (["costs", "--invoice", "--client", "Bee"], "--invoice needs --client and --fx"),
    ],
)
def test_costs_errors(
    env: dict[str, str], argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, *argv)[0] == EXIT_USAGE
    assert message in capsys.readouterr().err


def test_costs_default_is_current_month(env: dict[str, str]) -> None:
    code, out = run(env, "costs")
    first = date.today().replace(day=1)
    assert code == EXIT_OK
    assert out.startswith(f"Costs from {first}")


def test_forget_with_confirmation(env: dict[str, str]) -> None:
    code, out = run(env, "forget", "--client", "น้องเอ")
    assert code == EXIT_OK
    assert "anonymous cost records were kept" in out
    with Ledger(ledger_path(env)) as ledger:
        assert ledger.find_client("น้องเอ") is None
        totals = ledger.costs_by_client(date(2026, 12, 1), date(2026, 12, 31))
    assert sum((t.cost_usd for t in totals), Decimal(0)) == Decimal("0.35")  # costs kept


def test_forget_declined(env: dict[str, str]) -> None:
    assert run(env, "forget", "--client", "Bee", confirm=False)[0] == EXIT_CANCELLED
    with Ledger(ledger_path(env)) as ledger:
        assert ledger.find_client("Bee") is not None


def test_forget_with_yes_skips_the_question(env: dict[str, str]) -> None:
    assert run(env, "forget", "--client", "Bee", "--yes", confirm=False)[0] == EXIT_OK


def test_forget_unknown_client(env: dict[str, str]) -> None:
    assert run(env, "forget", "--client", "Nobody")[0] == EXIT_USAGE


def test_ai_options_need_a_job_description(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "libreoffice" / "en_clean.pdf"
    assert run({}, "check", str(fixture), "--ai")[0] == EXIT_USAGE
    assert "--ai needs a job description" in capsys.readouterr().err
    jd = tmp_path / "jd.txt"
    jd.write_text("Requirements\n- Python")
    assert run({}, "check", str(fixture), "--jd", str(jd), "--client", "A")[0] == EXIT_USAGE
    assert "--client only applies with --ai" in capsys.readouterr().err


def test_ai_without_running_gateway(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ats_core.cli import EXIT_AI_FAILED

    fixture = Path(__file__).parent / "fixtures" / "libreoffice" / "en_clean.pdf"
    jd = tmp_path / "jd.txt"
    jd.write_text("Requirements\n- Python")
    env = {"ATS_RUNTIME_DIR": str(tmp_path / "run"), "ATS_DATA_DIR": str(tmp_path / "data")}
    assert run(env, "check", str(fixture), "--jd", str(jd), "--ai")[0] == EXIT_AI_FAILED
    assert "atsctl start" in capsys.readouterr().err
