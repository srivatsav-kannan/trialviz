"""Whole requests under the conditions that break pipelines: throttling, outages, a stale
cache, an interpreter that lies, and an exhausted budget."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tests.workflows.cassette import RECORDING, Cassette
from tests.workflows.harness import run_workflow
from trialviz.llm.budget import SpendLedger
from trialviz.pipeline import Pipeline
from trialviz.query.interpreters.base import FallbackInterpreter, Interpretation
from trialviz.query.interpreters.claude import ClaudeInterpreter
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import NoteCode, ResolvedQuery
from trialviz.query.resolver import QueryResolver
from trialviz.retrieval.client import (
    RateLimiter,
    RegistryClient,
    RegistryUnavailable,
    ResponseCache,
)
from trialviz.retrieval.retriever import Retriever

pytestmark = pytest.mark.skipif(
    RECORDING, reason="replays cassettes recorded by the question workflows"
)

COMPARISON = {"query": "Compare phases for trials involving Keytruda vs Opdivo since 2018"}
ASSIGNMENT = {
    "query": "How has the number of trials for this drug changed over time?",
    "drug_name": "Pembrolizumab",
}


def study_ids(run: Any) -> list[list[str]]:
    return [
        [s["protocolSection"]["identificationModule"]["nctId"] for s in cohort.studies]
        for cohort in run.result.retrieval.cohorts
    ]


async def test_throttling_and_server_errors_mid_run_change_nothing_but_time(tmp_path: Path) -> None:
    clean = await run_workflow(Cassette("comparison_keytruda_opdivo"), COMPARISON, tmp_path)
    rough = await run_workflow(
        Cassette("comparison_keytruda_opdivo"),
        COMPARISON,
        tmp_path,
        registry_failures={2: 429, 5: 503, 6: 503},
    )
    assert study_ids(rough) == study_ids(clean)
    assert [c.total for c in rough.result.retrieval.cohorts] == [
        c.total for c in clean.result.retrieval.cohorts
    ]
    assert sorted(s for s in rough.sleeps.calls if s >= 1) == [2.0, 2.0, 4.0]


async def test_an_outage_is_an_error_never_a_partial_answer(tmp_path: Path) -> None:
    with pytest.raises(RegistryUnavailable, match="HTTP 503"):
        await run_workflow(
            Cassette("comparison_keytruda_opdivo"),
            COMPARISON,
            tmp_path,
            registry_failures={index: 503 for index in range(3, 7)},
        )


async def test_the_cache_serves_repeats_and_misses_after_a_registry_refresh(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "cache")
    first = await run_workflow(Cassette("assignment_example"), ASSIGNMENT, tmp_path, cache=cache)
    repeat = await run_workflow(Cassette("assignment_example"), ASSIGNMENT, tmp_path, cache=cache)
    assert [key.split("?")[0] for key in repeat.cassette.sent] == ["/api/v2/version"]
    assert study_ids(repeat) == study_ids(first)

    refreshed = Cassette("assignment_example")
    refreshed.registry["/api/v2/version?"]["dataTimestamp"] = "2026-11-08T10:00:05"
    after_refresh = await run_workflow(refreshed, ASSIGNMENT, tmp_path, cache=cache)
    assert len(after_refresh.cassette.sent) == len(first.cassette.sent)


class _LyingInterpreter:
    """Contradicts the caller, invents a phase, and proposes an inverted year range."""

    name = "liar"

    async def interpret(self, request: QueryRequest, today: date) -> Interpretation:
        return Interpretation(
            interpreter=self.name,
            proposal={
                "filters": {
                    "interventions": ["nivolumab"],
                    "phases": ["PHASE5"],
                    "start_year_to": 2010,
                    "countries": ["Atlantis"],
                },
                "analysis": {"kind": "count", "group_by": "phase"},
            },
        )


class _RecordingRegistry:
    """Answers every query with an empty result and keeps what it was asked."""

    def __init__(self) -> None:
        self.params: list[dict[str, str]] = []

    def client(self) -> Any:
        import httpx

        def answer(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/version"):
                return httpx.Response(200, json={"dataTimestamp": "v1"})
            self.params.append(dict(request.url.params))
            return httpx.Response(200, json={"studies": [], "totalCount": 0})

        return httpx.AsyncClient(transport=httpx.MockTransport(answer))


async def test_a_lying_interpreter_cannot_change_what_is_sent_to_the_registry() -> None:
    registry = _RecordingRegistry()
    pipeline = Pipeline(
        QueryResolver(_LyingInterpreter(), today=lambda: date(2026, 11, 7)),
        Retriever(RegistryClient(registry.client(), RateLimiter(1e9)), max_records=100),
    )
    result = await pipeline.run(
        QueryRequest.model_validate(
            {"query": "q", "drug_name": "pembrolizumab", "start_year": 2015}
        )
    )
    resolution = result.resolution
    assert isinstance(resolution, ResolvedQuery)
    assert {(n.code, n.slot) for n in resolution.notes} == {
        (NoteCode.FIELD_OVERRIDES_QUESTION, "filters.interventions"),
        (NoteCode.INFERRED_VALUE_REJECTED, "filters.phases"),
        (NoteCode.INFERRED_VALUE_REJECTED, "filters.countries"),
        (NoteCode.INFERRED_VALUE_REJECTED, "filters.start_year_to"),
        (NoteCode.INFERRED_VALUE_NOT_APPLICABLE, "analysis.group_by"),
    }
    named, mentions = (
        {k: v for k, v in p.items() if k.startswith(("query.", "filter."))} for p in registry.params
    )
    strict = (
        "(AREA[InterventionName](pembrolizumab) OR "
        "AREA[InterventionOtherName](pembrolizumab) OR AREA[InterventionMeshTerm](pembrolizumab))"
    )
    assert named == {
        "query.intr": strict,
        "filter.advanced": "(AREA[StartDate]RANGE[2015-01-01, MAX])",
    }
    assert mentions == {
        "query.intr": "(pembrolizumab)",
        "filter.advanced": f"(AREA[StartDate]RANGE[2015-01-01, MAX]) AND (NOT ({strict}))",
    }


class _NeverCalled:
    async def create(self, **_: object) -> object:
        raise AssertionError("Claude must not be called once the cap is reached")


async def test_at_the_spend_cap_questions_resolve_without_calling_claude(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=20.0)
    await ledger.settle(0.0, 19.95)
    interpreter = FallbackInterpreter(
        ClaudeInterpreter(
            _NeverCalled(), ledger, model="claude-opus-5-5", effort="low", max_tokens=8000
        ),
        RuleBasedInterpreter(),
    )
    registry = _RecordingRegistry()
    pipeline = Pipeline(
        QueryResolver(interpreter, today=lambda: date(2026, 11, 7)),
        Retriever(RegistryClient(registry.client(), RateLimiter(1e9)), max_records=100),
    )
    result = await pipeline.run(
        QueryRequest(query="How many recruiting asthma trials started since 2020?")
    )
    resolution = result.resolution
    assert isinstance(resolution, ResolvedQuery)
    assert resolution.interpreter == "rules"
    assert "spend cap reached" in resolution.notes[0].message
    assert ledger.spent_usd() == pytest.approx(19.95)
    assert json.dumps(registry.params[0]).count("RECRUITING") == 1
