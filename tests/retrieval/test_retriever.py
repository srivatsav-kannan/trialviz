"""The retrieval rules: how many requests a plan becomes and what each one fetches."""

from __future__ import annotations

from typing import Any

from tests.retrieval.fake_registry import FakeRegistry
from tests.retrieval.test_client import FakeTime, client_for
from trialviz.query.plan import Plan
from trialviz.retrieval.retriever import TRUNCATED_ORDER, Retriever


def plan(
    analysis: dict[str, Any], presentation: dict[str, Any] | None = None, **filters: Any
) -> Plan:
    return Plan.model_validate(
        {
            "filters": filters,
            "analysis": analysis,
            "presentation": {"citations_per_datum": 3, **(presentation or {})},
        }
    )


def retriever(registry: FakeRegistry, max_records: int = 10_000) -> Retriever:
    return Retriever(client_for(registry, FakeTime()), max_records=max_records)


COMPARISON = {
    "kind": "comparison",
    "group_by": "phase",
    "cohorts": [
        {"label": "Keytruda", "filters": {"interventions": ["Keytruda"]}},
        {"label": "Opdivo", "filters": {"interventions": ["Opdivo"]}},
    ],
}


async def test_each_cohort_is_counted_and_fetched_with_the_shared_filters() -> None:
    registry = FakeRegistry(40, totals={"(keytruda)": 30, "(opdivo)": 12})
    result = await retriever(registry).retrieve(plan(COMPARISON, start_year_from=2018))
    keytruda, opdivo = result.cohorts
    assert (keytruda.label, keytruda.total, len(keytruda.studies)) == ("Keytruda", 30, 30)
    assert (opdivo.label, opdivo.total, len(opdivo.studies)) == ("Opdivo", 12, 12)
    for params in registry.study_requests():
        assert params["filter.advanced"] == "(AREA[StartDate]RANGE[2018-01-01, MAX])"
    assert not keytruda.truncated and keytruda.order is None


async def test_a_count_fetches_only_enough_records_to_cite() -> None:
    registry = FakeRegistry(500)
    result = await retriever(registry).retrieve(plan({"kind": "count"}))
    [cohort] = result.cohorts
    assert (cohort.total, len(cohort.studies), result.record_limit) == (500, 3, 3)
    assert registry.study_requests()[-1]["pageSize"] == "3"


async def test_a_count_with_no_citations_fetches_nothing_after_counting() -> None:
    registry = FakeRegistry(500)
    result = await retriever(registry).retrieve(
        plan({"kind": "count"}, presentation={"citations_per_datum": 0})
    )
    assert result.cohorts[0].studies == ()
    assert len(registry.study_requests()) == 1


async def test_no_matches_means_no_fetch() -> None:
    registry = FakeRegistry(0)
    result = await retriever(registry).retrieve(plan({"kind": "distribution", "group_by": "phase"}))
    assert result.cohorts[0].total == 0
    assert len(registry.study_requests()) == 1


async def test_above_the_cap_a_fixed_order_subset_is_fetched_and_flagged() -> None:
    registry = FakeRegistry(120)
    result = await retriever(registry, max_records=50).retrieve(
        plan({"kind": "distribution", "group_by": "phase"})
    )
    [cohort] = result.cohorts
    assert (cohort.total, len(cohort.studies), cohort.truncated) == (120, 50, True)
    assert cohort.order == TRUNCATED_ORDER
    assert registry.study_requests()[-1]["sort"] == TRUNCATED_ORDER


async def test_below_the_cap_no_sort_is_requested() -> None:
    registry = FakeRegistry(30)
    await retriever(registry).retrieve(plan({"kind": "distribution", "group_by": "phase"}))
    assert all("sort" not in params for params in registry.study_requests())


async def test_the_result_records_how_every_number_will_be_traceable() -> None:
    registry = FakeRegistry(30)
    result = await retriever(registry).retrieve(
        plan({"kind": "distribution", "group_by": "country"})
    )
    [cohort] = result.cohorts
    assert result.data_version == "2026-11-06T10:00:05"
    assert "LocationCountry" in result.fields
    assert cohort.requests[0].endswith("countTotal=true&fields=NCTId&pageSize=1")
    assert len(cohort.requests) == 2
