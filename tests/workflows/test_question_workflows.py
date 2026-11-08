"""Whole questions through the real pipeline, replayed from recordings of the real services.

Each test checks the chain end to end: how the question was read, what was sent to the
registry, and whether the records that came back actually answer the question. The record
checks are the point. They test the query translation against how the registry really
behaves, which no assertion about our own constants can do.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from itertools import pairwise
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest

from tests.workflows.cassette import RECORDING, Cassette
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
from trialviz.llm.budget import FileLedger
from trialviz.query.interpreters.base import FallbackInterpreter, Interpreter
from trialviz.query.interpreters.claude import ClaudeInterpreter, MessagesClient
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.plan import ComparisonSpec
from trialviz.query.resolution import NoteCode, ResolvedQuery, Source
from trialviz.retrieval.client import RateLimiter, RegistryClient
from trialviz.retrieval.retriever import TRUNCATED_ORDER, Retriever
from trialviz.store import AnswerStore, FileStore, NoStore
from trialviz.vocabulary import ChartType, Dimension

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
    # Each cited trial is cited for every active filter: where it names the drug, its phase
    # and its status. A drug citation must actually name the drug, never another one.
    by_trial: dict[str, list[dict[str, str]]] = {}
    for citation in datum["citations"]:
        by_trial.setdefault(citation["nct_id"], []).append(citation)
    assert by_trial
    names = ("semaglutide", "ozempic", "wegovy", "rybelsus", "nn9535")
    for citations in by_trial.values():
        fields = [c["field"] for c in citations]
        assert any(
            "intervention" in c["field"].casefold()
            and any(n in c["excerpt"].casefold() for n in names)
            for c in citations
        ), citations
        assert "protocolSection.designModule.phases" in fields
        assert "protocolSection.statusModule.overallStatus" in fields


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
    natural += ["Not applicable", "No phase (observational)", "Not reported"]
    assert shown == [p for p in natural if p in shown], "phases keep their natural order"


class _Overloaded:
    """A Claude client that is always overloaded, as during an Anthropic incident."""

    async def create(self, **_: object) -> object:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.InternalServerError(
            "overloaded", response=httpx.Response(529, request=request), body=None
        )


def _claude_down(_: MessagesClient, ledger: FileLedger) -> Interpreter:
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

    # "Lung cancer" must include trials filed under MeSH terms below Lung Neoplasms, such as
    # "Carcinoma, Non-Small-Cell Lung", which the registry cannot search by hierarchy. An
    # independent audit found 57 such trials wrongly reported as mere mentions.
    [cohort] = run.result.retrieval.cohorts
    assert (
        cohort.descendants and "Carcinoma, Non-Small-Cell Lung" in cohort.descendants["lung cancer"]
    )
    terms = set(cohort.descendants["lung cancer"])

    def meshes(study: Mapping[str, Any]) -> set[str]:
        browse = study.get("derivedSection", {}).get("conditionBrowseModule", {})
        return {m["term"] for m in browse.get("meshes", [])}

    # Some trials are in only because of their MeSH term, not their own wording.
    by_mesh_only = [
        nct_id(study)
        for study in cohort.studies
        if "lung cancer" not in condition_text(study) and meshes(study) & terms
    ]
    assert by_mesh_only, "the MeSH descendants must bring in trials the wording misses"
    included = {nct_id(study) for study in cohort.studies}
    assert cohort.mentions is not None
    assert not included & {nct_id(study) for study in cohort.mentions.studies}
    # And no mention-only trial is filed under a single one of those MeSH terms.
    assert [
        nct_id(s) for s in cohort.mentions.studies if len(meshes(s)) == 1 and meshes(s) & terms
    ] == []
    assert any(n.code is NoteCode.CONDITION_INCLUDES_MESH_TERMS for n in answer.meta.notes)
    # Observational studies have no phase by design and are labeled so, not "Not reported".
    phase_labels = {n["label"] for n in flow["nodes"] if n["stage"] == 1}
    assert "No phase (observational)" in phase_labels


async def test_two_named_conditions_are_matched_as_phrases_not_loose_words(
    tmp_path: Path,
) -> None:
    # An audit found "type 1 diabetes" matching a trial whose condition was "Type 2
    # Diabetes Mellitus" and whose keyword was "phase 1", because the words were matched
    # separately. Each cohort must now hold only trials that name its own type.
    run = await run_workflow(
        Cassette("diabetes_types_2024"),
        {"query": "Compare type 1 and type 2 diabetes trials that started in 2024 by phase"},
        tmp_path,
    )
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert run.result.retrieval is not None
    first, second = run.result.retrieval.cohorts
    assert first.total > 0 and second.total > 0

    type_one = re.compile(r"type (1|i)\b|\bt1d|(?<!non)(?<!non-)insulin-dependent|juvenile|iddm")
    type_two = re.compile(r"type (2|ii)\b|\bt2d|non-?insulin|niddm")

    def names(kind: re.Pattern[str]) -> Callable[[Mapping[str, Any]], bool]:
        return lambda study: bool(kind.search(condition_text(study)))

    for cohort in (first, second):
        own, other = (type_one, type_two) if "1" in (cohort.label or "") else (type_two, type_one)
        # A trial may study both types. It may not study only the other one.
        wrong = [nct_id(s) for s in cohort.studies if names(other)(s) and not names(own)(s)]
        assert wrong == [], (cohort.label, wrong)
        if cohort.mentions is not None:
            assert len(cohort.mentions.studies) <= 30, "reported mentions are only quoted"


async def test_cohort_totals_come_from_counts_without_fetching_the_cohorts(
    tmp_path: Path,
) -> None:
    # A comparison of totals needs only the registry's counts. A review found it fetching
    # 40,000 records for two numbers, then calling the exact totals a subset.
    run = await run_workflow(
        Cassette("totals_keytruda_opdivo_2024"),
        {"query": "How many Keytruda trials versus Opdivo trials started in 2024?"},
        tmp_path,
    )
    resolution = run.result.resolution
    assert isinstance(resolution, ResolvedQuery)
    assert resolution.plan.analysis.kind == "comparison"
    assert resolution.plan.analysis.group_by is None  # type: ignore[union-attr]
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    assert run.result.retrieval is not None
    for cohort in run.result.retrieval.cohorts:
        assert len(cohort.studies) <= 3 and not cohort.truncated
    bars = {row["cohort"]: row["trial_count"] for row in answer.visualization.data}  # type: ignore[index]
    assert bars == {c.label: c.total for c in run.result.retrieval.cohorts}
    assert not any(n.code is NoteCode.COMPUTED_FROM_SUBSET for n in answer.meta.notes)
    assert len(answer.meta.registry_requests) == len(run.cassette.sent) - 1, (
        "every registry request except the version check is listed in the answer"
    )


async def test_a_network_can_link_sponsors_to_their_investigators(tmp_path: Path) -> None:
    # The assignment names investigators and sites among the entities a network can show.
    run = await run_workflow(
        Cassette("network_investigators_myeloma"),
        {
            "query": "Show a network of lead sponsors and principal investigators for "
            "multiple myeloma trials that started in 2024"
        },
        tmp_path,
    )
    resolution = run.result.resolution
    assert isinstance(resolution, ResolvedQuery)
    assert set(resolution.plan.analysis.node_types) == {  # type: ignore[union-attr]
        Dimension.LEAD_SPONSOR,
        Dimension.INVESTIGATOR,
    }
    answer = run.result.answer
    assert answer is not None and answer_problems(run) == []
    graph = answer.visualization.data
    assert isinstance(graph, dict) and graph["edges"]
    kinds = {node["id"]: node["type"] for node in graph["nodes"]}
    assert {kinds[e["source"]] for e in graph["edges"]} | {
        kinds[e["target"]] for e in graph["edges"]
    } == {"lead_sponsor", "investigator"}
    # Each link is cited by the investigator's entry and the sponsor of the same trial.
    for edge in graph["edges"]:
        fields = {c["field"].split("[")[0].rsplit(".", 1)[-1] for c in edge["citations"]}
        assert "overallOfficials" in " ".join(c["field"] for c in edge["citations"]), edge
        assert any("leadSponsor" in c["field"] for c in edge["citations"]), fields


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


async def test_the_http_endpoints_serve_save_export_and_stream_the_pipelines_answer(
    tmp_path: Path,
) -> None:
    if RECORDING:
        pytest.skip("replays the cassette the assignment workflow records")
    request = {
        "query": "How has the number of trials for this drug changed over time?",
        "drug_name": "Pembrolizumab",
    }
    direct = await run_workflow(Cassette("assignment_example"), request, tmp_path)
    assert direct.result.answer is not None
    expected = direct.result.answer.model_dump(mode="json")

    def app(store: AnswerStore) -> httpx.AsyncClient:
        # Each app replays the recording afresh. Replay is strict, so a request the
        # recording never saw fails the test.
        cassette = Cassette("assignment_example")
        ledger = FileLedger(tmp_path / "http-spend.json", 20.0)
        client = RegistryClient(
            httpx.AsyncClient(transport=cassette.registry_transport()), RateLimiter(1e9)
        )
        service = create_app(
            interpreter=default_interpreter(cassette.claude_messages(), ledger),
            retriever=Retriever(client, max_records=MAX_RECORDS),
            store=store,
        )
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=service), base_url="http://t")

    async with app(FileStore(tmp_path / "answers", keep=10)) as http:
        response = await http.post("/v1/query", json=request)
        assert response.status_code == 200
        answer = response.json()
        key = answer["meta"]["answer_id"]
        assert answer == expected | {"meta": expected["meta"] | {"answer_id": key}}

        # The same request against the same data is the saved answer, at no cost: nothing
        # more is spent on Claude, and the strict replay would fail on any new registry call.
        spent = FileLedger(tmp_path / "http-spend.json", 20.0).spent_usd()
        assert spent > 0
        again = await http.post("/v1/query", json=request)
        assert FileLedger(tmp_path / "http-spend.json", 20.0).spent_usd() == spent
        assert again.json()["meta"]["answer_id"] == key
        reopened = await http.get(f"/v1/answers/{key}")
        assert reopened.status_code == 200 and reopened.json() == again.json()
        csv = await http.get(f"/v1/answers/{key}/csv")
        header, *lines = csv.text.splitlines()
        assert csv.headers["content-type"].startswith("text/csv")
        assert header.split(",")[:3] == ["id", "period", "trial_count"]
        assert len(lines) == len(answer["visualization"]["data"])
        missing = await http.get("/v1/answers/0123456789abcdef0123")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "answer_not_found"

    # Streamed, the same answer arrives after progress through every stage.
    async with app(NoStore()) as http:
        streamed = await http.post("/v1/query/stream", json=request)
    events = [
        (block.split("\n")[0].removeprefix("event: "), json.loads(block.split("\n")[1][6:]))
        for block in streamed.text.strip().split("\n\n")
        if block.startswith("event:")
    ]
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == "answer" and set(kinds[:-1]) == {"progress"}
    stages = list(dict.fromkeys(body["stage"] for kind, body in events if kind == "progress"))
    assert stages == ["reading", "retrieving", "aggregating", "counting", "presenting"]
    last = events[-2][1]
    assert last["records_fetched"] == last["records_expected"] > 0
    assert events[-1][1] == answer
