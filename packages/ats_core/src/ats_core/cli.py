"""Command-line interface: ``ats check RESUME [--jd FILE]``.

Runs entirely on this machine; nothing is sent anywhere.

Exit codes: 0 success, 2 usage error, 3 file cannot be read, 4 the document
cannot be parsed (unsupported, encrypted, corrupt or too large).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, TextIO

from ats_core import __version__
from ats_core.extract import ExtractionError, UnsupportedFormatError, extract_document
from ats_core.report import Language, build_report, render_json, render_text

__all__ = ["main"]

EXIT_OK: Final = 0
EXIT_USAGE: Final = 2
EXIT_UNREADABLE: Final = 3
EXIT_UNPARSEABLE: Final = 4
MAX_JD_TEXT_BYTES: Final = 1024 * 1024


class _CliError(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ats",
        description="Check how an applicant tracking system reads your resume (runs locally).",
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
        "--name",
        action="append",
        default=[],
        metavar="NAME",
        help="your name, to make sure it is redacted (repeatable)",
    )
    check.add_argument(
        "--show-redacted",
        action="store_true",
        help="also print the resume text exactly as it would be sent to AI (personal data removed)",
    )
    return parser


def _check(args: argparse.Namespace, out: TextIO) -> None:
    data = _read_bytes(args.resume)
    try:
        document = extract_document(data)
    except ExtractionError as exc:
        raise _CliError(f"cannot parse {args.resume}: {exc}", EXIT_UNPARSEABLE) from exc
    jd_text = _read_jd(args.jd) if args.jd is not None else None
    report = build_report(
        document, source_name=args.resume.name, jd_text=jd_text, extra_names=args.name
    )
    language: Language = args.lang
    out.write(render_json(report, language) if args.json else render_text(report, language))
    if args.show_redacted:
        out.write("\n" + "-" * 60 + "\n" + report.redaction.text + "\n")


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    stdout = out if out is not None else sys.stdout
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse: --help / --version (0) or usage error (2)
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    try:
        _check(args, stdout)
    except _CliError as exc:
        print(f"ats: error: {exc}", file=sys.stderr)
        return exc.code
    return EXIT_OK
