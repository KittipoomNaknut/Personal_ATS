"""Command-line interface.

* ``ats check RESUME [--jd FILE] [--ai [--client NAME] [--yes]]`` - audit a resume.
  Without ``--ai`` everything runs on this machine and nothing is sent anywhere.
  With ``--ai`` the redacted text goes to Claude through the local gateway
  (``atsctl start``); a cost estimate is shown and must be confirmed first.
* ``ats costs [--client NAME] [--month YYYY-MM | --from D --to D] [--fx R] [--markup P]
  [--csv] [--invoice]`` - what each client's analyses cost.
* ``ats forget --client NAME [--yes]`` - remove a client's name and document
  fingerprints; anonymous cost rows are kept for your accounts.

Exit codes: 0 success, 2 usage error, 3 file cannot be read, 4 the document
cannot be parsed, 5 the AI analysis failed, 6 cancelled by you.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, TextIO

from ats_core import __version__
from ats_core.billing.ledger import Ledger, LedgerError
from ats_core.extract import ExtractionError, UnsupportedFormatError, extract_document
from ats_core.gateway_client import GatewayClient, GatewayError, default_socket_path
from ats_core.paths import ledger_path
from ats_core.pipeline import AnalysisCancelled, Estimate, run_ai_analysis
from ats_core.report import CheckReport, Language, build_report, render_json, render_text

__all__ = ["main"]

EXIT_OK: Final = 0
EXIT_USAGE: Final = 2
EXIT_UNREADABLE: Final = 3
EXIT_UNPARSEABLE: Final = 4
EXIT_AI_FAILED: Final = 5
EXIT_CANCELLED: Final = 6
MAX_JD_TEXT_BYTES: Final = 1024 * 1024

Confirm = Callable[[str], bool]


class _CliError(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# ── Input ────────────────────────────────────────────────────────────────


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _CliError(f"cannot read {path}: {exc.strerror or exc}", EXIT_UNREADABLE) from exc


def _read_jd(path: Path) -> str:
    """A job description may be plain text, PDF or DOCX."""
    data = _read_bytes(path)
    try:
        return extract_document(data).text
    except UnsupportedFormatError:
        pass
    except ExtractionError as exc:
        raise _CliError(f"cannot parse job description {path}: {exc}", EXIT_UNPARSEABLE) from exc
    if len(data) > MAX_JD_TEXT_BYTES:
        raise _CliError(f"job description {path} is larger than 1 MB", EXIT_UNPARSEABLE)
    return data.decode("utf-8", errors="replace")


def _ask(question: str) -> bool:
    """Default confirmation: interactive terminals only."""
    if not sys.stdin.isatty():
        return False
    print(question + " [y/N] ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().casefold() in {"y", "yes"}


# ── Parser ───────────────────────────────────────────────────────────────


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ats", description="Check how an applicant tracking system reads a resume."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser(
        "check", help="audit a resume, optionally against a job description"
    )
    check.add_argument("resume", type=Path, help="resume file (PDF or DOCX)")
    check.add_argument("--jd", type=Path, help="job description (text, PDF or DOCX)")
    check.add_argument("--lang", choices=("th", "en"), default="th", help="report language")
    check.add_argument("--json", action="store_true", help="machine-readable output")
    check.add_argument(
        "--name", action="append", default=[], metavar="NAME",
        help="the candidate's name, to make sure it is redacted (repeatable)",
    )  # fmt: skip
    check.add_argument(
        "--show-redacted", action="store_true",
        help="also print the resume text exactly as it would be sent to AI (personal data removed)",
    )  # fmt: skip
    check.add_argument("--ai", action="store_true", help="also judge every requirement with Claude")
    check.add_argument("--client", metavar="NAME", help="bill this analysis to a client")
    check.add_argument("--yes", action="store_true", help="accept the cost estimate without asking")

    costs = commands.add_parser("costs", help="what analyses cost, per client")
    costs.add_argument("--client", metavar="NAME")
    period = costs.add_mutually_exclusive_group()
    period.add_argument("--month", metavar="YYYY-MM", help="default: the current month")
    period.add_argument("--from", dest="start", type=date.fromisoformat, metavar="YYYY-MM-DD")
    costs.add_argument("--to", dest="end", type=date.fromisoformat, metavar="YYYY-MM-DD")
    costs.add_argument("--fx", metavar="RATE", help="THB per USD, e.g. 36.20")
    costs.add_argument("--markup", metavar="PERCENT", default="0", help="added on top of cost")
    costs.add_argument("--csv", action="store_true", help="CSV output")
    costs.add_argument(
        "--invoice", action="store_true", help="save an invoice snapshot (needs --client and --fx)"
    )

    forget = commands.add_parser("forget", help="remove a client's name and document fingerprints")
    forget.add_argument("--client", metavar="NAME", required=True)
    forget.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return parser


# ── check ────────────────────────────────────────────────────────────────


def _estimate_text(estimate: Estimate, remaining: str | None) -> str:
    lines = [
        f"Estimated cost of judging {estimate.calls_min}-{estimate.calls_max} calls: "
        f"{estimate.judge_low:.4f}-{estimate.judge_high:.4f} USD ({estimate.basis})",
        f"Already spent on this analysis: {estimate.spent:.4f} USD",
        f"Expected total: {estimate.total_low:.4f}-{estimate.total_high:.4f} USD",
    ]
    if remaining is not None:
        lines.append(f"Daily budget remaining: {remaining} USD")
    return "\n".join(lines)


def _check(args: argparse.Namespace, out: TextIO, env: Mapping[str, str], confirm: Confirm) -> None:
    if (args.ai or args.client) and args.jd is None:
        raise _CliError("--ai needs a job description (--jd)", EXIT_USAGE)
    if args.client and not args.ai:
        raise _CliError("--client only applies with --ai", EXIT_USAGE)
    data = _read_bytes(args.resume)
    try:
        document = extract_document(data)
    except ExtractionError as exc:
        raise _CliError(f"cannot parse {args.resume}: {exc}", EXIT_UNPARSEABLE) from exc
    jd_text = _read_jd(args.jd) if args.jd is not None else None
    language: Language = args.lang
    report = build_report(
        document, source_name=args.resume.name, jd_text=jd_text, extra_names=args.name
    )

    if args.ai and jd_text is not None:
        report = _with_ai(args, report, document.visible_text, jd_text, env, confirm, language)

    out.write(render_json(report, language) if args.json else render_text(report, language))
    if args.show_redacted:
        out.write("\n" + "-" * 60 + "\n" + report.redaction.text + "\n")


def _with_ai(
    args: argparse.Namespace,
    report: CheckReport,
    visible_text: str,
    jd_text: str,
    env: Mapping[str, str],
    confirm: Confirm,
    language: Language,
) -> CheckReport:
    try:
        gateway = GatewayClient(default_socket_path(env))
        health = gateway.health()
    except GatewayError as exc:
        raise _CliError(str(exc), EXIT_AI_FAILED) from exc
    remaining = str(health.get("budget", {}).get("remaining_usd")) if health else None

    def decide(estimate: Estimate) -> bool:
        text = _estimate_text(estimate, remaining)
        print(text, file=sys.stderr)
        return True if args.yes else confirm("Continue with the AI analysis?")

    with Ledger(ledger_path(env)) as ledger:
        client_id = ledger.client_for(args.client).id if args.client else None
        try:
            analysis = run_ai_analysis(
                visible_text,
                jd_text,
                gateway,
                ledger,
                parseability=report.parseability.score,
                keyword_coverage=report.keywords.required_coverage if report.keywords else None,
                client_id=client_id,
                extra_names=args.name,
                language=language,
                on_progress=lambda message: print(f"... {message}", file=sys.stderr),
                confirm=decide,
            )
        except AnalysisCancelled as exc:
            raise _CliError(
                f"cancelled; {exc.cost_usd} USD was spent reading the documents", EXIT_CANCELLED
            ) from exc
        except GatewayError as exc:
            spent = f" ({exc.cost_usd} USD spent on that step)" if exc.cost_usd is not None else ""
            raise _CliError(f"AI analysis failed: {exc}{spent}", EXIT_AI_FAILED) from exc
    return replace(report, ai=analysis, client=args.client)


# ── costs ────────────────────────────────────────────────────────────────


def _period(args: argparse.Namespace, today: date) -> tuple[date, date]:
    if args.start is not None:
        return args.start, args.end or today
    if args.end is not None:
        raise _CliError("--to needs --from", EXIT_USAGE)
    month = args.month or today.strftime("%Y-%m")
    try:
        year, number = (int(part) for part in month.split("-"))
        last = calendar.monthrange(year, number)[1]
        return date(year, number, 1), date(year, number, last)
    except (ValueError, calendar.IllegalMonthError):
        raise _CliError(f"invalid --month {month!r}; use YYYY-MM", EXIT_USAGE) from None


def _decimal(value: str | None, flag: str) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise _CliError(f"invalid {flag} {value!r}", EXIT_USAGE) from None
    if not number.is_finite() or number < 0:
        raise _CliError(f"invalid {flag} {value!r}", EXIT_USAGE)
    return number


def _costs(args: argparse.Namespace, out: TextIO, env: Mapping[str, str]) -> None:
    start, end = _period(args, date.today())
    fx = _decimal(args.fx, "--fx")
    markup = _decimal(args.markup, "--markup") or Decimal(0)
    with Ledger(ledger_path(env)) as ledger:
        wanted = ledger.find_client(args.client) if args.client else None
        if args.client and wanted is None:
            raise _CliError(f"no client named {args.client!r}", EXIT_USAGE)
        if args.invoice:
            if wanted is None or fx is None:
                raise _CliError("--invoice needs --client and --fx", EXIT_USAGE)
            invoice = ledger.create_invoice(wanted.id, start, end, fx_rate=fx, markup_pct=markup)
            out.write(
                f"invoice {invoice.id} for {wanted.nickname}, {start} to {end}: "
                f"{invoice.cost_usd} USD x {fx} THB/USD + {markup}% = {invoice.total_thb:.2f} THB\n"
            )
            return
        rows = [
            c for c in ledger.costs_by_client(start, end)
            if wanted is None or c.client_id == wanted.id
        ]  # fmt: skip
    rows.sort(key=lambda c: (c.nickname is None, c.nickname or ""))
    header = ["client", "analyses", "calls", "cost_usd"]
    if fx is not None:
        header += ["cost_thb", "total_thb"]

    def cells(nickname: str, analyses: int, calls: int, cost: Decimal) -> list[str]:
        values = [nickname, str(analyses), str(calls), f"{cost:.4f}"]
        if fx is not None:
            thb = cost * fx
            values += [f"{thb:.2f}", f"{thb * (1 + markup / 100):.2f}"]
        return values

    table = [
        cells(c.nickname or "(own use / forgotten)", c.analyses, c.calls, c.cost_usd) for c in rows
    ]
    total = sum((c.cost_usd for c in rows), Decimal(0))
    table.append(cells("TOTAL", sum(c.analyses for c in rows), sum(c.calls for c in rows), total))
    if args.csv:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(table)
        return
    out.write(
        f"Costs from {start} to {end}" + (f" (markup {markup}%)" if fx is not None else "") + "\n"
    )
    widths = [max(len(row[i]) for row in [header, *table]) for i in range(len(header))]
    for row in [header, *table]:
        out.write(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
            + "\n"
        )


# ── forget ───────────────────────────────────────────────────────────────


def _forget(
    args: argparse.Namespace, out: TextIO, env: Mapping[str, str], confirm: Confirm
) -> None:
    with Ledger(ledger_path(env)) as ledger:
        client = ledger.find_client(args.client)
        if client is None:
            raise _CliError(f"no client named {args.client!r}", EXIT_USAGE)
        if not args.yes and not confirm(f"Forget {client.nickname!r}? This cannot be undone."):
            raise _CliError("not forgotten", EXIT_CANCELLED)
        try:
            ledger.forget_client(client.id)
        except LedgerError as exc:  # pragma: no cover - the client was just found
            raise _CliError(str(exc), EXIT_USAGE) from exc
    out.write(f"forgot {args.client!r}; anonymous cost records were kept\n")


# ── Entry point ──────────────────────────────────────────────────────────


def main(
    argv: Sequence[str] | None = None,
    out: TextIO | None = None,
    *,
    env: Mapping[str, str] | None = None,
    confirm: Confirm = _ask,
) -> int:
    stdout = out if out is not None else sys.stdout
    environment = os.environ if env is None else env
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse: --help / --version (0) or usage error (2)
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    try:
        if args.command == "check":
            _check(args, stdout, environment, confirm)
        elif args.command == "costs":
            _costs(args, stdout, environment)
        else:
            _forget(args, stdout, environment, confirm)
    except _CliError as exc:
        print(
            f"ats: error: {exc}" if exc.code != EXIT_CANCELLED else f"ats: {exc}", file=sys.stderr
        )
        return exc.code
    except LedgerError as exc:
        print(f"ats: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK
