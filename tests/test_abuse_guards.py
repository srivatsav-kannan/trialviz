"""What stops one client, or many, from overwhelming the service or spending its budget.

Each test drives the real application over HTTP, with limits set low enough to reach, and
checks what a client would see: the status code, the one error shape, `Retry-After`, the
RateLimit headers, and its standing in `GET /v1/status`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
from fastapi.testclient import TestClient

from trialviz.api import create_app
from trialviz.query.interpreters.base import Interpretation
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.request import QueryRequest
from trialviz.settings import Settings

QUESTION = {"query": "How are melanoma trials distributed across phases?"}


def _settings(**overrides: Any) -> Settings:
    # One trusted proxy in front, as on the deployed service, so each test can choose the
    # client's address through X-Forwarded-For.
    base: dict[str, Any] = {
        "anthropic_api_key": None,
        "ctgov_cache_dir": None,
        "answer_store_dir": None,
    }
    return Settings(_env_file=None, trusted_proxy_hops=1, **{**base, **overrides})  # type: ignore[call-arg]


def _client(**overrides: Any) -> TestClient:
    return TestClient(create_app(_settings(**overrides), interpreter=RuleBasedInterpreter()))


def _as(address: str, forged: str | None = None) -> dict[str, str]:
    return {"x-forwarded-for": f"{forged}, {address}" if forged else address}


def _error(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message", "details"}
    error: dict[str, Any] = body["error"]
    return error


def test_each_client_has_its_own_question_quota_and_cannot_borrow_another() -> None:
    client = _client(questions_per_minute=3)
    answered = [
        client.post("/v1/interpret", json=QUESTION, headers=_as("203.0.113.1")) for _ in range(3)
    ]
    assert [r.status_code for r in answered] == [200, 200, 200]
    assert [r.headers["ratelimit-remaining"] for r in answered] == ["2", "1", "0"]

    refused = client.post("/v1/interpret", json=QUESTION, headers=_as("203.0.113.1"))
    assert refused.status_code == 429 and _error(refused)["code"] == "rate_limited"
    assert 1 <= int(refused.headers["retry-after"]) <= 60
    # Writing another address in front of the one the proxy added changes nothing.
    forged = client.post("/v1/interpret", json=QUESTION, headers=_as("203.0.113.1", "198.51.100.9"))
    assert forged.status_code == 429
    # A different client is unaffected, and /v1/query shares the same quota.
    assert (
        client.post("/v1/interpret", json=QUESTION, headers=_as("203.0.113.2")).status_code == 200
    )
    assert client.post("/v1/query", json=QUESTION, headers=_as("203.0.113.1")).status_code == 429

    status = client.get("/v1/status", headers=_as("203.0.113.1")).json()
    assert status["client"] == "203.0.113.1"
    assert status["limits"]["questions_remaining"] == 0
    assert status["limits"]["resets_in_seconds"] > 0


def test_a_ceiling_bounds_questions_from_every_client_together() -> None:
    client = _client(questions_per_hour_total=4)
    for index in range(4):
        response = client.post("/v1/interpret", json=QUESTION, headers=_as(f"203.0.113.{index}"))
        assert response.status_code == 200
    refused = client.post("/v1/interpret", json=QUESTION, headers=_as("203.0.113.99"))
    assert refused.status_code == 429 and "across all clients" in _error(refused)["message"]
    # The refused client's own quota was not spent on a question that never ran.
    status = client.get("/v1/status", headers=_as("203.0.113.99")).json()
    assert status["limits"]["questions_remaining"] == status["limits"]["questions_per_minute"]


class _Slow:
    """Reads like the rule-based interpreter, after a pause long enough to overlap."""

    name = "slow rules"

    async def interpret(self, request: QueryRequest, today: date) -> Interpretation:
        await asyncio.sleep(0.3)
        return await RuleBasedInterpreter().interpret(request, today)


async def test_only_a_few_questions_are_answered_at_once() -> None:
    app = create_app(_settings(max_concurrent_answers=1), interpreter=_Slow())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        responses = await asyncio.gather(
            *(
                http.post("/v1/interpret", json=QUESTION, headers=_as(f"203.0.113.{index}"))
                for index in range(3)
            )
        )
    assert sorted(r.status_code for r in responses) == [200, 503, 503]
    busy = next(r for r in responses if r.status_code == 503)
    assert _error(busy)["code"] == "busy" and int(busy.headers["retry-after"]) > 0


def test_oversized_bodies_are_refused_before_they_are_read() -> None:
    client = _client(max_body_bytes=2048)
    declared = client.post(
        "/v1/interpret",
        json={"query": "x" * 1500, "filters": {"keywords": ["y" * 600]}},
        headers=_as("203.0.113.5"),
    )
    assert declared.status_code == 413 and _error(declared)["code"] == "request_too_large"

    def chunks() -> Iterator[bytes]:
        yield b'{"query": "'
        yield b"x" * 4000
        yield b'"}'

    # A body sent without a declared length is counted as it arrives.
    streamed = client.post(
        "/v1/interpret",
        content=chunks(),
        headers={"content-type": "application/json", **_as("203.0.113.5")},
    )
    assert streamed.status_code == 413
