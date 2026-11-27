"""Only the gateway may talk to the Claude API (ARCHITECTURE §2.4)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
IMPORT_ANTHROPIC = re.compile(r"^\s*(?:import|from)\s+anthropic\b", re.MULTILINE)


def test_core_package_never_imports_anthropic() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "packages").rglob("*.py")
        if IMPORT_ANTHROPIC.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_only_the_gateway_depends_on_anthropic() -> None:
    owners = []
    for pyproject in [
        *(ROOT / "packages").glob("*/pyproject.toml"),
        *(ROOT / "services").glob("*/pyproject.toml"),
    ]:
        deps = tomllib.loads(pyproject.read_text())["project"].get("dependencies", [])
        if any(re.match(r"anthropic\b", dep) for dep in deps):
            owners.append(pyproject.parent.name)
    assert owners == ["llm-gateway"]
