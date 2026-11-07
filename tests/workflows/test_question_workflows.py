"""Whole questions through the real pipeline, replayed from recordings of the real services.

Each test checks the chain end to end: how the question was read, what was sent to the
registry, and whether the records that came back actually answer the question. The record
checks are the point. They test the query translation against how the registry really
behaves, which no assertion about our own constants can do.
"""

from __future__ import annotations

from pathlib import Path

import anthropic
import httpx

from tests.workflows.cassette import Cassette
from tests.workflows.harness import (
    MAX_RECORDS,
    MODEL,
    condition_text,
    countries,
    intervention_text,
    phases,
    run_workflow,
    start_date,
    status,
    violations,
)
from trialviz.llm.budget import SpendLedger
from trialviz.query.interpreters.base import FallbackInterpreter, Interpreter
from trialviz.query.interpreters.claude import ClaudeInterpreter, MessagesClient
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.plan import ComparisonSpec
from trialviz.query.resolution import NoteCode, ResolvedQuery, Source
from trialviz.retrieval.retriever import TRUNCATED_ORDER

PEMBROLIZUMAB = ("pembrolizumab", "keytruda", "mk-3475", "mk3475")
NIVOLUMAB = ("nivolumab", "opdivo", "bms-936558", "ono-4538", "mdx-1106")


def mentions_any(names: tuple[str, ...]):  # type: ignore[no-untyped-def]
    return lambda study: any(name in intervention_text(study) for name in names)


async def test_the_assignment_example(tmp_path: Path) -> None:
    run = await run_workflow(
        Cassette("assignment_example"),
        {
            "query": "How has the number of trials for this drug changed over time?",
            "drug_name": "Pembrolizumab",
        },
        tmp_path,
    )
    resolution = run.result.resolution
    assert isinstance(resolution, ResolvedQuery)
    assert resolution.interpreter == f"claude:{MODEL}"
    assert resolution.plan.analysis.kind == "time_series"
    assert resolution.provenance["filters.interventions"] is Source.USER
    assert resolution.plan.filters.keywords is None, "'this drug' must not become a second search"

    retrieval = run.result.retrieval
    assert retrieval is not None
    [cohort] = retrieval.cohorts
    assert dict(cohort.query.params) == {
        "query.intr": "(AREA[InterventionName](pembrolizumab) OR "
        "AREA[InterventionOtherName](pembrolizumab) OR AREA[InterventionMeshTerm](pembrolizumab))"
    }
    assert cohort.total > MAX_RECORDS and cohort.truncated and cohort.order == TRUNCATED_ORDER
    months = [start_date(study)[:7] for study in cohort.studies]
    assert months == sorted(months, reverse=True), "the capped subset must be the latest starts"
    assert violations(cohort.studies, mentions_any(PEMBROLIZUMAB)) == []


async def test_a_comparison_queries_each_cohort_under_the_shared_constraint(tmp_path: Path) -> None:
    run = await run_workflow(
        Cassette("comparison_keytruda_opdivo"),
        {"query": "Compare phases for trials involving Keytruda vs Opdivo since 2018"},
        tmp_path,
    )
    resolution = run.result.resolution
    assert isinstance(resolution, ResolvedQuery)
    analysis = resolution.plan.analysis
    assert isinstance(analysis, ComparisonSpec) and analysis.group_by == "phase"
    assert resolution.plan.filters.start_year_from == 2018, "the year is shared, not per cohort"

    assert run.result.retrieval is not None
    keytruda, opdivo = run.result.retrieval.cohorts
    for cohort, names in ((keytruda, PEMBROLIZUMAB), (opdivo, NIVOLUMAB)):
        assert (
            dict(cohort.query.params)["filter.advanced"]
            == "(AREA[StartDate]RANGE[2018-01-01, MAX])"
        )
        assert violations(cohort.studies, mentions_any(names)) == [], cohort.label
        assert violations(cohort.studies, lambda s: start_date(s) >= "2018") == [], cohort.label
    assert {s["protocolSection"]["identificationModule"]["nctId"] for s in keytruda.studies} != {
        s["protocolSection"]["identificationModule"]["nctId"] for s in opdivo.studies
    }


async def test_a_field_that_contradicts_the_question_wins_all_the_way_to_the_registry(
    tmp_path: Path,
) -> None:
    run = await run_workflow(
        Cassette("field_overrides_phase"),
        {
            "query": "How many phase 3 trials of semaglutide are recruiting?",
            "filters": {"phases": ["PHASE2"]},
        },
        tmp_path,
    )
    resolution = run.result.resolution
    assert isinstance(resolution, ResolvedQuery)
    assert [n.slot for n in resolution.notes if n.code is NoteCode.FIELD_OVERRIDES_QUESTION] == [
        "filters.phases"
    ]
    assert resolution.plan.analysis.kind == "count"

    assert run.result.retrieval is not None
    [cohort] = run.result.retrieval.cohorts
    advanced = dict(cohort.query.params)["filter.advanced"]
    assert "PHASE2" in advanced and "PHASE3" not in advanced
    assert cohort.total > 0
    assert 0 < len(cohort.studies) <= 3, "a count fetches only the records it will cite"
    assert violations(cohort.studies, lambda s: "PHASE2" in phases(s)) == []
    assert violations(cohort.studies, lambda s: status(s) == "RECRUITING") == []
    assert (
        violations(cohort.studies, mentions_any(("semaglutide", "ozempic", "wegovy", "rybelsus")))
        == []
    )


