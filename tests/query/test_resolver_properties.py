"""Properties that must hold for every combination of caller fields and proposals.

Hand-written cases cover the combinations someone thought of. These generate thousands of
random pairings of caller fields with interpreter proposals, drawn from pools that mix
valid values, near misses and garbage, and check the guarantees the resolver makes:

1. It always returns a plan or a clarification. It never raises.
2. Every slot the caller set appears in the plan unchanged, marked `user`.
3. The plan's slots and the provenance keys are the same set.
4. An inferred value only reaches the plan if the proposal contained it.
5. Whenever the proposal contradicts a caller field, a note says so.
6. The same input always produces the same output.
"""

from __future__ import annotations

import asyncio
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.support import TODAY, ScriptedInterpreter, plan_slots
from trialviz.query.consistency import same_meaning
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import ClarificationNeeded, NoteCode, ResolvedQuery, Source
from trialviz.query.resolver import QueryResolver, explicit_slots

# Each pool mixes canonical values, loose spellings the parsers accept, and values they
# must reject. The caller pools hold only valid values, because invalid caller input is a
# 422 at the boundary and never reaches the resolver.
CALLER_FILTERS = {
    "phases": [["PHASE2"], ["Phase III", "1"], ["1/2"]],
    "statuses": [["RECRUITING"], ["completed", "terminated"]],
    "interventions": [["Pembrolizumab"], ["nivolumab", "ipilimumab"]],
    "conditions": [["Melanoma"]],
    "countries": [["United States"], ["japan", "USA"]],
    "start_year_from": [2010, 2018],
    "start_year_to": [2019, 2024],
}
PROPOSED_FILTERS = {
    "phases": [["PHASE3"], ["phase 2"], ["PHASE5"], "PHASE3", ["1/2/3"]],
    "statuses": [["RECRUITING"], ["active"], ["Completed"]],
    "interventions": [["pembrolizumab"], ["Keytruda", "lenvatinib"], [""], "Opdivo"],
    "conditions": [["melanoma"], ["NSCLC"]],
    "countries": [["Korea"], ["Atlantis"], ["United States"], ["Germany"]],
    "keywords": [["PD-1"]],
    "start_year_from": [2015, 2025, "2015", 1800, True],
    "start_year_to": [2012, 2020, 2101],
    "made_up_field": [["x"]],
}
CALLER_ANALYSES: list[dict[str, Any]] = [
    {},
    {"kind": "count"},
    {"group_by": "phase"},
    {"kind": "time_series", "granularity": "month"},
    {"series_by": "status"},
    {"measure": "enrollment"},
    {"node_types": ["lead_sponsor", "intervention"]},
]
PROPOSED_ANALYSES: list[dict[str, Any]] = [
    {},
    {"kind": "count"},
    {"kind": "distribution", "group_by": "country"},
    {"kind": "distribution", "group_by": "status", "series_by": "status"},
    {"kind": "time_series", "series_by": "phase"},
    {"kind": "histogram"},
    {"kind": "scatter", "x": "enrollment", "y": "enrollment"},
    {"kind": "network", "node_types": ["lead_sponsor", "lead_sponsor"]},
    {"kind": "comparison", "cohorts": [{"label": "A", "filters": {"interventions": ["A"]}}]},
    {
        "kind": "comparison",
        "cohorts": [
            {"label": "A", "filters": {"interventions": ["A"]}},
            {"label": "B", "filters": {"conditions": ["B"]}},
        ],
    },
    {"kind": "sorcery"},
    {"granularity": "decade"},
]


def subset_of(pool: dict[str, list[Any]]) -> st.SearchStrategy[dict[str, Any]]:
    return st.dictionaries(st.sampled_from(sorted(pool)), st.none(), max_size=4).flatmap(
        lambda keys: st.fixed_dictionaries({key: st.sampled_from(pool[key]) for key in keys})
    )


@st.composite
def caller_requests(draw: st.DrawFn) -> dict[str, Any]:
    request = {
        "query": "generated",
        "filters": draw(subset_of(CALLER_FILTERS)),
        "analysis": draw(st.sampled_from(CALLER_ANALYSES)),
    }
    try:
        QueryRequest.model_validate(request)
    except ValidationError:
        request["filters"].pop("start_year_to", None)  # the only invalid caller pairing
    return request


@st.composite
def proposals(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "filters": draw(subset_of(PROPOSED_FILTERS)),
        "analysis": draw(st.sampled_from(PROPOSED_ANALYSES)),
        "presentation": draw(st.sampled_from([{}, {"top_n": 10}, {"top_n": 0}, {"sort": "x"}])),
    }


def run(request: QueryRequest, proposal: dict[str, Any]) -> ResolvedQuery | ClarificationNeeded:
    resolver = QueryResolver(ScriptedInterpreter(proposal), today=lambda: TODAY)
    return asyncio.run(resolver.resolve(request))


@settings(max_examples=1500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(caller_requests(), proposals())
def test_resolution_guarantees_hold_for_every_combination(
    raw_request: dict[str, Any], proposal: dict[str, Any]
) -> None:
    request = QueryRequest.model_validate(raw_request)
    outcome = run(request, proposal)  # guarantee 1: no exception escapes

    assert outcome.model_dump_json() == run(request, proposal).model_dump_json()  # 6

    if isinstance(outcome, ClarificationNeeded):
        assert outcome.message
        return

    slots = plan_slots(outcome)
    assert set(slots) == set(outcome.provenance)  # 3

    caller = explicit_slots(request)
    for path, value in caller.items():  # 2
        assert outcome.provenance[path] is Source.USER
        assert same_meaning(
            getattr(getattr(outcome.plan, path.split(".")[0]), path.split(".")[1]), value
        )

    for path, source in outcome.provenance.items():  # 4
        if source is Source.INFERRED:
            section, name = path.split(".", 1)
            assert proposal.get(section, {}).get(name) is not None

    overridden = {
        note.slot for note in outcome.notes if note.code is NoteCode.FIELD_OVERRIDES_QUESTION
    }
    for path, value in caller.items():  # 5
        section, name = path.split(".", 1)
        proposed = proposal.get(section, {}).get(name)
        if proposed is None:
            continue
        model = type(getattr(request, section))
        try:
            parsed = getattr(model.model_validate({name: proposed}), name)
        except ValidationError:
            continue  # an invalid proposal is rejected, not a contradiction
        if not same_meaning(parsed, value):
            assert path in overridden
