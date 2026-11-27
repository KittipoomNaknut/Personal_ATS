"""Cost ledger: clients, analyses and every Claude call, in a local SQLite file.

Money is stored as TEXT and summed in Python with :class:`Decimal` (SQLite's
``SUM`` would go through binary floating point). The database file holds client
nicknames, so it is created with owner-only permissions. Nothing in it is
resume, job-description, prompt or response text.

Schema changes are numbered migrations tracked with ``PRAGMA user_version``.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from ats_core.billing.pricing import CostBreakdown, Usage

__all__ = [
    "BILLING_TZ",
    "CallRecord",
    "Client",
    "ClientCost",
    "Invoice",
    "Ledger",
    "LedgerError",
]

BILLING_TZ: Final = ZoneInfo("Asia/Bangkok")
_FORGOTTEN: Final = "[forgotten]"

_MIGRATIONS: Final[tuple[str, ...]] = (
    # 1: initial schema
    """
    CREATE TABLE clients (
        id TEXT PRIMARY KEY,
        nickname TEXT,
        nickname_key TEXT UNIQUE,
        created_at TEXT NOT NULL,
        forgotten_at TEXT
    );
    CREATE TABLE analyses (
        id TEXT PRIMARY KEY,
        client_id TEXT REFERENCES clients(id),
        created_at TEXT NOT NULL,
        resume_sha256 TEXT,
        jd_sha256 TEXT,
        status TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running', 'done', 'failed', 'cancelled'))
    );
    CREATE TABLE llm_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        analysis_id TEXT REFERENCES analyses(id),
        client_id TEXT REFERENCES clients(id),
        task TEXT NOT NULL,
        requested_model TEXT NOT NULL,
        model TEXT NOT NULL,
        effort TEXT,
        input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL,
        cache_write_5m_tokens INTEGER NOT NULL,
        cache_write_1h_tokens INTEGER NOT NULL,
        cache_read_tokens INTEGER NOT NULL,
        cost_usd TEXT NOT NULL,
        pricing_version TEXT NOT NULL,
        latency_ms INTEGER,
        stop_reason TEXT,
        request_id TEXT,
        attempt INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE INDEX llm_calls_created_at ON llm_calls(created_at);
    CREATE INDEX llm_calls_client ON llm_calls(client_id);
    CREATE TABLE invoices (
        id TEXT PRIMARY KEY,
        client_id TEXT REFERENCES clients(id),
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        cost_usd TEXT NOT NULL,
        fx_rate TEXT NOT NULL,
        markup_pct TEXT NOT NULL,
        total_thb TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
)


class LedgerError(Exception):
    """Invalid ledger operation (unknown client, duplicate nickname, bad values)."""


@dataclass(frozen=True, slots=True)
class Client:
    id: str
    nickname: str
    created_at: datetime
    forgotten: bool


@dataclass(frozen=True, slots=True)
class CallRecord:
    task: str
    requested_model: str
    usage: Usage
    cost: CostBreakdown
    analysis_id: str | None = None
    client_id: str | None = None
    effort: str | None = None
    latency_ms: int | None = None
    stop_reason: str | None = None
    request_id: str | None = None
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class ClientCost:
    client_id: str | None
    nickname: str | None
    calls: int
    analyses: int
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class Invoice:
    id: str
    client_id: str
    period_start: date
    period_end: date
    cost_usd: Decimal
    fx_rate: Decimal
    markup_pct: Decimal
    total_thb: Decimal


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise LedgerError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _key(nickname: str) -> str:
    return " ".join(nickname.split()).casefold()


def _day_bounds(day: date, tz: ZoneInfo) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return _iso(start), _iso(start + timedelta(days=1))


