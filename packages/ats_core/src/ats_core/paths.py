"""Where the project keeps its local data (XDG base directories).

Must agree with ``atsctl`` (``ats_gateway.control.Paths``); a test checks that.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

__all__ = ["data_dir", "ledger_path"]


def data_dir(env: Mapping[str, str]) -> Path:
    override = env.get("ATS_DATA_DIR")
    if override:
        return Path(override)
    home = Path(env.get("HOME", str(Path.home())))
    return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "personal-ats"


def ledger_path(env: Mapping[str, str]) -> Path:
    return data_dir(env) / "ledger.db"
