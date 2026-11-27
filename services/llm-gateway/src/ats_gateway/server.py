"""``ats-gateway serve``: start the gateway on a private unix socket.

Normally started by ``atsctl start``, which writes the key file, waits for
``/health`` and makes sure the key file is gone.
"""

from __future__ import annotations

import argparse
import os
import socket
import stat
import sys
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

import anthropic
import uvicorn

from ats_core.billing.budget import DailyBudget
from ats_core.billing.ledger import Ledger, LedgerError
from ats_core.billing.pricing import default_price_table
from ats_core.redaction import configure_logging
from ats_gateway.app import GatewayState, create_app
from ats_gateway.caller import DEFAULT_FALLBACK_MODEL, DEFAULT_MODEL, ClaudeCaller
from ats_gateway.environment import API_BASE_URL, environment_problems
from ats_gateway.keys import KeyFileError, load_api_key

__all__ = ["bind_private_socket", "main", "socket_problems"]

EXIT_OK: Final = 0
EXIT_CONFIG: Final = 2
REQUEST_TIMEOUT_SECONDS: Final = 600.0


def socket_problems(socket_path: Path) -> list[str]:
    """The socket's directory must be private to this user; the path must be free."""
    directory = socket_path.parent
    try:
        info = directory.stat()
    except FileNotFoundError:
        return [f"socket directory does not exist: {directory}"]
    problems = []
    if info.st_uid != os.getuid():
        problems.append(f"socket directory is not owned by the current user: {directory}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        problems.append(
            f"socket directory is accessible by other users; run: chmod 700 {directory}"
        )
    if socket_path.exists() or socket_path.is_symlink():
        problems.append(f"socket path already exists (is a gateway running?): {socket_path}")
    return problems


def bind_private_socket(path: Path) -> socket.socket:
    """A listening unix socket that only the current user can connect to (mode 0600)."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o177)
    try:
        listener.bind(str(path))
    finally:
        os.umask(previous)
    os.chmod(path, 0o600)
    listener.listen(64)
    return listener


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ats-gateway")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the gateway on a unix socket")
    serve.add_argument("--socket", type=Path, required=True)
    serve.add_argument("--key-file", type=Path, required=True)
    serve.add_argument("--ledger", type=Path, required=True)
    serve.add_argument(
        "--unlink-key-file", action="store_true", help="delete the key file after reading"
    )
    serve.add_argument("--daily-budget", default="2.00", help="USD per day (default 2.00)")
    serve.add_argument("--model", default=DEFAULT_MODEL)
    serve.add_argument("--fallback-model", default=DEFAULT_FALLBACK_MODEL)
    return parser


def _fail(message: str) -> int:
    print(f"ats-gateway: {message}", file=sys.stderr)
    return EXIT_CONFIG


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging()
    problems = environment_problems(os.environ if env is None else env) + socket_problems(
        args.socket
    )
    if problems:
        return _fail("refusing to start:\n  - " + "\n  - ".join(problems))
    try:
        budget = DailyBudget(Decimal(args.daily_budget))
    except (InvalidOperation, ValueError):
        return _fail(f"invalid --daily-budget {args.daily_budget!r}")
    try:
        key = load_api_key(args.key_file, unlink=args.unlink_key_file)
        ledger = Ledger(args.ledger)
    except (KeyFileError, LedgerError) as exc:
        return _fail(str(exc))

    client = anthropic.Anthropic(
        api_key=key.reveal(), base_url=API_BASE_URL, max_retries=2, timeout=REQUEST_TIMEOUT_SECONDS
    )
    del key
    try:
        caller = ClaudeCaller(
            client=client,
            ledger=ledger,
            prices=default_price_table(),
            budget=budget,
            model=args.model,
            fallback_model=args.fallback_model,
        )
    except ValueError as exc:
        return _fail(str(exc))

    app = create_app(GatewayState(caller=caller, ledger=ledger, budget=budget))
    listener = bind_private_socket(args.socket)
    # uvicorn would chmod a socket it creates itself to 0666; handing it an
    # already-bound descriptor keeps the owner-only mode.
    uvicorn.run(app, fd=listener.fileno(), log_config=None, access_log=False)
    listener.close()
    ledger.close()
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
