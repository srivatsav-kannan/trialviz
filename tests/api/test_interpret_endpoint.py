"""The HTTP contract for /v1/interpret: success shapes and the single error envelope."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trialviz.api import create_app
from trialviz.query.interpreters.rules import RuleBasedInterpreter


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(interpreter=RuleBasedInterpreter()))


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_the_assignment_example_resolves_to_a_time_series(client: TestClient) -> None:
    response = client.post(
        "/v1/interpret",
        json={
            "query": "How has the number of trials for this drug changed over time?",
            "drug_name": "Pembrolizumab",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["plan"]["filters"]["interventions"] == ["Pembrolizumab"]
    assert body["plan"]["analysis"] == {
        "kind": "time_series",
        "time_field": "start_date",
        "granularity": "year",
        "series_by": None,
    }
    assert body["provenance"]["filters.interventions"] == "user"
    assert body["provenance"]["analysis.kind"] == "inferred"
    assert body["interpreter"] == "rules"


def test_a_clarification_is_a_200_with_a_different_status(client: TestClient) -> None:
    response = client.post("/v1/interpret", json={"query": "How many trials for this drug?"})
    assert response.status_code == 200
    assert response.json()["status"] == "needs_clarification"
    assert response.json()["reason"] == "ambiguous_reference"


def test_validation_errors_share_one_envelope_with_readable_paths(client: TestClient) -> None:
    response = client.post(
        "/v1/interpret",
        json={
            "query": "q",
            "filters": {"phases": ["Phase 9"], "countries": ["Korea"]},
            "drug_nmae": "x",
        },
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    paths = {detail["path"]: detail["message"] for detail in error["details"]}
    # Phases are parsed as a list (so "1/2" can expand), so the error points at the list.
    assert "Unknown phase 'Phase 9'" in paths["filters.phases"]
    assert "South Korea or North Korea" in paths["filters.countries[0]"]
    assert paths["drug_nmae"] == "Extra inputs are not permitted"


def test_the_openapi_document_publishes_both_outcome_shapes(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert {"ResolvedQuery", "ClarificationNeeded", "QueryRequest"} <= set(schemas)
