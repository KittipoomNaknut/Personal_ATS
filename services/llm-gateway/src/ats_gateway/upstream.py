"""Startup check against the Claude API (free: the Models API is not billed).

Run before the gateway accepts requests, so a bad key or an unusable fallback
model fails ``atsctl start`` immediately instead of in the middle of a client's
analysis. The worst-case budget check assumes the configured fallback model; if
the API would not allow it, the gateway must not start.
"""

from __future__ import annotations

import logging

import anthropic

__all__ = ["UpstreamCheckError", "verify_upstream"]

logger = logging.getLogger(__name__)


class UpstreamCheckError(Exception):
    """The API key or the model configuration is not usable."""


def verify_upstream(client: anthropic.Anthropic, model: str, fallback_model: str) -> None:
    try:
        info = client.beta.models.retrieve(model)
    except anthropic.AuthenticationError:
        raise UpstreamCheckError("the API key was rejected (invalid or revoked)") from None
    except anthropic.PermissionDeniedError:
        raise UpstreamCheckError("the API key may not use the Models API or this model") from None
    except anthropic.NotFoundError:
        raise UpstreamCheckError(f"model {model!r} does not exist for this key") from None
    except anthropic.APIStatusError as exc:
        raise UpstreamCheckError(
            f"the Claude API answered {exc.status_code} during the startup check"
        ) from None
    except anthropic.APIError as exc:
        raise UpstreamCheckError(f"cannot reach the Claude API: {type(exc).__name__}") from None
    allowed = getattr(info, "allowed_fallback_models", None)
    if allowed is None:
        logger.warning("the API did not report allowed fallback models for %s; continuing", model)
        return
    if fallback_model not in allowed:
        raise UpstreamCheckError(
            f"fallback model {fallback_model!r} is not allowed for {model!r} "
            f"(allowed: {', '.join(allowed) or 'none'})"
        )