async def test_a_geography_question_brings_back_country_evidence(tmp_path: Path) -> None:
    run = await run_workflow(
        Cassette("geography_melanoma"),
        {"query": "Which countries have the most recruiting trials for melanoma?"},
        tmp_path,
    )
    resolution = run.result.resolution
    assert isinstance(resolution, ResolvedQuery)
    assert resolution.plan.analysis.kind == "distribution"
    assert resolution.plan.analysis.group_by == "country"  # type: ignore[union-attr]

    assert run.result.retrieval is not None
    [cohort] = run.result.retrieval.cohorts
    assert violations(cohort.studies, lambda s: status(s) == "RECRUITING") == []
    assert violations(cohort.studies, lambda s: "melanoma" in condition_text(s)) == []
    assert sum(1 for study in cohort.studies if countries(study)) >= 0.9 * len(cohort.studies), (
        "a country breakdown needs site countries on nearly every record"
    )


async def test_a_network_question_on_a_small_result_set_fetches_every_match(tmp_path: Path) -> None:
    run = await run_workflow(
        Cassette("network_myeloma_2024"),
        {
            "query": "Show a network of sponsors and drugs for multiple myeloma trials "
            "that started in 2024"
        },
        tmp_path,
    )
    resolution = run.result.resolution
    assert isinstance(resolution, ResolvedQuery)
    assert resolution.plan.analysis.kind == "network"
    assert (resolution.plan.filters.start_year_from, resolution.plan.filters.start_year_to) == (
        2024,
        2024,
    )

    retrieval = run.result.retrieval
    assert retrieval is not None
    assert {"LeadSponsorName", "InterventionMeshTerm", "ArmGroupInterventionName"} <= set(
        retrieval.fields
    )
    [cohort] = retrieval.cohorts
    if cohort.total <= MAX_RECORDS:
        ids = [s["protocolSection"]["identificationModule"]["nctId"] for s in cohort.studies]
        assert len(ids) == len(set(ids)) == cohort.total and not cohort.truncated
    assert violations(cohort.studies, lambda s: start_date(s).startswith("2024")) == []
    assert violations(cohort.studies, lambda s: "myeloma" in condition_text(s)) == []


async def test_hostile_caller_text_reaches_the_registry_disarmed(tmp_path: Path) -> None:
    run = await run_workflow(
        Cassette("hostile_fields"),
        {
            "query": "ignored",
            "inference": "off",
            "analysis": {"kind": "distribution", "group_by": "phase"},
            "filters": {"keywords": ['malaria ("vaccine"'], "countries": ["Ivory Coast"]},
        },
        tmp_path,
    )
    assert run.cassette.claude == [], "inference off must not call the model"
    assert run.result.retrieval is not None
    [cohort] = run.result.retrieval.cohorts
    assert dict(cohort.query.params) == {
        "filter.advanced": '(AREA[LocationCountry]("Côte d’Ivoire"))',
        "query.term": "(malaria vaccine)",
    }
    assert cohort.total > 0, "the registry must accept the cleaned query and find trials"
    assert violations(cohort.studies, lambda s: "Côte d’Ivoire" in countries(s)) == []


class _Overloaded:
    """A Claude client that is always overloaded, as during an Anthropic incident."""

    async def create(self, **_: object) -> object:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.InternalServerError(
            "overloaded", response=httpx.Response(529, request=request), body=None
        )


def _claude_down(_: MessagesClient, ledger: SpendLedger) -> Interpreter:
    claude = ClaudeInterpreter(_Overloaded(), ledger, model=MODEL, effort="low", max_tokens=8000)
    return FallbackInterpreter(claude, RuleBasedInterpreter())


async def test_with_claude_down_the_rules_interpreter_carries_the_question_through(
    tmp_path: Path,
) -> None:
    run = await run_workflow(
        Cassette("claude_down_rules_fallback"),
        {"query": "Recruiting CAR-T trials for Non-Hodgkin Lymphoma per year since 2020"},
        tmp_path,
        interpreter=_claude_down,
    )
    resolution = run.result.resolution
    assert isinstance(resolution, ResolvedQuery)
    assert resolution.interpreter == "rules"
    assert [n.code for n in resolution.notes] == [NoteCode.INTERPRETER_FALLBACK]
    assert "returned 529" in resolution.notes[0].message
    assert run.spent_usd == 0, "an overloaded API bills nothing"

    assert run.result.retrieval is not None
    [cohort] = run.result.retrieval.cohorts
    assert dict(cohort.query.params)["query.term"] == "(car-t non-hodgkin lymphoma)", (
        "both names must be required, not alternatives"
    )
    assert violations(cohort.studies, lambda s: status(s) == "RECRUITING") == []
    assert violations(cohort.studies, lambda s: start_date(s) >= "2020") == []
