"""A fake Claude API behind the real SDK.

Tests use the real ``anthropic`` client with an ``httpx2.MockTransport``, so
request serialisation, headers and response parsing are exercised for real;
only the network is replaced. No API key or connection is needed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx2
from anthropic.types.beta import BetaMessage

FAKE_KEY = "sk-ant-" + "api03-" + "x" * 40


def usage(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write: int = 0,
    cache_read: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
        **extra,
    }


def message_json(
    *,
    model: str = "claude-opus-5-5",
    stop_reason: str = "end_turn",
    text: str = '{"ok": true}',
    usage_json: dict[str, Any] | None = None,
    content: list[dict[str, Any]] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "content": content if content is not None else [{"type": "text", "text": text}],
        "usage": usage_json if usage_json is not None else usage(10_000, 2_000),
    }


def message(**kwargs: Any) -> BetaMessage:
    return BetaMessage.model_validate(message_json(**kwargs))


def fallback_block(
    category: str | None, src: str = "claude-opus-5-5", dst: str = "claude-opus-5"
) -> dict[str, Any]:
    return {
        "type": "fallback",
        "from": {"model": src},
        "to": {"model": dst},
        "trigger": {"type": "refusal", "category": category},
    }


def iteration(
    kind: str, model: str | None, input_tokens: int, output_tokens: int = 0, **extra: Any
) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": kind, **usage(input_tokens, output_tokens), **extra}
    if model is not None:
        entry["model"] = model
    return entry


@dataclass
class FakeApi:
    """Records requests; answers count_tokens and messages.create."""

    responses: list[httpx2.Response] = field(default_factory=list)
    counted_tokens: int = 5_000
    requests: list[httpx2.Request] = field(default_factory=list)

    def reply(self, body: dict[str, Any], *, status: int = 200) -> None:
        self.responses.append(
            httpx2.Response(status, json=body, headers={"request-id": "req_fake"})
        )

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path.endswith("/count_tokens"):
            return httpx2.Response(200, json={"input_tokens": self.counted_tokens})
        if not self.responses:
            raise AssertionError("unexpected messages.create call")
        return self.responses.pop(0)

    @property
    def created(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/v1/messages")]

    def client(
        self, transport_error: Callable[[httpx2.Request], None] | None = None
    ) -> anthropic.Anthropic:
        def handle(request: httpx2.Request) -> httpx2.Response:
            if transport_error is not None:
                transport_error(request)
            return self.handler(request)

        return anthropic.Anthropic(
            api_key=FAKE_KEY,
            base_url="https://api.anthropic.com",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handle)),
        )
