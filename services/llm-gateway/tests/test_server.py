"""The gateway process: startup refusals and a real run on a unix socket."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from ats_gateway.app import GatewayState, create_app
from ats_gateway.caller import ClaudeCaller
from ats_gateway.server import main, socket_problems
from fakes import FAKE_KEY, FakeApi
from fastapi.testclient import TestClient

from ats_core.billing.budget import DailyBudget
from ats_core.billing.ledger import Ledger
from ats_core.billing.pricing import default_price_table


@pytest.fixture
def private_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "run"
    directory.mkdir(mode=0o700)
    return directory


def write_key(directory: Path) -> Path:
    path = directory / "api-key"
    path.write_text(FAKE_KEY)
    os.chmod(path, 0o600)
    return path


def args(directory: Path, *extra: str) -> list[str]:
    return [
        "serve",
        "--socket",
        str(directory / "gateway.sock"),
        "--key-file",
        str(directory / "api-key"),
        "--ledger",
        str(directory / "ledger.db"),
        *extra,
    ]


# ── Health endpoint ──────────────────────────────────────────────────────


def test_health_reports_budget(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        caller = ClaudeCaller(
            client=FakeApi().client(), ledger=ledger, prices=default_price_table()
        )
        app = create_app(GatewayState(caller=caller, ledger=ledger, budget=DailyBudget()))
        body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    assert body["model"] == "claude-opus-5-5"
    assert body["fallback_model"] == "claude-opus-5"
    assert body["budget"] == {"limit_usd": "2.00", "spent_today_usd": "0", "remaining_usd": "2.00"}


def test_no_docs_or_generic_endpoints(tmp_path: Path) -> None:
    with Ledger(tmp_path / "ledger.db") as ledger:
        caller = ClaudeCaller(
            client=FakeApi().client(), ledger=ledger, prices=default_price_table()
        )
        client = TestClient(
            create_app(GatewayState(caller=caller, ledger=ledger, budget=DailyBudget()))
        )
        for path in ("/docs", "/openapi.json", "/redoc", "/v1/messages"):
            assert client.get(path).status_code == 404


# ── Startup refusals ─────────────────────────────────────────────────────


def test_refuses_unsafe_environment(private_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_key(private_dir)
    assert main(args(private_dir), env={"ANTHROPIC_API_KEY": "x"}) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_refuses_shared_socket_directory(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)  # noqa: S103 - deliberately unsafe for the test
    (problem,) = socket_problems(shared / "gateway.sock")
    assert "accessible by other users" in problem


def test_refuses_missing_directory_and_existing_socket(tmp_path: Path, private_dir: Path) -> None:
    assert "does not exist" in socket_problems(tmp_path / "missing" / "gateway.sock")[0]
    (private_dir / "gateway.sock").touch()
    assert "already exists" in socket_problems(private_dir / "gateway.sock")[0]


def test_refuses_bad_key_file(private_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(args(private_dir), env={}) == 2
    assert "key file not found" in capsys.readouterr().err


def test_refuses_bad_budget(private_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_key(private_dir)
    assert main(args(private_dir, "--daily-budget", "lots"), env={}) == 2
    assert "invalid --daily-budget" in capsys.readouterr().err


def test_refuses_unpriced_model(private_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_key(private_dir)
    assert main(args(private_dir, "--model", "claude-opus-9"), env={}) == 2
    assert "no price" in capsys.readouterr().err


# ── Real process on a unix socket ────────────────────────────────────────


@pytest.fixture
def running_gateway(private_dir: Path) -> Iterator[tuple[Path, subprocess.Popen[bytes]]]:
    write_key(private_dir)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    process = subprocess.Popen(  # noqa: S603 - fixed arguments
        [
            sys.executable,
            "-m",
            "ats_gateway.server",
            *args(private_dir, "--unlink-key-file", "--skip-upstream-check"),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    socket = private_dir / "gateway.sock"
    deadline = time.monotonic() + 20
    while not socket.exists():
        if process.poll() is not None or time.monotonic() > deadline:
            process.kill()
            raise AssertionError(process.communicate()[1].decode())
        time.sleep(0.05)
    try:
        yield private_dir, process
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_gateway_serves_on_private_socket(
    running_gateway: tuple[Path, subprocess.Popen[bytes]],
) -> None:
    directory, _ = running_gateway
    socket = directory / "gateway.sock"
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=str(socket)), base_url="http://gateway"
    ) as client:
        assert client.get("/health").json()["status"] == "ok"
    mode = socket.stat().st_mode
    assert stat.S_ISSOCK(mode)
    assert stat.S_IMODE(mode) & 0o077 == 0  # only the owner can connect
    assert not (directory / "api-key").exists()  # key lives only in the process


def test_gateway_stops_cleanly(running_gateway: tuple[Path, subprocess.Popen[bytes]]) -> None:
    _, process = running_gateway
    process.terminate()
    # uvicorn shuts down gracefully, then re-raises SIGTERM so the exit status
    # reflects the signal.
    assert process.wait(timeout=10) in (0, -signal.SIGTERM)
    stdout, stderr = process.communicate()
    assert b"Shutting down" in stderr
    assert b"Traceback" not in stderr
    assert FAKE_KEY.encode() not in stdout + stderr


def test_bind_private_socket_is_owner_only(private_dir: Path) -> None:
    from ats_gateway.server import bind_private_socket

    path = private_dir / "gateway.sock"
    listener = bind_private_socket(path)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        import socket as socket_module

        with socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM) as client:
            client.connect(str(path))  # the owner can connect
    finally:
        listener.close()
