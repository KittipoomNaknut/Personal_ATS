#!/usr/bin/env python3
"""Security checklist before (and after) using a real Anthropic API key.

Run from the repository root:  uv run python scripts/security_checklist.py [--after-run]

Automated checks print PASS / FAIL. Checks only you can do are listed as
MANUAL. Nothing here reads the key, the keyring or your shell history.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from ats_gateway.control import Paths

ROOT = Path(__file__).resolve().parents[1]
KEY_PATTERN = re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}")
results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def manual(name: str) -> None:
    print(f"[MANUAL] {name}")


def run(*command: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, cwd=ROOT, capture_output=True, check=False)  # noqa: S603


def gitleaks_binary() -> str | None:
    found = shutil.which("gitleaks")
    if found:
        return found
    cache = Path.home() / ".cache" / "pre-commit"
    candidates = sorted(p for p in cache.rglob("gitleaks") if p.is_file() and os.access(p, os.X_OK))
    return str(candidates[0]) if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-run", action="store_true", help="also scan logs after a live run")
    args = parser.parse_args()

    # 1. Repository history has no secrets.
    binary = gitleaks_binary()
    if binary is None:
        check(
            "gitleaks over the full git history", False, "gitleaks not found (run pre-commit once)"
        )
    else:
        scan = run(binary, "git", "--redact", "--no-banner", "--exit-code", "1", ".")
        check("gitleaks over the full git history", scan.returncode == 0)
    history = run("git", "log", "-p", "--all")
    check("no key-shaped string anywhere in git history", not KEY_PATTERN.search(history.stdout))

    # 2. No credentials in this shell's environment (names only are inspected).
    leaked = [n for n in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN") if os.environ.get(n)]
    check("no API key in the environment", not leaked, ", ".join(leaked))

    # 3. PII redaction is fully covered by its tests.
    tests = run(
        "uv", "run", "pytest", "packages/ats_core/tests/test_pii.py", "-q",
        "-p", "no:cacheprovider", "-o", "addopts=",  # drop the project-wide --cov settings
        "--cov=ats_core.pii", "--cov-report=term", "--cov-fail-under=100",
    )  # fmt: skip
    check("PII redaction tests pass with 100% coverage", tests.returncode == 0)

    # 4. Local data is private.
    paths = Paths.from_env(os.environ)  # exactly where atsctl reads and writes
    data = paths.data
    if data.exists():
        mode = stat.S_IMODE(data.stat().st_mode)
        check(f"data directory is private ({data})", mode & 0o077 == 0, f"mode {mode:o}")
        ledger = data / "ledger.db"
        if ledger.exists():
            mode = stat.S_IMODE(ledger.stat().st_mode)
            check("ledger file is private", mode & 0o077 == 0, f"mode {mode:o}")
    else:
        print(f"[INFO] data directory not created yet ({data})")

    # 5. After a live run: no key in any log or local file the project wrote.
    if args.after_run:
        scanned = [
            p
            for root in (paths.state, data)
            if root.exists()
            for p in root.rglob("*")
            if p.is_file()
        ]
        hits = [str(p) for p in scanned if KEY_PATTERN.search(p.read_bytes())]
        check(
            f"no key in logs or local data ({len(scanned)} files scanned)",
            not hits,
            ", ".join(hits),
        )
        check("no key file left in the runtime directory", not paths.key_file.exists())

    manual("Anthropic Console: a separate workspace 'personal-ats' with a monthly spend limit")
    manual("the key was created in that workspace and stored only with `secret-tool store`")
    manual("shell history has no key: run `history | grep -c sk-ant` yourself; it should print 0")
    print(f"\n{sum(results)}/{len(results)} automated checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
