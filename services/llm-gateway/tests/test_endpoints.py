"""Task endpoints end to end: HTTP request -> gateway -> real SDK -> fake API -> ledger."""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ats_gateway.app import GatewayState, create_app
from ats_gateway.caller import ClaudeCaller
from fakes import FakeApi, message_json, usage
from fastapi.testclient import TestClient

from ats_core.billing.budget import DailyBudget
from ats_core.billing.ledger import Ledger
from ats_core.billing.pricing import default_price_table

BULLETS = [
    {"id": "b1", "text": "[NAME_1]"},
    {"id": "b2", "text": "Built FastAPI services in Python"},
]
REQUIREMENT = {
    "id": "R1",
    "text": "Python",
    "kind": "skill",
    "importance": "required",
    "min_years": 2,
}
JUDGE_ANSWER = {
    "verdict": "met",
    "evidence": [{"bullet_id": "b2", "quote": "in Python"}],
    "rationale": "ใช้ Python ในงาน",
}


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "ledger.db") as opened:
        yield opened


@pytest.fixture
def api() -> FakeApi:
    return FakeApi(counted_tokens=2_000)


@pytest.fixture
def client(api: FakeApi, ledger: Ledger) -> TestClient:
    caller = ClaudeCaller(client=api.client(), ledger=ledger, prices=default_price_table())
    return TestClient(create_app(GatewayState(caller=caller, ledger=ledger, budget=DailyBudget())))


def reply(api: FakeApi, data: Any, **kwargs: Any) -> None:
    api.reply(message_json(text=json.dumps(data, ensure_ascii=False), **kwargs))


