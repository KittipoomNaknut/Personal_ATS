"""Client for the local LLM gateway, over its private unix socket.

The core package never talks to the Claude API; it only talks to the gateway,
which holds the key. Standard library only.
"""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from ats_core.analysis.models import Bullet

__all__ = ["GatewayClient", "GatewayError", "TaskResult", "default_socket_path"]

DEFAULT_TIMEOUT_SECONDS: Final = 660.0  # a high-effort answer can take minutes


def default_socket_path(env: Mapping[str, str]) -> Path:
    """Where ``atsctl start`` puts the gateway socket."""
    runtime = env.get("ATS_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "gateway.sock"
    xdg = env.get("XDG_RUNTIME_DIR")
    if not xdg:
        raise GatewayError("not_configured", "XDG_RUNTIME_DIR is not set")
    return Path(xdg) / "personal-ats" / "gateway.sock"


class GatewayError(Exception):
    """The gateway refused or failed a task. ``code`` is stable (pii, budget, ...)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        cost_usd: Decimal | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.cost_usd = cost_usd
        self.payload = dict(payload or {})


@dataclass(frozen=True, slots=True)
class TaskResult:
    result: Mapping[str, Any]
    cost_usd: Decimal
    attempts: int
    prompt_version: str


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("gateway", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self._path))


class GatewayClient:
    def __init__(self, socket_path: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        connection = _UnixConnection(self.socket_path, self.timeout)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        except (FileNotFoundError, ConnectionRefusedError):
            raise GatewayError(
                "not_running", "the AI gateway is not running; start it with: atsctl start"
            ) from None
        except (OSError, http.client.HTTPException) as exc:
            raise GatewayError("connection", f"gateway connection failed: {exc}") from exc
        finally:
            connection.close()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise GatewayError(
                "protocol", "gateway sent a non-JSON reply", status=response.status
            ) from None
        if response.status != 200:
            cost = data.get("cost_usd") if isinstance(data, dict) else None
            raise GatewayError(
                str(data.get("error", "http_error")) if isinstance(data, dict) else "http_error",
                str(data.get("message", f"HTTP {response.status}"))
                if isinstance(data, dict)
                else "",
                status=response.status,
                cost_usd=Decimal(cost) if cost is not None else None,
                payload=data if isinstance(data, dict) else None,
            )
        return data

    def health(self) -> Mapping[str, Any]:
        data: Mapping[str, Any] = self._request("GET", "/health")
        return data

    def _task(self, path: str, body: Mapping[str, Any]) -> TaskResult:
        data = self._request("POST", path, body)
        return TaskResult(
            result=data["result"],
            cost_usd=Decimal(data["cost_usd"]),
            attempts=int(data["attempts"]),
            prompt_version=str(data["prompt_version"]),
        )

    @staticmethod
    def _bullets(bullets: Sequence[Bullet]) -> list[dict[str, str]]:
        return [{"id": b.id, "text": b.text} for b in bullets]

    def parse_jd(
        self, jd_text: str, *, analysis_id: str | None, client_id: str | None
    ) -> TaskResult:
        return self._task(
            "/v1/tasks/parse-jd",
            {"jd_text": jd_text, "analysis_id": analysis_id, "client_id": client_id},
        )

    def structure_resume(
        self, bullets: Sequence[Bullet], *, analysis_id: str | None, client_id: str | None
    ) -> TaskResult:
        return self._task(
            "/v1/tasks/structure-resume",
            {"bullets": self._bullets(bullets), "analysis_id": analysis_id, "client_id": client_id},
        )

    def judge(
        self,
        requirement: Mapping[str, Any],
        bullets: Sequence[Bullet],
        *,
        total_years: float | None,
        language: str,
        analysis_id: str | None,
        client_id: str | None,
    ) -> TaskResult:
        return self._task(
            "/v1/tasks/judge",
            {
                "requirement": dict(requirement),
                "bullets": self._bullets(bullets),
                "total_years": total_years,
                "language": language,
                "analysis_id": analysis_id,
                "client_id": client_id,
            },
        )