class Ledger:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self._prepare_file()
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(self.path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # ── Setup ───────────────────────────────────────────────────────────

    def _prepare_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise LedgerError(
                f"{self.path} is readable by other users (mode {mode:o}); "
                f"run: chmod 600 {self.path}"
            )

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > len(_MIGRATIONS):
            raise LedgerError(f"ledger schema {version} is newer than this program")
        for number, script in enumerate(_MIGRATIONS[version:], start=version + 1):
            with self._transaction():
                for statement in filter(str.strip, script.split(";")):
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version = {number}")

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ── Clients ─────────────────────────────────────────────────────────

    def _client(self, row: sqlite3.Row) -> Client:
        return Client(
            id=row["id"],
            nickname=row["nickname"] or _FORGOTTEN,
            created_at=datetime.fromisoformat(row["created_at"]),
            forgotten=row["forgotten_at"] is not None,
        )

    def add_client(self, nickname: str, *, now: datetime | None = None) -> Client:
        name = " ".join(nickname.split())
        if not name:
            raise LedgerError("client nickname must not be empty")
        client_id = uuid.uuid4().hex
        try:
            with self._transaction():
                self._conn.execute(
                    "INSERT INTO clients (id, nickname, nickname_key, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (client_id, name, _key(name), _iso(now or _now())),
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"client {name!r} already exists") from exc
        return self.get_client(client_id)

    def get_client(self, client_id: str) -> Client:
        row = self._conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if row is None:
            raise LedgerError(f"unknown client {client_id}")
        return self._client(row)

    def find_client(self, nickname: str) -> Client | None:
        row = self._conn.execute(
            "SELECT * FROM clients WHERE nickname_key = ?", (_key(nickname),)
        ).fetchone()
        return self._client(row) if row else None

    def client_for(self, nickname: str) -> Client:
        """The client with this nickname, created if needed."""
        return self.find_client(nickname) or self.add_client(nickname)

    def clients(self) -> list[Client]:
        rows = self._conn.execute("SELECT * FROM clients ORDER BY created_at").fetchall()
        return [self._client(row) for row in rows]

    def forget_client(self, client_id: str, *, now: datetime | None = None) -> None:
        """Remove the client's name and document fingerprints; keep anonymous cost rows."""
        self.get_client(client_id)
        with self._transaction():
            self._conn.execute(
                "UPDATE clients SET nickname = NULL, nickname_key = NULL, forgotten_at = ? "
                "WHERE id = ?",
                (_iso(now or _now()), client_id),
            )
            self._conn.execute(
                "UPDATE analyses SET resume_sha256 = NULL, jd_sha256 = NULL WHERE client_id = ?",
                (client_id,),
            )

    # ── Analyses and calls ──────────────────────────────────────────────

    def start_analysis(
        self,
        *,
        client_id: str | None = None,
        resume_sha256: str | None = None,
        jd_sha256: str | None = None,
        now: datetime | None = None,
    ) -> str:
        if client_id is not None:
            self.get_client(client_id)
        analysis_id = uuid.uuid4().hex
        with self._transaction():
            self._conn.execute(
                "INSERT INTO analyses (id, client_id, created_at, resume_sha256, jd_sha256) "
                "VALUES (?, ?, ?, ?, ?)",
                (analysis_id, client_id, _iso(now or _now()), resume_sha256, jd_sha256),
            )
        return analysis_id

    def finish_analysis(self, analysis_id: str, status: str = "done") -> None:
        try:
            with self._transaction():
                cursor = self._conn.execute(
                    "UPDATE analyses SET status = ? WHERE id = ?", (status, analysis_id)
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"invalid analysis status {status!r}") from exc
        if cursor.rowcount == 0:
            raise LedgerError(f"unknown analysis {analysis_id}")

    def record_call(self, record: CallRecord, *, now: datetime | None = None) -> int:
        if record.attempt < 1:
            raise LedgerError("attempt starts at 1")
        usage = record.usage
        with self._transaction():
            cursor = self._conn.execute(
                """
                INSERT INTO llm_calls (
                    analysis_id, client_id, task, requested_model, model, effort,
                    input_tokens, output_tokens, cache_write_5m_tokens, cache_write_1h_tokens,
                    cache_read_tokens, cost_usd, pricing_version, latency_ms, stop_reason,
                    request_id, attempt, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.analysis_id,
                    record.client_id,
                    record.task,
                    record.requested_model,
                    record.cost.model,
                    record.effort,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_write_5m_tokens,
                    usage.cache_write_1h_tokens,
                    usage.cache_read_tokens,
                    str(record.cost.total),
                    record.cost.pricing_version,
                    record.latency_ms,
                    record.stop_reason,
                    record.request_id,
                    record.attempt,
                    _iso(now or _now()),
                ),
            )
        return int(cursor.lastrowid or 0)

    # ── Reporting ───────────────────────────────────────────────────────

    def _sum(self, sql: str, params: Sequence[object]) -> Decimal:
        return sum((Decimal(row[0]) for row in self._conn.execute(sql, params)), start=Decimal(0))

    def spent_on(self, day: date, *, tz: ZoneInfo = BILLING_TZ) -> Decimal:
        start, end = _day_bounds(day, tz)
        return self._sum(
            "SELECT cost_usd FROM llm_calls WHERE created_at >= ? AND created_at < ?", (start, end)
        )

    def spent_today(self, *, now: datetime | None = None, tz: ZoneInfo = BILLING_TZ) -> Decimal:
        return self.spent_on((now or _now()).astimezone(tz).date(), tz=tz)

    def analysis_cost(self, analysis_id: str) -> Decimal:
        return self._sum("SELECT cost_usd FROM llm_calls WHERE analysis_id = ?", (analysis_id,))

    def costs_by_client(
        self, start: date, end: date, *, tz: ZoneInfo = BILLING_TZ
    ) -> list[ClientCost]:
        """Cost per client for calls made on days ``start`` .. ``end`` (inclusive)."""
        if end < start:
            raise LedgerError("end date is before start date")
        lower, _ = _day_bounds(start, tz)
        _, upper = _day_bounds(end, tz)
        rows = self._conn.execute(
            """
            SELECT c.client_id, k.nickname, c.cost_usd, c.analysis_id
            FROM llm_calls AS c LEFT JOIN clients AS k ON k.id = c.client_id
            WHERE c.created_at >= ? AND c.created_at < ?
            """,
            (lower, upper),
        ).fetchall()
        totals: dict[str | None, tuple[str | None, int, set[str], Decimal]] = {}
        for row in rows:
            nickname, calls, analyses, cost = totals.get(
                row["client_id"], (row["nickname"], 0, set(), Decimal(0))
            )
            if row["analysis_id"]:
                analyses.add(row["analysis_id"])
            totals[row["client_id"]] = (
                nickname,
                calls + 1,
                analyses,
                cost + Decimal(row["cost_usd"]),
            )
        return [
            ClientCost(client_id, nickname, calls, len(analyses), cost)
            for client_id, (nickname, calls, analyses, cost) in totals.items()
        ]

    # ── Invoices ────────────────────────────────────────────────────────

    def create_invoice(
        self,
        client_id: str,
        start: date,
        end: date,
        *,
        fx_rate: Decimal,
        markup_pct: Decimal = Decimal(0),
        now: datetime | None = None,
    ) -> Invoice:
        """Snapshot a client's cost for a period; later price or FX changes do not alter it."""
        self.get_client(client_id)
        if fx_rate <= 0 or markup_pct < 0:
            raise LedgerError("fx_rate must be positive and markup_pct non-negative")
        costs = {c.client_id: c.cost_usd for c in self.costs_by_client(start, end)}
        cost = costs.get(client_id, Decimal(0))
        total_thb = cost * fx_rate * (1 + markup_pct / 100)
        invoice = Invoice(
            uuid.uuid4().hex, client_id, start, end, cost, fx_rate, markup_pct, total_thb
        )
        with self._transaction():
            self._conn.execute(
                "INSERT INTO invoices (id, client_id, period_start, period_end, cost_usd, fx_rate, "
                "markup_pct, total_thb, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    invoice.id,
                    client_id,
                    start.isoformat(),
                    end.isoformat(),
                    str(cost),
                    str(fx_rate),
                    str(markup_pct),
                    str(total_thb),
                    _iso(now or _now()),
                ),
            )
        return invoice

    def invoices(self, client_id: str) -> list[Invoice]:
        rows = self._conn.execute(
            "SELECT * FROM invoices WHERE client_id = ? ORDER BY created_at", (client_id,)
        ).fetchall()
        return [
            Invoice(
                row["id"],
                row["client_id"],
                date.fromisoformat(row["period_start"]),
                date.fromisoformat(row["period_end"]),
                Decimal(row["cost_usd"]),
                Decimal(row["fx_rate"]),
                Decimal(row["markup_pct"]),
                Decimal(row["total_thb"]),
            )
            for row in rows
        ]
