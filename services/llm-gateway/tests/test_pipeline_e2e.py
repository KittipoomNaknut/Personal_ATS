"""Whole path: pipeline -> GatewayClient -> unix socket -> real gateway -> real SDK -> fake API.

The gateway and the pipeline open the ledger file separately, as the two
processes do in real use.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from ats_gateway.app import GatewayState, create_app
from ats_gateway.caller import ClaudeCaller
from ats_gateway.server import bind_private_socket
from fakes import FakeApi, message_json, usage

from ats_core.analysis.models import Verdict
from ats_core.billing.budget import DailyBudget
from ats_core.billing.ledger import Ledger
from ats_core.billing.pricing import default_price_table
from ats_core.gateway_client import GatewayClient, GatewayError
from ats_core.pipeline import run_ai_analysis

RESUME = (
    "Somchai Example\nsomchai@example.com\nExperience\n"
    "Engineer, Co. Jan 2024 - Present\n- Built APIs in Python\n"
)
JD = "Requirements\n- Python\nContact jobs@company.example"
PARSED_JD = {
    "job_title": "Engineer",
    "requirements": [
        {
            "text": "Python",
            "kind": "skill",
            "importance": "required",
            "min_years": None,
            "knockout": False,
        }
    ],
}
STRUCTURE = {
    "jobs": [
        {
            "title": "Engineer",
            "organization": "Co.",
            "start": "Jan 2024",
            "end": "Present",
            "bullet_ids": ["b4", "b5"],
        }
    ],
    "education": [],
    "certifications": [],
    "languages": [],
}


def answer(api: FakeApi, data: dict[str, Any], tokens: tuple[int, int] = (1_000, 200)) -> None:
    api.reply(message_json(text=json.dumps(data), usage_json=usage(*tokens)))


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[tuple[GatewayClient, FakeApi, Path]]:
    directory = tmp_path / "run"
    directory.mkdir(mode=0o700)
    ledger_path = tmp_path / "ledger.db"
    api = FakeApi(counted_tokens=1_000)
    gateway_ledger = Ledger(ledger_path)
    caller = ClaudeCaller(client=api.client(), ledger=gateway_ledger, prices=default_price_table())
    app = create_app(GatewayState(caller=caller, ledger=gateway_ledger, budget=DailyBudget()))
    socket_path = directory / "gateway.sock"
    listener = bind_private_socket(socket_path)
    server = uvicorn.Server(uvicorn.Config(app, fd=listener.fileno(), log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "gateway did not start"
        time.sleep(0.02)
    try:
        yield GatewayClient(socket_path, timeout=30), api, ledger_path
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        gateway_ledger.close()


def test_end_to_end_analysis(gateway: tuple[GatewayClient, FakeApi, Path]) -> None:
    client, api, ledger_path = gateway
    answer(api, PARSED_JD)
    answer(api, STRUCTURE)
    for _ in range(2):  # a required requirement is judged twice
        answer(
            api,
            {
                "verdict": "met",
                "evidence": [{"bullet_id": "b5", "quote": "in Python"}],
                "rationale": "ok",
            },
        )

    with Ledger(ledger_path) as ledger:
        owner = ledger.add_client("Bee")
        analysis = run_ai_analysis(
            RESUME,
            JD,
            client,
            ledger,
            parseability=1.0,
            keyword_coverage=1.0,
            client_id=owner.id,
            today=date(2026, 11, 27),
        )
        assert [r.consensus.verdict for r in analysis.results] == [Verdict.MET]
        assert analysis.total_years == 2.9  # Jan 2024 - Nov 2026, computed in code
        # 4 calls x (1k x $4 + 200 x $20) per million = 4 x $0.008, recorded by the gateway.
        assert str(analysis.cost_usd) == "0.032"
        assert ledger.analysis_cost(analysis.analysis_id) == analysis.cost_usd

    sent = json.dumps(api.created)
    for secret in ("Somchai", "somchai@example.com", "jobs@company.example"):
        assert secret not in sent
    assert len(api.created) == 4


def test_gateway_refusal_reaches_the_pipeline(gateway: tuple[GatewayClient, FakeApi, Path]) -> None:
    client, api, ledger_path = gateway
    api.reply(message_json(text="not json"))
    with Ledger(ledger_path) as ledger, pytest.raises(GatewayError) as info:
        run_ai_analysis(RESUME, JD, client, ledger, parseability=1, keyword_coverage=None)
    assert info.value.code == "invalid_output"
    assert info.value.cost_usd is not None
    with Ledger(ledger_path) as ledger:
        (status,) = ledger._conn.execute("SELECT status FROM analyses").fetchone()
    assert status == "failed"
