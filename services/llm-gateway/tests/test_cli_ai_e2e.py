"""``ats check --ai`` against the real gateway on a unix socket (fake Claude API)."""

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from ats_gateway.app import GatewayState, create_app
from ats_gateway.caller import ClaudeCaller
from ats_gateway.control import Paths
from ats_gateway.server import bind_private_socket
from fakes import FakeApi, message_json, usage

from ats_core.billing.budget import DailyBudget
from ats_core.billing.ledger import Ledger
from ats_core.billing.pricing import default_price_table
from ats_core.cli import EXIT_CANCELLED, EXIT_OK, main
from ats_core.paths import data_dir, ledger_path

RESUME = (
    Path(__file__).resolve().parents[3]
    / "packages/ats_core/tests/fixtures/libreoffice/en_clean.pdf"
)


def _requirement(text: str, importance: str, min_years: float | None) -> dict[str, Any]:
    return {
        "text": text,
        "kind": "skill",
        "importance": importance,
        "min_years": min_years,
        "knockout": False,
    }


def _job(title: str, start: str, end: str, bullet_ids: list[str]) -> dict[str, Any]:
    return {
        "title": title,
        "organization": "",
        "start": start,
        "end": end,
        "bullet_ids": bullet_ids,
    }


PARSED = {
    "job_title": "Backend Engineer",
    "requirements": [
        _requirement("Python", "required", 3),
        _requirement("Kafka", "preferred", None),
    ],
}
# Line ids of en_clean.pdf: b6-b8 are the 2021 job (FastAPI, not the word "Python"),
# b9-b10 the 2018 job ("data pipelines with Python").
STRUCTURE = {
    "jobs": [
        _job("Senior Software Engineer", "Jan 2021", "Present", ["b6", "b7", "b8"]),
        _job("Software Engineer", "Jun 2018", "Dec 2020", ["b9", "b10"]),
    ],
    "education": [],
    "certifications": [],
    "languages": [],
}
PYTHON_MET = {
    "verdict": "met",
    "evidence": [{"bullet_id": "b14", "quote": "Python, FastAPI"}],
    "rationale": "[NAME_1] lists Python",
}
KAFKA_NOT_MET = {"verdict": "not_met", "evidence": [], "rationale": "Kafka is not mentioned"}


def reply(api: FakeApi, data: dict[str, Any]) -> None:
    api.reply(message_json(text=json.dumps(data), usage_json=usage(1_000, 200)))


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[dict[str, str], FakeApi, Path]]:
    env = {
        "ATS_RUNTIME_DIR": str(tmp_path / "run"),
        "ATS_DATA_DIR": str(tmp_path / "data"),
        "HOME": str(tmp_path),
    }
    Path(env["ATS_RUNTIME_DIR"]).mkdir(mode=0o700)
    api = FakeApi(counted_tokens=1_000)
    ledger = Ledger(ledger_path(env))
    caller = ClaudeCaller(client=api.client(), ledger=ledger, prices=default_price_table())
    app = create_app(GatewayState(caller=caller, ledger=ledger, budget=DailyBudget()))
    listener = bind_private_socket(Path(env["ATS_RUNTIME_DIR"]) / "gateway.sock")
    server = uvicorn.Server(uvicorn.Config(app, fd=listener.fileno(), log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    jd = tmp_path / "jd.txt"
    jd.write_text(
        "Requirements\n- Python 3+ years\nNice to have\n- Kafka\nContact hr@company.example"
    )
    try:
        yield env, api, jd
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        ledger.close()


def answer_full_run(api: FakeApi) -> None:
    reply(api, PARSED)
    reply(api, STRUCTURE)
    reply(api, PYTHON_MET)
    reply(api, PYTHON_MET)  # required: judged twice
    reply(api, KAFKA_NOT_MET)


def test_check_with_ai_in_english(
    setup: tuple[dict[str, str], FakeApi, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    env, api, jd = setup
    answer_full_run(api)
    out = io.StringIO()
    code = main(
        [
            "check",
            str(RESUME),
            "--jd",
            str(jd),
            "--ai",
            "--client",
            "น้องเอ",
            "--lang",
            "en",
            "--yes",
        ],
        out=out,
        env=env,
    )
    assert code == EXIT_OK, capsys.readouterr().err
    text = out.getvalue()
    assert "AI match (Claude):" in text
    # Python years count only jobs whose lines name Python: Jun 2018 - Dec 2020.
    assert "[x] R1 Python  (3+ years required, 2.6 found)" in text
    assert '        evidence: "Python, FastAPI, PostgreSQL, Docker, Kubernetes, AWS, Git"' in text
    assert "        reason: [NAME_1] lists Python" in text
    assert "[ ] R2 Kafka" in text
    # 5 calls x (1k x $4 + 200 x $20) per million = $0.04
    assert "Cost of this analysis: 0.040 USD (client: น้องเอ)" in text
    err = capsys.readouterr().err
    assert "Estimated cost of judging 3-4 calls" in err
    assert "Daily budget remaining: 2.00 USD" in err
    for secret in ("Somchai", "somchai@example.com", "081-234-5678", "hr@company.example"):
        assert secret not in text
        assert secret not in json.dumps(api.created)


def test_check_with_ai_json_in_thai(setup: tuple[dict[str, str], FakeApi, Path]) -> None:
    env, api, jd = setup
    answer_full_run(api)
    out = io.StringIO()
    assert (
        main(["check", str(RESUME), "--jd", str(jd), "--ai", "--json", "--yes"], out=out, env=env)
        == EXIT_OK
    )
    data = json.loads(out.getvalue())
    ai = data["ai"]
    assert [r["verdict"] for r in ai["requirements"]] == ["met", "not_met"]
    assert ai["requirements"][0]["votes"] == ["met", "met"]
    assert ai["requirements"][0]["years"] == 2.6
    assert ai["total_years"] == 8.5
    assert ai["cost_usd"] == "0.040"
    assert data["client"] is None


def test_declining_the_estimate_cancels(
    setup: tuple[dict[str, str], FakeApi, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    env, api, jd = setup
    reply(api, PARSED)
    reply(api, STRUCTURE)
    code = main(
        ["check", str(RESUME), "--jd", str(jd), "--ai"],
        out=io.StringIO(),
        env=env,
        confirm=lambda _q: False,
    )
    assert code == EXIT_CANCELLED
    assert "cancelled; 0.016 USD was spent reading the documents" in capsys.readouterr().err
    assert len(api.created) == 2  # nothing was judged
    with Ledger(ledger_path(env)) as ledger:
        (status,) = ledger._conn.execute("SELECT status FROM analyses").fetchone()
    assert status == "cancelled"


def test_paths_agree_with_atsctl() -> None:
    for env in (
        {"HOME": "/home/u", "XDG_RUNTIME_DIR": "/run/user/7"},
        {"HOME": "/home/u", "XDG_RUNTIME_DIR": "/r", "XDG_DATA_HOME": "/data"},
        {"HOME": "/home/u", "ATS_RUNTIME_DIR": "/r", "ATS_DATA_DIR": "/d"},
    ):
        assert data_dir(env) == Paths.from_env(env).data
        assert ledger_path(env) == Paths.from_env(env).ledger