def test_judge_endpoint(client: TestClient, api: FakeApi, ledger: Ledger) -> None:
    owner = ledger.add_client("A")
    analysis = ledger.start_analysis(client_id=owner.id)
    reply(api, JUDGE_ANSWER, usage_json=usage(1_000, 300, cache_read=2_000))
    response = client.post(
        "/v1/tasks/judge",
        json={
            "requirement": REQUIREMENT,
            "bullets": BULLETS,
            "total_years": 3.5,
            "language": "th",
            "analysis_id": analysis,
            "client_id": owner.id,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == JUDGE_ANSWER
    assert body["prompt_version"] == "v1"
    assert body["attempts"] == 1
    # 1k x $4 + 2k cached x $0.20 + 300 x $20, per million tokens
    assert Decimal(body["cost_usd"]) == Decimal("0.0104")
    assert ledger.analysis_cost(analysis) == Decimal("0.0104")
    (sent,) = api.created
    assert sent["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    row = ledger._conn.execute("SELECT task FROM llm_calls").fetchone()
    assert row["task"] == "judge.v1"


def test_parse_jd_endpoint(client: TestClient, api: FakeApi) -> None:
    answer = {
        "job_title": "Backend Engineer",
        "requirements": [
            {
                "text": "Python",
                "kind": "skill",
                "importance": "required",
                "min_years": 3,
                "knockout": False,
            },
            {
                "text": "Kafka",
                "kind": "skill",
                "importance": "preferred",
                "min_years": None,
                "knockout": False,
            },
        ],
    }
    reply(api, answer)
    response = client.post(
        "/v1/tasks/parse-jd",
        json={"jd_text": "Requirements\n- Python 3+ years\nNice to have\n- Kafka"},
    )
    assert response.status_code == 200
    assert response.json()["result"] == answer
    assert api.created[0]["output_config"]["effort"] == "high"


def test_structure_endpoint(client: TestClient, api: FakeApi) -> None:
    answer = {
        "jobs": [
            {
                "title": "Engineer",
                "organization": "Co",
                "start": "Jan 2021",
                "end": "Present",
                "bullet_ids": ["b2"],
            }
        ],
        "education": [],
        "certifications": [],
        "languages": [],
    }
    reply(api, answer)
    response = client.post("/v1/tasks/structure-resume", json={"bullets": BULLETS})
    assert response.status_code == 200
    assert response.json()["result"]["jobs"][0]["start"] == "Jan 2021"


# ── Errors ───────────────────────────────────────────────────────────────


def test_pii_is_refused_with_422(client: TestClient, api: FakeApi) -> None:
    bullets = [{"id": "b1", "text": "contact me: somchai@example.com"}]
    response = client.post("/v1/tasks/structure-resume", json={"bullets": bullets})
    assert response.status_code == 422
    assert response.json()["error"] == "pii"
    assert response.json()["kinds"] == ["EMAIL"]
    assert api.requests == []


def test_budget_refusal_is_429(client: TestClient, api: FakeApi, ledger: Ledger) -> None:
    from ats_core.billing.ledger import CallRecord
    from ats_core.billing.pricing import Usage

    spent = Usage(output_tokens=99_000)
    ledger.record_call(
        CallRecord(
            task="x",
            requested_model="m",
            usage=spent,
            cost=default_price_table().cost("claude-opus-5-5", spent),
        )
    )
    response = client.post("/v1/tasks/judge", json={"requirement": REQUIREMENT, "bullets": BULLETS})
    assert response.status_code == 429
    body = response.json()
    assert body["error"] == "budget"
    assert body["spent_usd"] == "1.98"
    assert api.created == []


def test_unknown_references_are_refused_before_spending(client: TestClient, api: FakeApi) -> None:
    for field in ("client_id", "analysis_id"):
        response = client.post(
            "/v1/tasks/judge", json={"requirement": REQUIREMENT, "bullets": BULLETS, field: "nope"}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "unknown_reference"
    assert api.requests == []


def test_invalid_model_output_is_502_but_cost_is_recorded(
    client: TestClient, api: FakeApi, ledger: Ledger
) -> None:
    api.reply(message_json(text="not json"))
    response = client.post("/v1/tasks/judge", json={"requirement": REQUIREMENT, "bullets": BULLETS})
    assert response.status_code == 502
    body = response.json()
    assert body["error"] == "invalid_output"
    assert Decimal(body["cost_usd"]) > 0
    assert ledger.spent_today() == Decimal(body["cost_usd"])


def test_refusal_is_422_with_category(client: TestClient, api: FakeApi) -> None:
    api.reply(
        message_json(
            stop_reason="refusal",
            content=[],
            stop_details={"type": "refusal", "category": "cyber"},
            usage_json=usage(500, 0),
        )
    )
    response = client.post("/v1/tasks/judge", json={"requirement": REQUIREMENT, "bullets": BULLETS})
    assert response.status_code == 422
    assert response.json()["category"] == "cyber"
    assert response.json()["cost_usd"] == "0.00"


def test_truncated_answer_is_502(client: TestClient, api: FakeApi) -> None:
    api.reply(message_json(stop_reason="max_tokens", usage_json=usage(100, 4_000)))
    response = client.post("/v1/tasks/judge", json={"requirement": REQUIREMENT, "bullets": BULLETS})
    assert response.status_code == 502
    assert response.json()["error"] == "truncated"


def test_upstream_errors(client: TestClient, api: FakeApi) -> None:
    api.reply(
        {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}, status=529
    )
    response = client.post("/v1/tasks/judge", json={"requirement": REQUIREMENT, "bullets": BULLETS})
    assert response.status_code == 502
    assert response.json()["error"] == "upstream"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/tasks/parse-jd", {"jd_text": ""}),
        ("/v1/tasks/parse-jd", {"jd_text": "x" * 50_001}),
        ("/v1/tasks/structure-resume", {"bullets": []}),
        ("/v1/tasks/judge", {"requirement": {**REQUIREMENT, "kind": "magic"}, "bullets": BULLETS}),
        ("/v1/tasks/judge", {"requirement": REQUIREMENT, "bullets": BULLETS, "prompt": "override"}),
        ("/v1/tasks/judge", {"requirement": REQUIREMENT, "bullets": BULLETS, "language": "fr"}),
    ],
)
def test_request_validation(
    client: TestClient, api: FakeApi, path: str, payload: dict[str, Any]
) -> None:
    assert client.post(path, json=payload).status_code == 422
    assert api.requests == []


def test_health_reports_prompt_version(client: TestClient) -> None:
    assert client.get("/health").json()["prompt_version"] == "v1"
