"""Whole questions through the real pipeline, replayed from recordings of the real services.

Each test checks the chain end to end: how the question was read, what was sent to the
registry, and whether the records that came back actually answer the question. The record
checks are the point. They test the query translation against how the registry really
behaves, which no assertion about our own constants can do.
"""

from __future__ import annotations

from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path
from typing import Any

import anthropic
import httpx

from tests.workflows.cassette import Cassette
from tests.workflows.harness import (
    MAX_RECORDS,
    MODEL,
    answer_problems,
    condition_text,
    countries,
    default_interpreter,
    intervention_text,
    mention_text,
    nct_id,
    phases,
    run_workflow,
    start_date,
    status,
    violations,
)
from trialviz.api import create_app
from trialviz.llm.budget import SpendLedger
from trialviz.query.interpreters.base import FallbackInterpreter, Interpreter
from trialviz.query.interpreters.claude import ClaudeInterpreter, MessagesClient
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.plan import ComparisonSpec
from trialviz.query.resolution import NoteCode, ResolvedQuery, Source
from trialviz.retrieval.client import RateLimiter, RegistryClient
from trialviz.retrieval.retriever import TRUNCATED_ORDER, Retriever
from trialviz.vocabulary import ChartType

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

    # Trials that only mention the drug are fetched and reported, but not counted.
    mentions = cohort.mentions
    assert mentions is not None and mentions.total > 0 and not cohort.counts_mentions
    assert resolution.provenance["retrieval.name_matching"] is Source.DEFAULT
    assert not {nct_id(s) for s in mentions.studies} & {nct_id(s) for s in cohort.studies}
    assert violations(mentions.studies, lambda s: not mentions_any(PEMBROLIZUMAB)(s)) == [], (
        "a mention-only trial must not name the drug in a naming field"
    )
    cited = sum(any(n in mention_text(s) for n in PEMBROLIZUMAB) for s in mentions.studies)
    assert cited >= 0.9 * len(mentions.studies), "most mentions must be citable from fetched text"

    # The chart: one point per start year, counted exactly by the registry, since only a
    # capped subset of trials was fetched.
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert answer.visualization.type is ChartType.TIME_SERIES
    periods = [row["period"] for row in answer.visualization.data]  # type: ignore[index]
    assert periods == sorted(set(periods)), "one row per year, in order, none repeated"
    codes = {note.code for note in answer.meta.notes}
    assert (
        NoteCode.COUNTS_EXACT_FROM_REGISTRY in codes and NoteCode.COMPUTED_FROM_SUBSET not in codes
    )
    assert answer.meta.mentions and answer.meta.mentions[0].citations, "mentions are quoted"


