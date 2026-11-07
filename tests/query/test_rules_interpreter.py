"""The rule-based interpreter on phrasings that trip up naive pattern matching.

Each case targets a specific trap: an exclusive bound ("after 2015"), a number that is not
a phase ("type 2 diabetes"), a status hidden inside another ("not yet recruiting"), a place
that contains a country name ("New Jersey"), a time cue that should beat a count cue. The
interpreter's known blind spot, typing entities, is pinned down too: names it cannot place
must become keywords, never a guessed drug or condition.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.support import TODAY, clarification, resolved
from trialviz.query.interpreters.base import IssueKind
from trialviz.query.interpreters.rules import RuleBasedInterpreter, interpret_question
from trialviz.query.request import QueryRequest
from trialviz.query.resolver import QueryResolver


def read(query: str, **fields: Any) -> dict[str, dict[str, Any]]:
    interpretation = interpret_question(QueryRequest(query=query, **fields), TODAY)
    assert interpretation.issue is None, interpretation.issue
    return {
        section: {k: getattr(v, "value", v) for k, v in values.items()}
        for section, values in interpretation.proposal.items()  # type: ignore[union-attr]
    }


def filters(query: str, **fields: Any) -> dict[str, Any]:
    return read(query, **fields)["filters"]


class TestYears:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("trials since 2015", {"start_year_from": 2015}),
            ("trials after 2015", {"start_year_from": 2016}),
            ("trials before 2020", {"start_year_to": 2019}),
            ("trials until 2020", {"start_year_to": 2020}),
            ("trials in 2020", {"start_year_from": 2020, "start_year_to": 2020}),
            ("between 2020 and 2015", {"start_year_from": 2015, "start_year_to": 2020}),
            ("from 2010 to 2015", {"start_year_from": 2010, "start_year_to": 2015}),
            ("2010–2015 trials", {"start_year_from": 2010, "start_year_to": 2015}),
            ("in the last 5 years", {"start_year_from": 2022, "start_year_to": 2026}),
            ("over the past two years", {"start_year_from": 2025, "start_year_to": 2026}),
            ("in the past decade", {"start_year_from": 2017, "start_year_to": 2026}),
        ],
    )
    def test_year_expressions(self, query: str, expected: dict[str, int]) -> None:
        found = filters(query)
        assert {k: found.get(k) for k in expected} == expected
        assert set(found) & {"start_year_from", "start_year_to"} == set(expected)

    def test_a_four_digit_count_is_not_a_year(self) -> None:
        assert "start_year_from" not in filters("trials with 1000 participants in 5000 patients")


class TestPhasesAndStatuses:
    @pytest.mark.parametrize(
        ("query", "phases"),
        [
            ("Phase II/III trials", ["PHASE2", "PHASE3"]),
            ("phases 2 and 3", ["PHASE2", "PHASE3"]),
            ("early phase 1 studies", ["EARLY_PHASE1"]),
            ("phase 1 trials started in 2020", ["PHASE1"]),
        ],
    )
    def test_phase_expressions(self, query: str, phases: list[str]) -> None:
        assert filters(query)["phases"] == phases

    def test_a_number_inside_a_disease_name_is_not_a_phase(self) -> None:
        found = filters("phase 3 trials for type 2 diabetes")
        assert found["phases"] == ["PHASE3"]
        assert found["keywords"] == ["type 2 diabetes"]

    def test_not_yet_recruiting_is_not_also_recruiting(self) -> None:
        assert filters("not yet recruiting melanoma trials")["statuses"] == ["NOT_YET_RECRUITING"]

    def test_a_negated_status_produces_no_status(self) -> None:
        assert "statuses" not in filters("melanoma trials that are no longer recruiting")

    def test_completed_per_year_is_a_completion_time_series_not_a_status(self) -> None:
        reading = read("How many asthma trials completed per year?")
        assert "statuses" not in reading["filters"]
        assert reading["analysis"]["kind"] == "time_series"
        assert reading["analysis"]["time_field"] == "completion_date"


class TestPlaces:
    def test_a_us_state_is_not_the_country_it_contains(self) -> None:
        found = filters("recruiting trials in New Jersey and South Korea")
        assert found["countries"] == ["South Korea"]

    def test_country_names_do_not_leak_into_keywords(self) -> None:
        found = filters("lung cancer trials in the United Kingdom")
        assert found["countries"] == ["United Kingdom"]
        assert found["keywords"] == ["lung cancer"]


class TestChoosingTheAnalysis:
    @pytest.mark.parametrize(
        ("query", "analysis"),
        [
            (
                "How many trials started each year for type 2 diabetes?",
                {"kind": "time_series"},
            ),
            (
                "How has the number of trials for pembrolizumab changed since 2015?",
                {"kind": "time_series"},
            ),
            (
                "monthly trend of COVID-19 vaccine trials",
                {"kind": "time_series", "granularity": "month"},
            ),
            (
                "How are breast cancer trials distributed across phases?",
                {"kind": "distribution", "group_by": "phase"},
            ),
            (
                "top 10 sponsors of lung cancer trials",
                {"kind": "distribution", "group_by": "lead_sponsor"},
            ),
            ("melanoma trials per year by phase", {"kind": "time_series", "series_by": "phase"}),
            (
                "distribution of enrollment in asthma trials",
                {"kind": "histogram", "measure": "enrollment"},
            ),
            (
                "enrollment vs duration for completed trials",
                {"kind": "scatter", "x": "enrollment", "y": "duration_months"},
            ),
            ("How many glioblastoma trials are recruiting?", {"kind": "count"}),
        ],
    )
    def test_cue_priority(self, query: str, analysis: dict[str, Any]) -> None:
        found = read(query)["analysis"]
        assert {k: found.get(k) for k in analysis} == analysis

    def test_top_n_and_ranking_are_presentation(self) -> None:
        reading = read("top 10 sponsors of lung cancer trials")
        assert reading["presentation"] == {"top_n": 10, "sort": "value_desc"}

    def test_sponsor_class_is_read_before_sponsor(self) -> None:
        assert read("trials by sponsor class")["analysis"]["group_by"] == "sponsor_class"

    @pytest.mark.parametrize(
        ("query", "node_types"),
        [
            (
                "Show a network of sponsors ↔ drugs for myeloma trials",
                ["lead_sponsor", "intervention"],
            ),
            (
                "Which drugs frequently co-occur in combination studies?",
                ["intervention", "intervention"],
            ),
            (
                "network of conditions and drugs in rare disease trials",
                ["condition", "intervention"],
            ),
        ],
    )
    def test_networks(self, query: str, node_types: list[str]) -> None:
        found = read(query)["analysis"]
        assert found["kind"] == "network"
        assert [getattr(n, "value", n) for n in found["node_types"]] == node_types


class TestComparisons:
    def test_names_on_either_side_of_vs_become_cohorts(self) -> None:
        analysis = read("Compare phases for trials involving pembrolizumab vs nivolumab.")[
            "analysis"
        ]
        assert analysis["kind"] == "comparison"
        assert analysis["group_by"] == "phase"
        assert [c["label"] for c in analysis["cohorts"]] == ["pembrolizumab", "nivolumab"]

    def test_multiword_names_joined_by_and(self) -> None:
        analysis = read("Compare breast cancer and lung cancer trials by phase")["analysis"]
        assert [c["label"] for c in analysis["cohorts"]] == ["breast cancer", "lung cancer"]

    def test_comparison_per_year_groups_by_start_year(self) -> None:
        analysis = read("compare melanoma vs glioma trials per year")["analysis"]
        assert analysis["group_by"] == "start_year"
        assert "granularity" not in analysis

    async def test_a_comparison_with_no_named_groups_asks_for_them(self) -> None:
        resolver = QueryResolver(RuleBasedInterpreter(), today=lambda: TODAY)
        outcome = clarification(
            await resolver.resolve(
                QueryRequest(query="Compare sponsor categories across two conditions.")
            )
        )
        assert outcome.missing == ["analysis.cohorts"]


class TestNamesAndReferences:
    def test_names_keep_their_original_spelling(self) -> None:
        assert filters("Recruiting CAR-T trials for Non-Hodgkin Lymphoma")["keywords"] == [
            "CAR-T",
            "Non-Hodgkin Lymphoma",
        ]

    def test_disease_words_stay_part_of_the_name(self) -> None:
        assert filters(
            "What are the most common intervention types for Alzheimer's disease trials?"
        )["keywords"] == ["Alzheimer's disease"]

    def test_a_name_the_caller_already_supplied_is_not_repeated_as_a_keyword(self) -> None:
        assert "keywords" not in filters(
            "How many trials for Pembrolizumab per year?", drug_name="pembrolizumab"
        )

    def test_this_drug_without_a_drug_is_flagged(self) -> None:
        interpretation = interpret_question(
            QueryRequest(query="How many trials for this drug?"), TODAY
        )
        assert interpretation.issue is not None
        assert interpretation.issue.kind is IssueKind.AMBIGUOUS_REFERENCE
        assert "drug_name" in interpretation.issue.message

    async def test_this_drug_with_a_drug_resolves_end_to_end(self) -> None:
        resolver = QueryResolver(RuleBasedInterpreter(), today=lambda: TODAY)
        outcome = resolved(
            await resolver.resolve(
                QueryRequest.model_validate(
                    {
                        "query": "How has the number of trials for this drug changed over time?",
                        "drug_name": "Pembrolizumab",
                    }
                )
            )
        )
        assert outcome.plan.filters.interventions == ["Pembrolizumab"]
        assert outcome.plan.filters.keywords is None
        assert outcome.plan.analysis.kind == "time_series"
