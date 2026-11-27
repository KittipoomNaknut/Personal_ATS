"""Refuse to run in an environment that could leak or misroute the API key."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

__all__ = ["API_BASE_URL", "environment_problems"]

API_BASE_URL: Final = "https://api.anthropic.com"

# Credentials must come only from the key file, never from the environment
# (environment variables are inherited by child processes and show up in
# /proc/<pid>/environ and crash reports).
_CREDENTIAL_VARIABLES: Final = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
)
_VERBOSE_LOG_LEVELS: Final = frozenset({"debug", "info"})


def environment_problems(env: Mapping[str, str]) -> list[str]:
    """Reasons not to start; empty when the environment is safe."""
    problems = [
        f"{name} is set; the gateway only accepts the key file from atsctl. Unset it."
        for name in _CREDENTIAL_VARIABLES
        if env.get(name)
    ]
    base_url = env.get("ANTHROPIC_BASE_URL")
    if base_url and base_url.rstrip("/") != API_BASE_URL:
        # A different base URL would send the API key to another server.
        problems.append(
            "ANTHROPIC_BASE_URL points somewhere other than the Anthropic API. Unset it."
        )
    if env.get("ANTHROPIC_LOG", "").strip().lower() in _VERBOSE_LOG_LEVELS:
        problems.append("ANTHROPIC_LOG enables SDK request logging. Unset it.")
    return problems
