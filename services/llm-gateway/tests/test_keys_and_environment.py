from __future__ import annotations

import os
import pickle
from pathlib import Path

import pytest
from ats_gateway.environment import environment_problems
from ats_gateway.keys import ApiKey, KeyFileError, load_api_key
from fakes import FAKE_KEY


def key_file(tmp_path: Path, content: str | bytes = FAKE_KEY + "\n", mode: int = 0o600) -> Path:
    path = tmp_path / "api-key"
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    os.chmod(path, mode)
    return path


def test_loads_valid_key(tmp_path: Path) -> None:
    key = load_api_key(key_file(tmp_path))
    assert key.reveal() == FAKE_KEY


def test_key_never_shows_itself(tmp_path: Path) -> None:
    key = load_api_key(key_file(tmp_path))
    assert FAKE_KEY not in repr(key)
    assert FAKE_KEY not in str(key)
    assert FAKE_KEY not in f"{key}"
    with pytest.raises(TypeError, match="pickled"):
        pickle.dumps(key)


def test_read_only_file_is_accepted(tmp_path: Path) -> None:
    assert load_api_key(key_file(tmp_path, mode=0o400)).reveal() == FAKE_KEY


def test_unlink_after_read(tmp_path: Path) -> None:
    path = key_file(tmp_path)
    load_api_key(path, unlink=True)
    assert not path.exists()


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644])
def test_shared_file_is_refused(tmp_path: Path, mode: int) -> None:
    with pytest.raises(KeyFileError, match="accessible by other users"):
        load_api_key(key_file(tmp_path, mode=mode))


def test_symlink_is_refused(tmp_path: Path) -> None:
    target = key_file(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(KeyFileError, match="cannot open"):
        load_api_key(link)


def test_directory_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "dir"
    directory.mkdir(mode=0o700)
    with pytest.raises(KeyFileError):
        load_api_key(directory)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(KeyFileError, match="not found"):
        load_api_key(tmp_path / "nope")


def test_oversized_file(tmp_path: Path) -> None:
    with pytest.raises(KeyFileError, match="too large"):
        load_api_key(key_file(tmp_path, "x" * 600))


@pytest.mark.parametrize(
    "content",
    ["not-a-key", "sk-ant-short", "sk-ant-" + "x" * 30 + " extra words", b"\xff\xfe binary", ""],
)
def test_invalid_contents_are_refused_without_echoing(tmp_path: Path, content: str | bytes) -> None:
    with pytest.raises(KeyFileError) as info:
        load_api_key(key_file(tmp_path, content))
    text = content.decode(errors="replace") if isinstance(content, bytes) else content
    if text.strip():
        assert text.strip() not in str(info.value)


def test_api_key_validates_directly() -> None:
    with pytest.raises(KeyFileError):
        ApiKey("sk-live-123")


# ── Environment ──────────────────────────────────────────────────────────


def test_clean_environment() -> None:
    assert (
        environment_problems(
            {"PATH": "/usr/bin", "ANTHROPIC_BASE_URL": "https://api.anthropic.com/"}
        )
        == []
    )


@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        ({"ANTHROPIC_API_KEY": "x"}, "ANTHROPIC_API_KEY"),
        ({"ANTHROPIC_AUTH_TOKEN": "x"}, "ANTHROPIC_AUTH_TOKEN"),
        ({"ANTHROPIC_PROFILE": "work"}, "ANTHROPIC_PROFILE"),
        ({"ANTHROPIC_BASE_URL": "https://evil.example.com"}, "ANTHROPIC_BASE_URL"),
        ({"ANTHROPIC_LOG": "debug"}, "ANTHROPIC_LOG"),
        ({"ANTHROPIC_LOG": " INFO "}, "ANTHROPIC_LOG"),
    ],
)
def test_unsafe_environment(env: dict[str, str], fragment: str) -> None:
    (problem,) = environment_problems(env)
    assert fragment in problem


def test_empty_values_are_ignored() -> None:
    assert environment_problems({"ANTHROPIC_API_KEY": "", "ANTHROPIC_LOG": "warning"}) == []