async def test_counting_mentions_adds_them_without_double_counting(tmp_path: Path) -> None:
    run = await run_workflow(
        Cassette("mentions_counted"),
        {
            "query": "How are melanoma trials involving Keytruda distributed across phases?",
            "retrieval": {"name_matching": "mentioned"},
        },
        tmp_path,
    )
    resolution = run.result.resolution
    assert isinstance(resolution, ResolvedQuery)
    assert resolution.provenance["retrieval.name_matching"] is Source.USER
    assert run.result.retrieval is not None
    [cohort] = run.result.retrieval.cohorts
    mentions = cohort.mentions
    assert mentions is not None and mentions.total > 0 and cohort.counts_mentions
    assert cohort.total == cohort.named.total + mentions.total
    ids = [nct_id(s) for s in cohort.studies]
    assert len(ids) == len(set(ids)), "named trials and mentions must not overlap"
    assert len(cohort.studies) == len(cohort.named.studies) + len(mentions.studies)
    assert len(cohort.studies) <= MAX_RECORDS, "counted mentions share the cohort's record cap"

    # Phase gives every trial exactly one group, so exact counts must add up to the total,
    # named trials and mentions together.
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert sum(r["trial_count"] for r in answer.visualization.data) == cohort.total  # type: ignore[index]


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
    assert {nct_id(s) for s in keytruda.studies} != {nct_id(s) for s in opdivo.studies}

    # Two drugs per phase reads best as grouped bars: phases on the axis, a bar per drug.
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    view = answer.visualization
    assert view.type is ChartType.GROUPED_BAR_CHART
    assert (view.encoding["x"].field, view.encoding["color"].field) == ("phase", "cohort")
    assert {row["cohort"] for row in view.data} == {"Keytruda", "Opdivo"}  # type: ignore[union-attr,index]
    assert ChartType.NORMALIZED_STACKED_BAR_CHART in {a.type for a in answer.alternatives}


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

    # A count is one number, the registry's own total, cited by the field each trial matched.
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert answer.visualization.type is ChartType.STAT
    [datum] = answer.visualization.data  # type: ignore[misc]
    assert datum["trial_count"] == cohort.total
    assert all(
        c["field"].startswith("derivedSection.interventionBrowseModule") for c in datum["citations"]
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

    # A ranking of countries: sorted bars first, a world map as an alternative.
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert answer.visualization.type is ChartType.HORIZONTAL_BAR_CHART
    counts = [row["trial_count"] for row in answer.visualization.data]  # type: ignore[index]
    assert counts == sorted(counts, reverse=True)
    world = next(a for a in answer.alternatives if a.type is ChartType.CHOROPLETH_MAP)
    assert world.vega_lite is not None and world.options["id_matches"] == "iso_numeric"
    unmapped = [r["country"] for r in answer.visualization.data if r["iso_numeric"] is None]  # type: ignore[index]
    assert set(unmapped) <= {"Kosovo", "Not reported"}


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

    # Sponsors link only to drugs, never to each other, and the 3D view has real depth.
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert answer.visualization.type is ChartType.NETWORK_GRAPH
    graph = answer.visualization.data
    assert isinstance(graph, dict)
    kinds = {node["id"]: node["type"] for node in graph["nodes"]}
    assert all(
        {kinds[e["source"]], kinds[e["target"]]} == {"lead_sponsor", "intervention"}
        for e in graph["edges"]
    )
    three = next(a for a in answer.alternatives if a.type is ChartType.NETWORK_GRAPH_3D)
    assert three.data is None and three.encoding["z"].field == "z3", (
        "the 3D view reuses the same nodes"
    )
    depth = [node["z3"] for node in graph["nodes"]]
    assert all(-1 <= value <= 1 for value in depth) and max(depth) - min(depth) > 0.5


async def test_a_scatter_question_brings_back_both_measures_on_almost_every_record(
    tmp_path: Path,
) -> None:
    run = await run_workflow(
        Cassette("scatter_parkinsons"),
        {
            "query": "Is there a relationship between enrollment and duration "
            "in completed Parkinson's trials?"
        },
        tmp_path,
    )
    resolution = run.result.resolution
    assert isinstance(resolution, ResolvedQuery)
    analysis = resolution.plan.analysis
    assert analysis.kind == "scatter"
    assert {analysis.x, analysis.y} == {"enrollment", "duration_months"}  # type: ignore[union-attr]

    assert run.result.retrieval is not None
    [cohort] = run.result.retrieval.cohorts
    assert violations(cohort.studies, lambda s: status(s) == "COMPLETED") == []
    assert violations(cohort.studies, lambda s: "parkinson" in condition_text(s)) == []

    def plottable(study: Mapping[str, Any]) -> bool:
        design = study["protocolSection"].get("designModule", {})
        dates = study["protocolSection"].get("statusModule", {})
        has_end = "completionDateStruct" in dates or "primaryCompletionDateStruct" in dates
        return "count" in design.get("enrollmentInfo", {}) and bool(start_date(study)) and has_end

    plotted = sum(plottable(study) for study in cohort.studies)
    assert plotted >= 0.9 * len(cohort.studies), (
        "a scatter needs enrollment, a start date and an end date on nearly every record"
    )

    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert answer.visualization.type is ChartType.SCATTER_PLOT
    assert len(answer.visualization.data) == plotted  # type: ignore[arg-type]


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

    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    shown = [row["phase"] for row in answer.visualization.data]  # type: ignore[index]
    natural = [
        "Early Phase 1",
        "Phase 1",
        "Phase 1/2",
        "Phase 2",
        "Phase 2/3",
        "Phase 3",
        "Phase 4",
    ]
    natural += ["Not applicable", "Not reported"]
    assert shown == [p for p in natural if p in shown], "phases keep their natural order"


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

    assert run.result.answer is not None and answer_problems(run) == []


async def test_enrollment_by_phase_is_a_box_plot_with_consistent_statistics(tmp_path: Path) -> None:
    run = await run_workflow(
        Cassette("box_plot_asthma"),
        {"query": "Enrollment by phase for completed asthma trials"},
        tmp_path,
    )
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert answer.visualization.type is ChartType.BOX_PLOT
    boxes = answer.visualization.data
    assert isinstance(boxes, list)
    for box in boxes:
        order = [
            box[k] for k in ("min", "whisker_low", "q1", "median", "q3", "whisker_high", "max")
        ]
        assert order == sorted(order), box["phase"]
        assert box["n"] == len(box["nct_ids"])
    histogram = next(a for a in answer.alternatives if a.type is ChartType.HISTOGRAM)
    bins = histogram.data
    assert isinstance(bins, list), "a histogram has its own rows, bins rather than boxes"
    assert all(a["bin_end"] == b["bin_start"] for a, b in pairwise(bins))
    assert sum(b["trial_count"] for b in bins) == sum(box["n"] for box in boxes), (
        "phase is single-valued, so boxes and bins cover the same trials"
    )


async def test_a_flow_conserves_trials_at_every_stage(tmp_path: Path) -> None:
    run = await run_workflow(
        Cassette("flow_lung_cancer_2016"),
        {
            "query": "Show the flow of lung cancer trials started in 2016 from sponsor class "
            "to phase to overall status"
        },
        tmp_path,
    )
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert answer.visualization.type is ChartType.SANKEY
    flow = answer.visualization.data
    assert isinstance(flow, dict)
    stage = {node["id"]: node["stage"] for node in flow["nodes"]}
    assert all(stage[link["target"]] == stage[link["source"]] + 1 for link in flow["links"])
    for node in flow["nodes"]:
        inflow = sum(link["trial_count"] for link in flow["links"] if link["target"] == node["id"])
        outflow = sum(link["trial_count"] for link in flow["links"] if link["source"] == node["id"])
        if node["stage"] > 0:
            assert inflow == node["trial_count"], node["label"]
        if node["stage"] < 2:
            assert outflow == node["trial_count"], node["label"]
    assert run.result.retrieval is not None
    first_stage = sum(n["trial_count"] for n in flow["nodes"] if n["stage"] == 0)
    assert first_stage == len(run.result.retrieval.cohorts[0].studies)


async def test_a_timeline_lists_each_matching_trial_in_start_order(tmp_path: Path) -> None:
    run = await run_workflow(
        Cassette("timeline_keytruda_melanoma"),
        {"query": "Timeline of phase 3 Keytruda melanoma trials"},
        tmp_path,
    )
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert answer.visualization.type is ChartType.TIMELINE and answer.visualization.vega_lite
    rows = answer.visualization.data
    assert isinstance(rows, list) and 0 < len(rows) <= 60
    starts = [row["start"][:7] for row in rows if row["start"]]
    assert starts == sorted(starts)
    assert all("3" in row["phase"] for row in rows), "every trial on the timeline is phase 3"


async def test_exact_counts_for_a_capped_result_set_add_up_to_the_registry_total(
    tmp_path: Path,
) -> None:
    run = await run_workflow(
        Cassette("exact_counts_breast_cancer"),
        {"query": "How are breast cancer trials distributed by overall status?"},
        tmp_path,
    )
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert run.result.retrieval is not None
    [cohort] = run.result.retrieval.cohorts
    assert cohort.truncated and cohort.total > 10 * MAX_RECORDS
    codes = {note.code for note in answer.meta.notes}
    assert (
        NoteCode.COUNTS_EXACT_FROM_REGISTRY in codes and NoteCode.COMPUTED_FROM_SUBSET not in codes
    )
    # Every trial has exactly one status (or none, counted as not reported), so the
    # registry's per-status counts must sum to its total, rare statuses included.
    assert sum(r["trial_count"] for r in answer.visualization.data) == cohort.total  # type: ignore[index]


async def test_drug_combinations_come_only_from_drugs_given_in_the_same_arm(tmp_path: Path) -> None:
    run = await run_workflow(
        Cassette("combinations_myeloma"),
        {
            "query": "Which drugs are given together in the same arm in multiple myeloma "
            "trials started since 2022?"
        },
        tmp_path,
    )
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    graph = answer.visualization.data
    assert isinstance(graph, dict) and graph["edges"]
    labels = {node["id"]: node["label"].casefold() for node in graph["nodes"]}
    named_in_arm = 0
    for edge in graph["edges"]:
        assert all(".armGroups[" in c["field"] for c in edge["citations"]), (
            "edges cite treatment arms"
        )
        arm = edge["citations"][0]["excerpt"].casefold()
        named_in_arm += labels[edge["source"]] in arm and labels[edge["target"]] in arm
    assert named_in_arm >= 0.8 * len(graph["edges"]), "the cited arm names both drugs"


async def test_three_measures_give_a_3d_scatter_with_a_flat_bubble_alternative(
    tmp_path: Path,
) -> None:
    run = await run_workflow(
        Cassette("scatter_3d_diabetes"),
        {
            "query": "Plot enrollment, duration and number of sites for completed phase 3 "
            "diabetes trials in 3D"
        },
        tmp_path,
    )
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert answer.visualization.type is ChartType.SCATTER_PLOT_3D
    assert {"x", "y", "z"} <= set(answer.visualization.encoding)
    bubble = next(a for a in answer.alternatives if a.type is ChartType.BUBBLE_CHART)
    assert "size" in bubble.encoding and bubble.data is None


async def test_the_http_endpoint_serves_exactly_the_pipelines_answer(tmp_path: Path) -> None:
    request = {
        "query": "How has the number of trials for this drug changed over time?",
        "drug_name": "Pembrolizumab",
    }
    direct = await run_workflow(Cassette("assignment_example"), request, tmp_path)
    assert direct.result.answer is not None

    cassette = Cassette("assignment_example")
    ledger = SpendLedger(tmp_path / "http-spend.json", 20.0)
    client = RegistryClient(
        httpx.AsyncClient(transport=cassette.registry_transport()), RateLimiter(1e9)
    )
    app = create_app(
        interpreter=default_interpreter(cassette.claude_messages(), ledger),
        retriever=Retriever(client, max_records=MAX_RECORDS),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.post("/v1/query", json=request)
    assert response.status_code == 200
    assert response.json() == direct.result.answer.model_dump(mode="json")
