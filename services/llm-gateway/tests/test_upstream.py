from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from ats_gateway import server
from ats_gateway.upstream import UpstreamCheckError, verify_upstream
from fakes import FAKE_KEY, FakeApi

MODEL, FALLBACK = "claude-opus-5-5", "claude-opus-5"


def test_allowed_fallback_passes() -> None:
    api = FakeApi(model_info={"allowed_fallback_models": ["claude-opus-5", "claude-opus-4-8"]})
    verify_upstream(api.client(), MODEL, FALLBACK)
    assert api.requests[0].url.path == f"/v1/models/{MODEL}"
    assert api.requests[0].method == "GET"


def test_disallowed_fallback_refuses() -> None:
    api = FakeApi(model_info={"allowed_fallback_models": ["claude-opus-4-8"]})
    with pytest.raises(UpstreamCheckError, match="not allowed"):
        verify_upstream(api.client(), MODEL, FALLBACK)


def test_unreported_fallbacks_continue_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    verify_upstream(FakeApi().client(), MODEL, FALLBACK)
    assert "did not report allowed fallback models" in caplog.text


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "rejected"), (403, "may not use"), (404, "does not exist"), (500, "answered 500")],
)
def test_api_errors_refuse(status: int, message: str) -> None:
    with pytest.raises(UpstreamCheckError, match=message):
        verify_upstream(FakeApi(model_status=status).client(), MODEL, FALLBACK)


def test_unreachable_api_refuses() -> None:
    def down(request: httpx2.Request) -> None:
        raise httpx2.ConnectError("offline", request=request)

    with pytest.raises(UpstreamCheckError, match="cannot reach"):
        verify_upstream(FakeApi().client(down), MODEL, FALLBACK)


# ── Wired into the gateway's startup ─────────────────────────────────────


def _args(directory: Path, *extra: str) -> list[str]:
    key = directory / "api-key"
    key.write_text(FAKE_KEY)
    key.chmod(0o600)
    return [
        "serve",
        "--socket", str(directory / "gateway.sock"),
        "--key-file", str(key),
        "--ledger", str(directory / "ledger.db"),
        *extra,
    ]  # fmt: skip


@pytest.fixture
def private_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "run"
    directory.mkdir(mode=0o700)
    return directory


def test_startup_refuses_when_the_check_fails(
    private_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(*_args: object) -> None:
        raise UpstreamCheckError("the API key was rejected (invalid or revoked)")

    monkeypatch.setattr(server, "verify_upstream", failing)
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: pytest.fail("must not serve"))
    assert server.main(_args(private_dir), env={}) == 2
    assert "startup check failed: the API key was rejected" in capsys.readouterr().err


def test_startup_runs_the_check_by_default(
    private_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        server, "verify_upstream", lambda _client, model, fallback: calls.append((model, fallback))
    )
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: None)
    assert server.main(_args(private_dir), env={}) == 0
    assert calls == [(MODEL, FALLBACK)]


def test_skip_flag_skips_the_check(private_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "verify_upstream", lambda *_a: pytest.fail("check must be skipped"))
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: None)
    assert server.main(_args(private_dir, "--skip-upstream-check"), env={}) == 0
