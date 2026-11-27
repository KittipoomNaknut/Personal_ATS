"""Secret redaction for log output and arbitrary text.

Every service attaches :class:`SecretRedactingFilter` to its log handlers so that
API keys and credentials can never reach a log sink, including inside tracebacks
and stack traces. Use :func:`configure_logging` at process start-up.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Final, TextIO

__all__ = [
    "REDACTED",
    "SecretRedactingFilter",
    "configure_logging",
    "install_redaction",
    "redact",
]

REDACTED: Final = "[REDACTED]"

# Token formats that are secret wherever they appear.
_TOKEN_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # Anthropic API and Admin API keys (sk-ant-api03-..., sk-ant-admin01-...).
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    # GitHub personal / OAuth / app / refresh tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
)

# `key: value` / `key=value` pairs whose value is secret. The key name is kept so
# logs stay readable; only the value is replaced. Covers headers, JSON, dict
# reprs, kwargs and env-style names such as ANTHROPIC_API_KEY=...
_KEY_VALUE_PATTERN: Final = re.compile(
    r"""
    (?P<key>
        \b(?:[a-z0-9]+[_-])*
        (?:x-api-key|api[_-]?key|authorization|proxy-authorization
           |secret|password|passwd|token)
        \b
    )
    (?P<sep>["']?\s*[:=]\s*["']?)
    (?P<value>(?!\[REDACTED\])(?:bearer\s+|basic\s+)?[^\s"',;&}\]]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def redact(text: str) -> str:
    """Return ``text`` with every recognised secret replaced by ``[REDACTED]``."""
    text = _KEY_VALUE_PATTERN.sub(lambda m: f"{m['key']}{m['sep']}{REDACTED}", text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Logging filter that redacts secrets from the message, exception and stack.

    Attach it to *handlers* (not loggers): logger-level filters are skipped for
    records propagated from child loggers, handler-level filters are not.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed args must never break logging
            message = f"{record.msg} {record.args!r}"
        record.msg = redact(message)
        record.args = ()

        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install_redaction(logger: logging.Logger | None = None) -> None:
    """Attach a :class:`SecretRedactingFilter` to every handler of ``logger``.

    Defaults to the root logger. Safe to call more than once.
    """
    target = logger if logger is not None else logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())


def configure_logging(level: int = logging.INFO, stream: TextIO | None = None) -> None:
    """Configure root logging with secret redaction enabled on its handler."""
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(SecretRedactingFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
