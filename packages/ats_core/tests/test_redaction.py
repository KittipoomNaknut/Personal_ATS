"""Tests for ats_core.redaction.

Fake credentials are assembled at runtime so that no secret-shaped literal is
ever committed (keeps gitleaks clean without allow-list exceptions).
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from ats_core.redaction import (
    REDACTED,
    SecretRedactingFilter,
    configure_logging,
    install_redaction,
    redact,
)

FAKE_ANTHROPIC_KEY = "sk-ant-" + "api03-" + "A1b2C3d4_E5-f6" * 6
FAKE_ADMIN_KEY = "sk-ant-" + "admin01-" + "Z9y8X7w6" * 5
FAKE_GITHUB_TOKEN = "gh" + "p_" + "a1B2c3D4e5" * 4


@pytest.fixture
def logger_and_stream() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(SecretRedactingFilter())
    logger = logging.getLogger("ats_core.tests.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    yield logger, stream
    logger.handlers = []


# ── redact() ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        FAKE_ANTHROPIC_KEY,
        f"using key {FAKE_ANTHROPIC_KEY} now",
        f"admin {FAKE_ADMIN_KEY}",
        f"token {FAKE_GITHUB_TOKEN}",
        f"x-api-key: {FAKE_ANTHROPIC_KEY}",
        f"{{'x-api-key': '{FAKE_ANTHROPIC_KEY}'}}",
        f'{{"api_key": "{FAKE_ANTHROPIC_KEY}"}}',
        f"Anthropic(api_key='{FAKE_ANTHROPIC_KEY}')",
        f"ANTHROPIC_API_KEY={FAKE_ANTHROPIC_KEY}",
    ],
)
def test_redact_removes_known_secrets(text: str) -> None:
    result = redact(text)
    assert FAKE_ANTHROPIC_KEY not in result
    assert FAKE_ADMIN_KEY not in result
    assert FAKE_GITHUB_TOKEN not in result
    assert REDACTED in result


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abc.def.ghi", f"Authorization: {REDACTED}"),
        ("Proxy-Authorization: Basic dXNlcjpwYXNz", f"Proxy-Authorization: {REDACTED}"),
        ("db password=hunter2 host=db", f"db password={REDACTED} host=db"),
        ("access_token=xyz123&next=1", f"access_token={REDACTED}&next=1"),
        ("client_secret: s3cr3t", f"client_secret: {REDACTED}"),
    ],
)
def test_redact_key_value_keeps_key_name(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "input_tokens=1200 output_tokens=340 cache_read_input_tokens=900",
        "max_tokens=16000",
        "model=claude-opus-5-5 effort=high",
        "skill-ant-colony optimisation",
        "ผู้สมัครมีประสบการณ์ Kubernetes 3 ปี",
        "",
    ],
)
def test_redact_leaves_harmless_text_untouched(text: str) -> None:
    assert redact(text) == text


def test_redact_is_idempotent() -> None:
    once = redact(f"x-api-key: {FAKE_ANTHROPIC_KEY} and {FAKE_GITHUB_TOKEN}")
    assert redact(once) == once


# ── SecretRedactingFilter ───────────────────────────────────────────────


def test_filter_redacts_message_args(
    logger_and_stream: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = logger_and_stream
    logger.info("calling api with key=%s", FAKE_ANTHROPIC_KEY)
    output = stream.getvalue()
    assert FAKE_ANTHROPIC_KEY not in output
    assert "calling api with key=" in output


def test_filter_redacts_exception_traceback(
    logger_and_stream: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = logger_and_stream
    try:
        raise RuntimeError(f"auth failed for {FAKE_ANTHROPIC_KEY}")
    except RuntimeError:
        logger.exception("request failed")
    output = stream.getvalue()
    assert FAKE_ANTHROPIC_KEY not in output
    assert "RuntimeError: auth failed for" in output
    assert "Traceback" in output


def test_filter_redacts_stack_info(
    logger_and_stream: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = logger_and_stream
    secret_local = FAKE_ANTHROPIC_KEY
    logger.info("checkpoint %s", secret_local, stack_info=True)
    output = stream.getvalue()
    assert FAKE_ANTHROPIC_KEY not in output
    assert "Stack (most recent call last)" in output


def test_filter_survives_malformed_args(
    logger_and_stream: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = logger_and_stream
    logger.info("two placeholders %s %s", FAKE_ANTHROPIC_KEY)
    output = stream.getvalue()
    assert FAKE_ANTHROPIC_KEY not in output
    assert "two placeholders" in output


def test_filter_applies_to_propagated_child_records() -> None:
    stream = io.StringIO()
    parent = logging.getLogger("ats_core.tests.parent")
    handler = logging.StreamHandler(stream)
    parent.handlers = [handler]
    parent.propagate = False
    parent.setLevel(logging.DEBUG)
    try:
        install_redaction(parent)
        logging.getLogger("ats_core.tests.parent.child").warning("leak %s", FAKE_ANTHROPIC_KEY)
        assert FAKE_ANTHROPIC_KEY not in stream.getvalue()
        assert REDACTED in stream.getvalue()
    finally:
        parent.handlers = []


def test_install_redaction_is_idempotent() -> None:
    logger = logging.getLogger("ats_core.tests.idempotent")
    handler = logging.StreamHandler(io.StringIO())
    logger.handlers = [handler]
    try:
        install_redaction(logger)
        install_redaction(logger)
        assert sum(isinstance(f, SecretRedactingFilter) for f in handler.filters) == 1
    finally:
        logger.handlers = []


def test_install_redaction_defaults_to_root_logger() -> None:
    root = logging.getLogger()
    saved = root.handlers[:]
    handler = logging.StreamHandler(io.StringIO())
    root.handlers = [handler]
    try:
        install_redaction()
        assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters)
    finally:
        root.handlers = saved


# ── configure_logging() ─────────────────────────────────────────────────


def test_configure_logging_redacts_root_output() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    stream = io.StringIO()
    try:
        configure_logging(logging.DEBUG, stream=stream)
        logging.getLogger("ats_core.tests.any").debug("headers %s", {"x-api-key": FAKE_ADMIN_KEY})
        output = stream.getvalue()
        assert FAKE_ADMIN_KEY not in output
        assert "headers" in output
        assert root.level == logging.DEBUG
    finally:
        root.handlers, root.level = saved_handlers, saved_level


def test_configure_logging_defaults_to_stderr() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging()
        (handler,) = root.handlers
        assert isinstance(handler, logging.StreamHandler)
        assert root.level == logging.INFO
    finally:
        root.handlers, root.level = saved_handlers, saved_level
