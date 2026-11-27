from __future__ import annotations

import json
import socketserver
import threading
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ats_core.analysis.models import Bullet
from ats_core.gateway_client import GatewayClient, GatewayError, default_socket_path


class _Server:
    """A one-route HTTP server on a unix socket that records requests."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.status = 200
        self.body: bytes = b"{}"
        self.requests: list[tuple[str, str, Any]] = []
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                request_line = self.rfile.readline().decode().split()
                headers: dict[str, str] = {}
                while (line := self.rfile.readline().decode().strip()) != "":
                    name, _, value = line.partition(":")
                    headers[name.lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append((request_line[0], request_line[1], payload))
                self.wfile.write(
                    f"HTTP/1.1 {outer.status} X\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(outer.body)}\r\nConnection: close\r\n\r\n".encode()
                    + outer.body
                )

        self.server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reply(self, data: Any, status: int = 200) -> None:
        self.status = status
        self.body = json.dumps(data).encode() if not isinstance(data, bytes) else data

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server(tmp_path: Path) -> Iterator[_Server]:
    stub = _Server(tmp_path / "gateway.sock")
    try:
        yield stub
    finally:
        stub.close()


def ok(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "result": result,
        "cost_usd": "0.0123",
        "attempts": 1,
        "request_id": "req",
        "prompt_version": "v1",
    }


def test_judge_request_and_response(server: _Server) -> None:
    server.reply(ok({"verdict": "met"}))
    client = GatewayClient(server.path)
    result = client.judge(
        {"id": "R1", "text": "Python"},
        [Bullet("b1", "Python dev")],
        total_years=2.5,
        language="th",
        analysis_id="a1",
        client_id=None,
    )
    assert result.result == {"verdict": "met"}
    assert result.cost_usd == Decimal("0.0123")
    assert result.prompt_version == "v1"
    method, path, payload = server.requests[0]
    assert (method, path) == ("POST", "/v1/tasks/judge")
    assert payload == {
        "requirement": {"id": "R1", "text": "Python"},
        "bullets": [{"id": "b1", "text": "Python dev"}],
        "total_years": 2.5,
        "language": "th",
        "analysis_id": "a1",
        "client_id": None,
    }


def test_other_tasks_and_health(server: _Server) -> None:
    client = GatewayClient(server.path)
    server.reply(ok({"requirements": []}))
    client.parse_jd("JD", analysis_id=None, client_id="c1")
    client.structure_resume([Bullet("b1", "x")], analysis_id=None, client_id=None)
    server.reply({"status": "ok"})
    assert client.health() == {"status": "ok"}
    assert [r[1] for r in server.requests] == [
        "/v1/tasks/parse-jd",
        "/v1/tasks/structure-resume",
        "/health",
    ]
    assert server.requests[0][2] == {"jd_text": "JD", "analysis_id": None, "client_id": "c1"}


def test_errors_keep_code_status_and_cost(server: _Server) -> None:
    server.reply(
        {"error": "refused", "message": "declined", "category": "cyber", "cost_usd": "0.00"},
        status=422,
    )
    with pytest.raises(GatewayError) as info:
        GatewayClient(server.path).parse_jd("JD", analysis_id=None, client_id=None)
    error = info.value
    assert (error.code, error.status, str(error)) == ("refused", 422, "declined")
    assert error.cost_usd == Decimal("0.00")
    assert error.payload["category"] == "cyber"


def test_error_without_cost(server: _Server) -> None:
    server.reply({"error": "budget", "message": "over"}, status=429)
    with pytest.raises(GatewayError) as info:
        GatewayClient(server.path).health()
    assert info.value.cost_usd is None


def test_non_json_reply(server: _Server) -> None:
    server.reply(b"<html>oops</html>", status=500)
    with pytest.raises(GatewayError) as info:
        GatewayClient(server.path).health()
    assert (info.value.code, info.value.status) == ("protocol", 500)


def test_non_object_error_reply(server: _Server) -> None:
    server.reply(["unexpected"], status=500)
    with pytest.raises(GatewayError) as info:
        GatewayClient(server.path).health()
    assert info.value.code == "http_error"


def test_gateway_not_running(tmp_path: Path) -> None:
    with pytest.raises(GatewayError) as info:
        GatewayClient(tmp_path / "missing.sock").health()
    assert info.value.code == "not_running"
    assert "atsctl start" in str(info.value)


def test_connection_dropped_mid_request(tmp_path: Path) -> None:
    class Hangup(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.rfile.readline()  # read the request line, then close without answering

    path = tmp_path / "hangup.sock"
    hangup = socketserver.UnixStreamServer(str(path), Hangup)
    thread = threading.Thread(target=hangup.handle_request, daemon=True)
    thread.start()
    try:
        with pytest.raises(GatewayError) as info:
            GatewayClient(path, timeout=5).health()
        assert info.value.code == "connection"
    finally:
        thread.join(timeout=5)
        hangup.server_close()


def test_default_socket_path() -> None:
    assert default_socket_path({"ATS_RUNTIME_DIR": "/r"}) == Path("/r/gateway.sock")
    assert default_socket_path({"XDG_RUNTIME_DIR": "/run/user/7"}) == Path(
        "/run/user/7/personal-ats/gateway.sock"
    )
    with pytest.raises(GatewayError, match="XDG_RUNTIME_DIR"):
        default_socket_path({})
