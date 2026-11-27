"""atsctl: start / stop / status / restart / logs against a real gateway process.

The keyring is replaced by a fake source holding a fake key; the real keyring
is never read or written.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from ats_gateway import control
from ats_gateway.control import (
    ControlError,
    Controller,
    Paths,
    SecretToolSource,
    filesystem_type,
    main,
)
from fakes import FAKE_KEY

OFFLINE = ("--skip-upstream-check",)  # tests never call the real API
TMPFS = Path("/dev/shm")  # noqa: S108 - private mkdtemp dirs inside a tmpfs mount
needs_tmpfs = pytest.mark.skipif(
    filesystem_type(TMPFS) != "tmpfs", reason="needs a tmpfs mount at /dev/shm"
)


@dataclass
class FakeSecrets:
    value: bytes = FAKE_KEY.encode()
    present: bool = True
    error: str | None = None
    fetched: int = field(default=0)

    def available(self) -> bool:
        return self.present

    def fetch(self) -> bytes:
        self.fetched += 1
        if self.error:
            raise ControlError(self.error)
        return self.value


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "ATS_"))}


@pytest.fixture
def paths(tmp_path: Path) -> Iterator[Paths]:
    runtime = Path(tempfile.mkdtemp(prefix="ats-test-", dir=TMPFS))
    try:
        yield Paths(runtime=runtime, data=tmp_path / "data", state=tmp_path / "state")
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


@pytest.fixture
def messages() -> list[str]:
    return []


@pytest.fixture
def controller(paths: Paths, messages: list[str]) -> Iterator[Controller]:
    ctl = Controller(
        gateway_args=OFFLINE,
        paths=paths,
        secrets=FakeSecrets(),
        env=clean_env(),
        out=messages.append,
    )
    try:
        yield ctl
    finally:
        ctl.stop()


def wait_gone(pid: int, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)  # reap if it is our child
        if (
            not Path(f"/proc/{pid}/cmdline").exists()
            or not Path(f"/proc/{pid}/cmdline").read_bytes()
        ):
            return True
        time.sleep(0.05)
    return False


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ── start / status / stop ────────────────────────────────────────────────


@needs_tmpfs
def test_start_runs_gateway_without_leaving_the_key(
    controller: Controller, paths: Paths, messages: list[str]
) -> None:
    status = controller.start()
    assert status.running
    assert status.pid is not None
    assert messages[-1].startswith(f"started (pid {status.pid})")
    assert not paths.key_file.exists()  # gateway deleted it; start double-checks
    assert stat.S_ISSOCK(paths.socket.stat().st_mode)
    assert mode(paths.socket) == 0o600
    assert mode(paths.pid_file) == 0o600
    assert mode(paths.runtime) == 0o700
    assert mode(paths.log_file) == 0o600
    assert mode(paths.ledger) == 0o600

    current = controller.status()
    assert current.running
    assert current.pid == status.pid
    assert current.health is not None
    assert current.health["budget"]["limit_usd"] == "2.00"
    assert current.spent_today == 0


@needs_tmpfs
def test_start_is_idempotent(controller: Controller, messages: list[str]) -> None:
    first = controller.start()
    second = controller.start()
    assert second.pid == first.pid
    assert messages[-1] == f"already running (pid {first.pid})"


@needs_tmpfs
def test_stop_cleans_everything(controller: Controller, paths: Paths, messages: list[str]) -> None:
    pid = controller.start().pid
    assert pid is not None
    controller.stop()
    assert messages[-1] == f"stopped (pid {pid}), spent today: 0 USD"
    assert wait_gone(pid)
    for leftover in (paths.socket, paths.pid_file, paths.key_file):
        assert not leftover.exists()
    assert not controller.status().running


@needs_tmpfs
def test_stop_when_stopped(controller: Controller, messages: list[str]) -> None:
    assert not controller.stop().running
    assert messages[-1] == "not running"


@needs_tmpfs
def test_restart_starts_a_new_process(controller: Controller) -> None:
    first = controller.start().pid
    second = controller.restart().pid
    assert first is not None
    assert second not in (None, first)
    assert wait_gone(first)


@needs_tmpfs
def test_stale_pid_of_another_process_is_never_killed(controller: Controller, paths: Paths) -> None:
    # A crashed gateway's PID may since belong to an unrelated process: here, us.
    paths.runtime.mkdir(exist_ok=True, mode=0o700)
    paths.pid_file.write_text(f"{os.getpid()}\n")
    assert not controller.status().running
    controller.stop()  # must not signal the test process
    assert not paths.pid_file.exists()
    assert controller.start().running


@needs_tmpfs
def test_logs_are_redacted_and_readable(controller: Controller) -> None:
    controller.start()
    text = "\n".join(controller.logs(200))
    assert "Application startup complete" in text
    assert FAKE_KEY not in text
    assert controller.logs(1) == controller.logs(200)[-1:]


def test_logs_without_file(paths: Paths) -> None:
    assert Controller(gateway_args=OFFLINE, paths=paths, secrets=FakeSecrets()).logs() == []


# ── Failed starts leave nothing behind ───────────────────────────────────


def assert_nothing_left(paths: Paths) -> None:
    for leftover in (paths.key_file, paths.socket, paths.pid_file):
        assert not leftover.exists(), leftover


@needs_tmpfs
def test_invalid_key_fails_and_is_removed(paths: Paths) -> None:
    ctl = Controller(
        gateway_args=OFFLINE,
        paths=paths,
        secrets=FakeSecrets(value=b"not-a-key"),
        env=clean_env(),
        out=lambda _: None,
    )
    with pytest.raises(ControlError, match="exited during start"):
        ctl.start()
    assert_nothing_left(paths)
    assert "does not contain a valid Anthropic API key" in "\n".join(ctl.logs())
    assert "not-a-key" not in "\n".join(ctl.logs())


@needs_tmpfs
def test_missing_keyring_entry(paths: Paths) -> None:
    secrets = FakeSecrets(error="no API key in the keyring")
    ctl = Controller(
        gateway_args=OFFLINE, paths=paths, secrets=secrets, env=clean_env(), out=lambda _: None
    )
    with pytest.raises(ControlError, match="no API key"):
        ctl.start()
    assert_nothing_left(paths)


@needs_tmpfs
def test_gateway_that_never_becomes_healthy(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hang = tmp_path / "hang.py"
    hang.write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(control, "START_TIMEOUT_SECONDS", 0.5)
    wrapper = tmp_path / "python"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {hang}\n")
    wrapper.chmod(0o700)
    ctl = Controller(
        gateway_args=OFFLINE,
        paths=paths,
        secrets=FakeSecrets(),
        env=clean_env(),
        python=str(wrapper),
        out=lambda _: None,
    )
    with pytest.raises(ControlError, match="did not become healthy"):
        ctl.start()
    assert_nothing_left(paths)


@needs_tmpfs
def test_invalid_budget_fails_cleanly(paths: Paths) -> None:
    ctl = Controller(
        gateway_args=OFFLINE,
        paths=paths,
        secrets=FakeSecrets(),
        env=clean_env(),
        daily_budget="lots",
        out=lambda _: None,
    )
    with pytest.raises(ControlError, match="exited during start"):
        ctl.start()
    assert_nothing_left(paths)


# ── Preflight refusals ───────────────────────────────────────────────────


def test_runtime_dir_must_be_tmpfs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pretend the runtime directory is on a real disk (here /tmp may itself be tmpfs).
    monkeypatch.setattr(control, "filesystem_type", lambda _path: "ext4")
    secrets = FakeSecrets()
    paths = Paths(runtime=tmp_path / "run", data=tmp_path / "data", state=tmp_path / "state")
    ctl = Controller(
        gateway_args=OFFLINE, paths=paths, secrets=secrets, env=clean_env(), out=lambda _: None
    )
    try:
        with pytest.raises(ControlError, match="not on tmpfs"):
            ctl.start()
    finally:
        ctl.stop()  # if the guard ever regresses, do not leave a gateway running
    assert secrets.fetched == 0  # the key was never fetched


@needs_tmpfs
def test_unsafe_environment_is_refused(paths: Paths) -> None:
    secrets = FakeSecrets()
    env = {**clean_env(), "ANTHROPIC_BASE_URL": "https://evil.example.com"}
    with pytest.raises(ControlError, match="ANTHROPIC_BASE_URL"):
        Controller(gateway_args=OFFLINE, paths=paths, secrets=secrets, env=env).start()
    assert secrets.fetched == 0


@needs_tmpfs
def test_missing_secret_tool_is_refused(paths: Paths) -> None:
    with pytest.raises(ControlError, match="secret-tool not found"):
        Controller(
            gateway_args=OFFLINE, paths=paths, secrets=FakeSecrets(present=False), env=clean_env()
        ).start()


@needs_tmpfs
def test_runtime_dir_permissions_are_tightened(paths: Paths) -> None:
    os.chmod(paths.runtime, 0o755)  # noqa: S103 - deliberately loose for the test
    ctl = Controller(
        gateway_args=OFFLINE,
        paths=paths,
        secrets=FakeSecrets(),
        env=clean_env(),
        out=lambda _: None,
    )
    try:
        ctl.start()
        assert mode(paths.runtime) == 0o700
    finally:
        ctl.stop()


# ── Paths and filesystem detection ───────────────────────────────────────


def test_paths_from_xdg() -> None:
    paths = Paths.from_env({"HOME": "/home/u", "XDG_RUNTIME_DIR": "/run/user/7"})
    assert paths.runtime == Path("/run/user/7/personal-ats")
    assert paths.ledger == Path("/home/u/.local/share/personal-ats/ledger.db")
    assert paths.log_file == Path("/home/u/.local/state/personal-ats/gateway.log")
    assert paths.socket.name == "gateway.sock"


def test_paths_overrides() -> None:
    env = {"ATS_RUNTIME_DIR": "/r", "ATS_DATA_DIR": "/d", "ATS_STATE_DIR": "/s", "HOME": "/h"}
    assert Paths.from_env(env) == Paths(Path("/r"), Path("/d"), Path("/s"))
    custom = Paths.from_env(
        {"HOME": "/h", "XDG_RUNTIME_DIR": "/x", "XDG_DATA_HOME": "/dd", "XDG_STATE_HOME": "/ss"}
    )
    assert custom.data == Path("/dd/personal-ats")
    assert custom.state == Path("/ss/personal-ats")


def test_paths_refuse_missing_runtime_dir() -> None:
    with pytest.raises(ControlError, match="XDG_RUNTIME_DIR is not set"):
        Paths.from_env({"HOME": "/h"})


def test_filesystem_type_uses_longest_mount(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "30 22 0:5 / /run rw - tmpfs tmpfs rw\n"
        "31 30 0:6 / /run/user/1000 rw - tmpfs tmpfs rw\n"
        "40 22 0:7 / /mnt/my\\040disk rw - xfs /dev/sdb1 rw\n"
        "garbage line\n"
    )
    assert filesystem_type(Path("/run/user/1000/personal-ats"), mountinfo) == "tmpfs"
    assert filesystem_type(Path("/home/u"), mountinfo) == "ext4"
    assert filesystem_type(Path("/mnt/my disk/x"), mountinfo) == "xfs"
    assert filesystem_type(Path("/x"), tmp_path / "missing") is None


# ── Real secret-tool invocation (with a stand-in executable) ─────────────


def fake_secret_tool(directory: Path, script: str) -> None:
    directory.mkdir(exist_ok=True)
    tool = directory / "secret-tool"
    tool.write_text("#!/bin/sh\n" + script)
    tool.chmod(0o700)


def test_secret_tool_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    record = tmp_path / "argv"
    fake_secret_tool(bin_dir, f'echo "$@" > {record}\nprintf "%s\\n" "{FAKE_KEY}"\n')
    monkeypatch.setenv("PATH", str(bin_dir))
    source = SecretToolSource()
    assert source.available()
    assert source.fetch() == FAKE_KEY.encode()
    assert record.read_text().split() == [
        "lookup",
        "service",
        "anthropic",
        "account",
        "personal-ats",
    ]


def test_secret_tool_without_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_secret_tool(tmp_path / "bin", "exit 1\n")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(ControlError, match="secret-tool store"):
        SecretToolSource().fetch()


def test_secret_tool_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert not SecretToolSource().available()
    with pytest.raises(ControlError, match="not found"):
        SecretToolSource().fetch()


# ── CLI ──────────────────────────────────────────────────────────────────


@needs_tmpfs
def test_cli_lifecycle(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in [k for k in os.environ if k.startswith("ANTHROPIC_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("ATS_RUNTIME_DIR", str(paths.runtime))
    monkeypatch.setenv("ATS_DATA_DIR", str(paths.data))
    monkeypatch.setenv("ATS_STATE_DIR", str(paths.state))
    secrets = FakeSecrets()
    try:
        assert main(["status"], secrets=secrets, gateway_args=OFFLINE) == control.EXIT_NOT_RUNNING
        assert capsys.readouterr().out.strip() == "stopped"

        assert main(["start", "--daily-budget", "1.50"], secrets=secrets, gateway_args=OFFLINE) == 0
        assert "started" in capsys.readouterr().out

        assert main(["status"], secrets=secrets, gateway_args=OFFLINE) == 0
        out = capsys.readouterr().out
        assert "running (pid" in out
        assert "spent today: 0 / 1.50 USD (remaining 1.50)" in out
        assert "model: claude-opus-5-5 (fallback claude-opus-5)" in out

        assert main(["logs", "-n", "5"], secrets=secrets, gateway_args=OFFLINE) == 0
        assert capsys.readouterr().out.strip()

        assert main(["restart"], secrets=secrets, gateway_args=OFFLINE) == 0
        assert "started" in capsys.readouterr().out
    finally:
        assert main(["stop"], secrets=secrets, gateway_args=OFFLINE) == 0
    assert "stopped" in capsys.readouterr().out


def test_cli_reports_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ATS_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert main(["status"], secrets=FakeSecrets()) == control.EXIT_ERROR
    assert "XDG_RUNTIME_DIR is not set" in capsys.readouterr().err


def test_describe_unhealthy_gateway() -> None:
    text = control._describe(
        control.GatewayStatus(running=True, pid=42, started_at=time.time() - 3_725)
    )
    assert text.splitlines() == ["running (pid 42)", "uptime: 1h 2m 5s", "health: not responding"]
    assert control.GatewayStatus(running=True).spent_today is None


# ── Process handling details ─────────────────────────────────────────────


@needs_tmpfs
def test_stop_escalates_to_sigkill(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A gateway stuck in shutdown that ignores SIGTERM must still be stopped.
    stubborn = tmp_path / "stubborn.py"
    stubborn.write_text(
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"
    )
    paths.runtime.mkdir(exist_ok=True, mode=0o700)
    process = subprocess.Popen(  # noqa: S603 - fixed arguments
        [
            sys.executable,
            str(stubborn),
            "-m",
            "ats_gateway.server",
            "serve",
            "--socket",
            str(paths.socket),
        ]
    )
    try:
        time.sleep(0.3)  # let it install the SIGTERM handler
        paths.pid_file.write_text(f"{process.pid}\n")
        monkeypatch.setattr(control, "STOP_TIMEOUT_SECONDS", 0.3)
        Controller(
            gateway_args=OFFLINE, paths=paths, secrets=FakeSecrets(), out=lambda _: None
        ).stop()
        assert process.wait(timeout=5) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()


def test_alive_and_cmdline_helpers() -> None:
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    assert not control._alive(finished.pid)
    assert control._process_cmdline(finished.pid) == []
    assert control._alive(1)  # exists, but owned by root: PermissionError means alive
    assert control._alive(os.getpid())


def test_health_ignores_non_200(paths: Paths) -> None:
    import socketserver
    import threading

    paths.runtime.mkdir(exist_ok=True, mode=0o700)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.rfile.readline()
            self.wfile.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")

    server = socketserver.UnixStreamServer(str(paths.socket), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        assert control.fetch_health(paths.socket) is None
    finally:
        thread.join(timeout=5)
        server.server_close()
    assert control.fetch_health(paths.runtime / "missing.sock") is None


def test_describe_without_start_time() -> None:
    health = {
        "model": "m",
        "fallback_model": "f",
        "budget": {"spent_today_usd": "0.5", "limit_usd": "2.00", "remaining_usd": "1.5"},
    }
    text = control._describe(control.GatewayStatus(running=True, pid=7, health=health))
    assert "uptime" not in text
    assert "spent today: 0.5 / 2.00 USD (remaining 1.5)" in text


@needs_tmpfs
def test_atsctl_never_skips_the_startup_check_by_default(paths: Paths, tmp_path: Path) -> None:
    record = tmp_path / "argv"
    wrapper = tmp_path / "python"
    wrapper.write_text(f'#!/bin/sh\necho "$@" > {record}\nexit 1\n')
    wrapper.chmod(0o700)
    ctl = Controller(
        paths=paths, secrets=FakeSecrets(), env=clean_env(), python=str(wrapper), out=lambda _: None
    )
    with pytest.raises(ControlError):
        ctl.start()
    argv = record.read_text().split()
    assert "serve" in argv
    assert "--unlink-key-file" in argv
    assert "--skip-upstream-check" not in argv
