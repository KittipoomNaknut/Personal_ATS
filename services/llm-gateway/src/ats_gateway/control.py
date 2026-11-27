"""``atsctl``: start, stop and inspect the whole project.

The API key exists on disk only for the moment between ``start`` fetching it
from the OS keyring and the gateway reading it: it is written to a private
directory on tmpfs (RAM, never a real disk), the gateway deletes it after
reading, and ``start`` deletes it again whatever happens. ``stop`` leaves no
key behind anywhere.

Paths follow the XDG base directories and can be overridden for testing:

* runtime (tmpfs): ``$ATS_RUNTIME_DIR`` or ``$XDG_RUNTIME_DIR/personal-ats``
* data (ledger):   ``$ATS_DATA_DIR`` or ``$XDG_DATA_HOME/personal-ats``
* state (logs):    ``$ATS_STATE_DIR`` or ``$XDG_STATE_HOME/personal-ats``
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Protocol

from ats_gateway.environment import environment_problems

__all__ = [
    "ControlError",
    "Controller",
    "GatewayStatus",
    "Paths",
    "SecretSource",
    "SecretToolSource",
    "main",
]

KEYRING_ATTRIBUTES: Final = ("service", "anthropic", "account", "personal-ats")
START_TIMEOUT_SECONDS: Final = 15.0
STOP_TIMEOUT_SECONDS: Final = 30.0
_POLL_SECONDS: Final = 0.05
_GATEWAY_MODULE: Final = "ats_gateway.server"

EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_NOT_RUNNING: Final = 3  # same convention as `systemctl status`


class ControlError(Exception):
    """A start/stop operation could not be completed safely."""


# ── Paths ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Paths:
    runtime: Path
    data: Path
    state: Path

    @property
    def socket(self) -> Path:
        return self.runtime / "gateway.sock"

    @property
    def pid_file(self) -> Path:
        return self.runtime / "gateway.pid"

    @property
    def key_file(self) -> Path:
        return self.runtime / "api-key"

    @property
    def ledger(self) -> Path:
        return self.data / "ledger.db"

    @property
    def log_file(self) -> Path:
        return self.state / "gateway.log"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Paths:
        home = Path(env.get("HOME", str(Path.home())))
        runtime = env.get("ATS_RUNTIME_DIR")
        if runtime is None:
            xdg_runtime = env.get("XDG_RUNTIME_DIR")
            if not xdg_runtime:
                raise ControlError(
                    "XDG_RUNTIME_DIR is not set; refusing to fall back to a shared location"
                )
            runtime = str(Path(xdg_runtime) / "personal-ats")
        data = env.get("ATS_DATA_DIR") or str(
            Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "personal-ats"
        )
        state = env.get("ATS_STATE_DIR") or str(
            Path(env.get("XDG_STATE_HOME") or home / ".local" / "state") / "personal-ats"
        )
        return cls(Path(runtime), Path(data), Path(state))


def filesystem_type(path: Path, mountinfo: Path = Path("/proc/self/mountinfo")) -> str | None:
    """Filesystem type of the mount that contains ``path`` (Linux)."""
    target = str(path.resolve())
    best, best_type = "", None
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        left, _, right = line.partition(" - ")
        fields, extra = left.split(), right.split()
        if len(fields) < 5 or not extra:
            continue
        mount_point = fields[4].replace("\\040", " ")
        inside = target == mount_point or target.startswith(mount_point.rstrip("/") + "/")
        if inside and len(mount_point) >= len(best):
            best, best_type = mount_point, extra[0]
    return best_type


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid():
        raise ControlError(f"{path} is not owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)


# ── Keyring ──────────────────────────────────────────────────────────────


class SecretSource(Protocol):
    def available(self) -> bool: ...

    def fetch(self) -> bytes:
        """The API key as bytes, or raise ControlError."""
        ...


class SecretToolSource:
    """The GNOME / freedesktop keyring via ``secret-tool`` (libsecret)."""

    def __init__(self, attributes: Sequence[str] = KEYRING_ATTRIBUTES) -> None:
        self._attributes = tuple(attributes)

    def available(self) -> bool:
        return shutil.which("secret-tool") is not None

    def fetch(self) -> bytes:
        executable = shutil.which("secret-tool")
        if executable is None:
            raise ControlError("secret-tool not found; install libsecret-tools")
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [executable, "lookup", *self._attributes],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            store = " ".join(
                ["secret-tool store --label='Anthropic personal-ats'", *self._attributes]
            )
            raise ControlError(f"no API key in the keyring; store one with:\n  {store}")
        return result.stdout.strip()


# ── Gateway process ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GatewayStatus:
    running: bool
    pid: int | None = None
    started_at: float | None = None
    health: Mapping[str, Any] | None = None

    @property
    def spent_today(self) -> Decimal | None:
        if not self.health:
            return None
        return Decimal(str(self.health["budget"]["spent_today_usd"]))


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("gateway", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self._path))


def fetch_health(socket_path: Path, timeout: float = 2.0) -> dict[str, Any] | None:
    """``GET /health`` over the unix socket; ``None`` if nothing answers."""
    connection = _UnixHTTPConnection(socket_path, timeout)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status != 200:
            return None
        body: dict[str, Any] = json.loads(response.read())
        return body
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def _process_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _is_our_gateway(pid: int, socket_path: Path) -> bool:
    """True only for a live gateway serving this socket (guards against PID reuse)."""
    cmdline = _process_cmdline(pid)
    return _GATEWAY_MODULE in cmdline and str(socket_path) in cmdline


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Controller:
    paths: Paths
    secrets: SecretSource
    daily_budget: str = "2.00"
    env: Mapping[str, str] | None = None
    python: str = sys.executable
    require_tmpfs: bool = True
    out: Callable[[str], None] = print
    gateway_args: Sequence[str] = ()
    """Extra ``ats-gateway serve`` arguments; tests use it to skip the network check."""

    # ── State ───────────────────────────────────────────────────────────

    def _read_pid(self) -> int | None:
        try:
            text = self.paths.pid_file.read_text().strip()
        except FileNotFoundError:
            return None
        return int(text) if text.isdigit() else None

    def status(self) -> GatewayStatus:
        pid = self._read_pid()
        if pid is None or not _is_our_gateway(pid, self.paths.socket):
            return GatewayStatus(running=False)
        started = self.paths.pid_file.stat().st_mtime
        return GatewayStatus(True, pid, started, fetch_health(self.paths.socket))

    def _remove_runtime_files(self) -> None:
        for path in (self.paths.key_file, self.paths.socket, self.paths.pid_file):
            path.unlink(missing_ok=True)

    # ── Start ───────────────────────────────────────────────────────────

    def _preflight(self) -> None:
        env = os.environ if self.env is None else self.env
        problems = list(environment_problems(env))
        if not self.secrets.available():
            problems.append("secret-tool not found; install libsecret-tools")
        if problems:
            raise ControlError("cannot start:\n  - " + "\n  - ".join(problems))
        _private_dir(self.paths.runtime)
        if self.require_tmpfs and filesystem_type(self.paths.runtime) != "tmpfs":
            raise ControlError(
                f"{self.paths.runtime} is not on tmpfs; the key would touch a real disk"
            )
        _private_dir(self.paths.data)
        _private_dir(self.paths.state)

    def _write_key(self) -> None:
        secret = self.secrets.fetch()
        fd = os.open(
            self.paths.key_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(fd, secret)
        finally:
            os.close(fd)

    def _child_env(self) -> dict[str, str]:
        source = os.environ if self.env is None else self.env
        return {k: v for k, v in source.items() if not k.startswith("ANTHROPIC_")}

    def _spawn(self) -> subprocess.Popen[bytes]:
        log_fd = os.open(self.paths.log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [
                    self.python,
                    "-m",
                    _GATEWAY_MODULE,
                    "serve",
                    "--socket",
                    str(self.paths.socket),
                    "--key-file",
                    str(self.paths.key_file),
                    "--ledger",
                    str(self.paths.ledger),
                    "--unlink-key-file",
                    "--daily-budget",
                    self.daily_budget,
                    *self.gateway_args,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                env=self._child_env(),
                start_new_session=True,  # survives the terminal that ran atsctl
                close_fds=True,
            )
        finally:
            os.close(log_fd)

    def _wait_healthy(self, process: subprocess.Popen[bytes]) -> dict[str, Any]:
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ControlError(f"gateway exited during start; see {self.paths.log_file}")
            health = fetch_health(self.paths.socket, timeout=0.5)
            if health is not None:
                return health
            time.sleep(_POLL_SECONDS)
        process.terminate()
        raise ControlError(f"gateway did not become healthy in {START_TIMEOUT_SECONDS:.0f}s")

    def start(self) -> GatewayStatus:
        current = self.status()
        if current.running:
            self.out(f"already running (pid {current.pid})")
            return current
        self._preflight()
        self._remove_runtime_files()  # leftovers from a crash
        try:
            self._write_key()
            process = self._spawn()
            self.paths.pid_file.write_text(f"{process.pid}\n")
            os.chmod(self.paths.pid_file, 0o600)
            health = self._wait_healthy(process)
        except BaseException:
            self._remove_runtime_files()
            raise
        finally:
            self.paths.key_file.unlink(missing_ok=True)  # never leave the key behind
        self.out(
            f"started (pid {process.pid}), spent today: {health['budget']['spent_today_usd']} USD"
        )
        return GatewayStatus(True, process.pid, time.time(), health)

    # ── Stop ────────────────────────────────────────────────────────────

    def stop(self) -> GatewayStatus:
        current = self.status()
        if not current.running or current.pid is None:
            self._remove_runtime_files()
            self.out("not running")
            return GatewayStatus(running=False)
        spent = current.spent_today
        os.kill(current.pid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while _alive(current.pid) and _is_our_gateway(current.pid, self.paths.socket):
            if time.monotonic() > deadline:
                os.kill(current.pid, signal.SIGKILL)
                break
            time.sleep(_POLL_SECONDS)
        self._remove_runtime_files()
        suffix = f", spent today: {spent} USD" if spent is not None else ""
        self.out(f"stopped (pid {current.pid}){suffix}")
        return GatewayStatus(running=False)

    def restart(self) -> GatewayStatus:
        self.stop()
        return self.start()

    # ── Logs ────────────────────────────────────────────────────────────

    def logs(self, lines: int = 50) -> list[str]:
        try:
            content = self.paths.log_file.read_text(errors="replace").splitlines()
        except FileNotFoundError:
            return []
        return content[-lines:] if lines > 0 else content


# ── CLI ──────────────────────────────────────────────────────────────────


def _describe(status: GatewayStatus) -> str:
    if not status.running:
        return "stopped"
    lines = [f"running (pid {status.pid})"]
    if status.started_at is not None:
        uptime = int(time.time() - status.started_at)
        lines.append(f"uptime: {uptime // 3600}h {uptime % 3600 // 60}m {uptime % 60}s")
    if status.health:
        budget = status.health["budget"]
        lines.append(
            f"model: {status.health['model']} (fallback {status.health['fallback_model']})"
        )
        lines.append(
            f"spent today: {budget['spent_today_usd']} / {budget['limit_usd']} USD "
            f"(remaining {budget['remaining_usd']})"
        )
    else:
        lines.append("health: not responding")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atsctl", description="Start and stop the personal ATS.")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="load the key from the keyring and start the gateway")
    start.add_argument("--daily-budget", default="2.00", help="USD per day (default 2.00)")
    commands.add_parser("stop", help="stop the gateway; no key is left anywhere")
    restart = commands.add_parser("restart", help="stop, then start")
    restart.add_argument("--daily-budget", default="2.00")
    commands.add_parser("status", help="show whether the gateway runs and today's spend")
    logs = commands.add_parser("logs", help="show the gateway log (redacted)")
    logs.add_argument("-n", "--lines", type=int, default=50)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    secrets: SecretSource | None = None,
    gateway_args: Sequence[str] = (),
) -> int:
    args = _parser().parse_args(argv)
    try:
        controller = Controller(
            paths=Paths.from_env(os.environ),
            secrets=secrets or SecretToolSource(),
            daily_budget=getattr(args, "daily_budget", "2.00"),
            gateway_args=gateway_args,
        )
        if args.command == "start":
            controller.start()
        elif args.command == "stop":
            controller.stop()
        elif args.command == "restart":
            controller.restart()
        elif args.command == "status":
            status = controller.status()
            print(_describe(status))
            return EXIT_OK if status.running else EXIT_NOT_RUNNING
        else:
            for line in controller.logs(args.lines):
                print(line)
    except ControlError as exc:
        print(f"atsctl: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
