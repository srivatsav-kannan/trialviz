"""The request boundary: what callers may send, and what is refused with a 422.

Caller input is trusted to mean what it says, so it is held to the schema strictly. A
typo'd field name or an empty list is an error rather than something silently ignored,
because silently ignoring it would answer a different question than the one asked.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from trialviz.query.request import QueryRequest
from trialviz.vocabulary import Dimension, Phase


def parse(payload: dict[str, Any]) -> QueryRequest:
    return QueryRequest.model_validate(payload)


def rejection(payload: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as error:
        parse(payload)
    return str(error.value)


class TestAssignmentShorthands:
    def test_the_assignment_example_request_works_verbatim(self) -> None:
        request = parse(
            {
                "query": "How has the number of trials for this drug changed over time?",
                "drug_name": "Pembrolizumab",
            }
        )
        assert request.filters.interventions == ["Pembrolizumab"]
        assert request.drug_name is None, "shorthands are folded away, not kept twice"

    def test_every_shorthand_gets_the_full_parsing_of_its_target(self) -> None:
        request = parse(
            {
                "query": "q",
                "condition": "Melanoma",
                "trial_phase": "Phase I/II",
                "sponsor": "Merck Sharp & Dohme LLC",
                "country": "USA",
                "start_year": 2015,
                "end_year": 2020,
            }
        )
        filters = request.filters
        assert filters.conditions == ["Melanoma"]
        assert filters.phases == [Phase.PHASE1, Phase.PHASE2]
        assert filters.sponsors == ["Merck Sharp & Dohme LLC"]
        assert filters.countries == ["United States"]
        assert (filters.start_year_from, filters.start_year_to) == (2015, 2020)

    def test_a_shorthand_and_its_nested_field_together_are_ambiguous(self) -> None:
        message = rejection(
            {"query": "q", "drug_name": "Keytruda", "filters": {"interventions": ["Opdivo"]}}
        )
        assert "drug_name and filters.interventions are both set" in message

    def test_shorthand_years_obey_the_same_range_rule_as_nested_years(self) -> None:
        assert "runs backwards" in rejection({"query": "q", "start_year": 2020, "end_year": 2015})


class TestClosedSchema:
    @pytest.mark.parametrize(
        "payload",
        [
            {"query": "q", "drug_nmae": "Keytruda"},
            {"query": "q", "filters": {"drug": ["Keytruda"]}},
            {"query": "q", "analysis": {"groupby": "phase"}},
            {"query": "q", "presentation": {"colour": "red"}},
        ],
    )
    def test_unknown_fields_are_rejected_not_ignored(self, payload: dict[str, Any]) -> None:
        assert "Extra inputs are not permitted" in rejection(payload)

    @pytest.mark.parametrize("query", ["", "   ", "\n\t", "x" * 2001])
    def test_the_question_must_be_present_and_bounded(self, query: str) -> None:
        rejection({"query": query})

    def test_an_empty_list_is_refused_rather_than_read_as_no_filter(self) -> None:
        assert "at least 1 item" in rejection({"query": "q", "filters": {"phases": []}})

    def test_a_bare_string_where_a_list_belongs_is_refused(self) -> None:
        assert "expected a list" in rejection({"query": "q", "filters": {"phases": "PHASE3"}})

    @pytest.mark.parametrize("year", ["2015", 1899, 2101, True, 2015.0])
    def test_years_must_be_plausible_integers(self, year: object) -> None:
        rejection({"query": "q", "filters": {"start_year_from": year}})

    def test_every_object_in_the_published_schema_is_closed(self) -> None:
        schema = QueryRequest.model_json_schema()
        objects = [schema, *schema["$defs"].values()]
        open_objects = [
            obj.get("title")
            for obj in objects
            if obj.get("type") == "object" and obj.get("additionalProperties") is not False
        ]
        assert open_objects == []


class TestNormalization:
    def test_entity_names_are_deduplicated_case_insensitively_keeping_first_spelling(
        self,
    ) -> None:
        request = parse(
            {
                "query": "q",
                "filters": {
                    "interventions": ["Pembrolizumab", " pembrolizumab ", "PEMBROLIZUMAB"],
                    "conditions": ["Non  small   cell lung cancer"],
                },
            }
        )
        assert request.filters.interventions == ["Pembrolizumab"]
        assert request.filters.conditions == ["Non small cell lung cancer"]

    def test_enum_lists_are_ordered_canonically_so_equal_requests_are_equal(self) -> None:
        first = parse({"query": "q", "filters": {"statuses": ["completed", "recruiting"]}})
        second = parse({"query": "q", "filters": {"statuses": ["RECRUITING", "Completed"]}})
        assert first == second


class TestAnalysisFields:
    def test_parameters_must_belong_to_the_chosen_kind(self) -> None:
        message = rejection({"query": "q", "analysis": {"kind": "count", "group_by": "phase"}})
        assert "group_by does not apply to a count analysis" in message

    def test_grouping_and_series_must_differ(self) -> None:
        message = rejection({"query": "q", "analysis": {"group_by": "phase", "series_by": "phase"}})
        assert "grouping and series must differ" in message

    def test_a_scatter_needs_two_different_measures(self) -> None:
        rejection({"query": "q", "analysis": {"x": "enrollment", "y": "enrollment"}})

    def test_a_single_valued_dimension_cannot_form_a_self_network(self) -> None:
        message = rejection(
            {"query": "q", "analysis": {"node_types": ["lead_sponsor", "lead_sponsor"]}}
        )
        assert "each trial has only one lead_sponsor" in message

    def test_multi_valued_dimensions_can_form_a_self_network(self) -> None:
        request = parse({"query": "q", "analysis": {"node_types": ["drug", "intervention"]}})
        assert request.analysis.node_types == (Dimension.INTERVENTION, Dimension.INTERVENTION)

    @pytest.mark.parametrize(
        ("cohorts", "expected"),
        [
            ([{"label": "A", "filters": {"interventions": ["A"]}}], "at least 2 items"),
            (
                [{"label": str(i), "filters": {"interventions": [str(i)]}} for i in range(7)],
                "at most 6 items",
            ),
            (
                [
                    {"label": "Keytruda", "filters": {"interventions": ["Keytruda"]}},
                    {"label": "keytruda", "filters": {"interventions": ["pembrolizumab"]}},
                ],
                "labels must be unique",
            ),
            (
                [
                    {"label": "A", "filters": {"interventions": ["Keytruda"]}},
                    {"label": "B", "filters": {"interventions": ["keytruda"]}},
                ],
                "identical filters",
            ),
            (
                [
                    {"label": "A", "filters": {}},
                    {"label": "B", "filters": {"interventions": ["B"]}},
                ],
                "would match everything",
            ),
        ],
    )
    def test_degenerate_comparisons_are_refused(
        self, cohorts: list[dict[str, Any]], expected: str
    ) -> None:
        assert expected in rejection({"query": "q", "analysis": {"cohorts": cohorts}})

    @pytest.mark.parametrize("top_n", [0, 101])
    def test_presentation_bounds(self, top_n: int) -> None:
        rejection({"query": "q", "presentation": {"top_n": top_n}})
