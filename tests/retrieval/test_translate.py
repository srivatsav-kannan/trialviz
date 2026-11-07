"""Plan filters to API parameters: exact strings, injection safety, determinism.

The exact-string assertions are deliberate. These parameters are the contract with
ClinicalTrials.gov, and the live tests confirm the syntax each one relies on.
"""

from __future__ import annotations

from typing import Any

import pytest

from trialviz.query.filters import Filters
from trialviz.query.plan import Plan
from trialviz.retrieval.essie import any_of_terms, sanitize_term
from trialviz.retrieval.translate import (
    BASE_FIELDS,
    STUDIES_URL,
    RegistryQuery,
    cohort_queries,
    fields_for,
    translate,
)


def params(**filters: Any) -> dict[str, str]:
    return dict(translate(Filters.model_validate(filters)).params)


def plan(analysis: dict[str, Any], **filters: Any) -> Plan:
    return Plan.model_validate({"filters": filters, "analysis": analysis, "presentation": {}})


class TestSanitizing:
    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("AREA[Phase]PHASE3", "area phase phase3"),
            ('Crohn"s disease', "crohn s disease"),
            ("(melanoma) OR (glioma)", "melanoma or glioma"),
            ("lung cancer NOT small cell", "lung cancer not small cell"),
            ("CAR-T", "car-t"),
            ("Crohn's disease", "crohn's disease"),
            ("5-FU + leucovorin", "5-fu + leucovorin"),
            ("covid^10 ~fuzzy", "covid 10 fuzzy"),
            ("  spaced\t\tout  ", "spaced out"),
        ],
    )
    def test_syntax_becomes_plain_words(self, raw: str, clean: str) -> None:
        assert sanitize_term(raw) == clean

    def test_lowercase_operator_words_are_left_as_words(self) -> None:
        assert (
            sanitize_term("drugs for anxiety and depression") == "drugs for anxiety and depression"
        )

    def test_terms_that_are_only_syntax_vanish_instead_of_searching_for_nothing(self) -> None:
        assert any_of_terms(['""', "[]", "()"]) is None
        assert params(keywords=['""']) == {}


class TestNameFilters:
    def test_each_name_filter_uses_its_search_parameter(self) -> None:
        assert params(
            conditions=["melanoma"],
            interventions=["Keytruda"],
            sponsors=["Merck Sharp & Dohme LLC"],
            keywords=["PD-1"],
        ) == {
            "query.cond": "(melanoma)",
            "query.intr": "(keytruda)",
            "query.lead": "(merck sharp & dohme llc)",
            "query.term": "(pd-1)",
        }

    def test_values_in_one_filter_are_alternatives(self) -> None:
        assert params(conditions=["breast cancer", "lung cancer"]) == {
            "query.cond": "(breast cancer) OR (lung cancer)"
        }

    def test_a_cohort_adds_to_the_shared_filter_rather_than_replacing_it(self) -> None:
        query = translate(
            Filters(interventions=["Keytruda"]), Filters(interventions=["Opdivo", "Yervoy"])
        )
        assert dict(query.params) == {"query.intr": "((keytruda)) AND ((opdivo) OR (yervoy))"}

    def test_injected_syntax_is_searched_as_words(self) -> None:
        assert params(interventions=["AREA[Phase]PHASE3"]) == {"query.intr": "(area phase phase3)"}


class TestControlledFilters:
    def test_each_controlled_filter_becomes_one_parenthesized_area_clause(self) -> None:
        advanced = params(
            phases=["2", "3"],
            statuses=["recruiting"],
            sponsor_classes=["industry"],
            intervention_types=["device"],
            study_types=["interventional"],
        )["filter.advanced"]
        assert advanced == (
            "(AREA[Phase](PHASE2 OR PHASE3)) AND (AREA[OverallStatus](RECRUITING)) AND "
            "(AREA[LeadSponsorClass](INDUSTRY)) AND (AREA[InterventionType](DEVICE)) AND "
            "(AREA[StudyType](INTERVENTIONAL))"
        )

    def test_countries_are_quoted_and_kept_byte_for_byte(self) -> None:
        advanced = params(countries=["USA", "Côte d’Ivoire", "Bonaire, Saint Eustatius and Saba"])[
            "filter.advanced"
        ]
        assert advanced == (
            '(AREA[LocationCountry]("United States" OR "Côte d’Ivoire" OR '
            '"Bonaire, Saint Eustatius and Saba "))'
        )

    @pytest.mark.parametrize(
        ("years", "clause"),
        [
            ({"start_year_from": 2018}, "AREA[StartDate]RANGE[2018-01-01, MAX]"),
            ({"start_year_to": 2015}, "AREA[StartDate]RANGE[MIN, 2015-12-31]"),
            (
                {"start_year_from": 2020, "start_year_to": 2020},
                "AREA[StartDate]RANGE[2020-01-01, 2020-12-31]",
            ),
        ],
    )
    def test_year_bounds_cover_whole_years(self, years: dict[str, int], clause: str) -> None:
        assert params(**years)["filter.advanced"] == f"({clause})"

    def test_layers_combine_all_their_clauses(self) -> None:
        query = translate(Filters(start_year_from=2018), Filters(start_year_to=2014))
        assert dict(query.params)["filter.advanced"] == (
            "(AREA[StartDate]RANGE[2018-01-01, MAX]) AND (AREA[StartDate]RANGE[MIN, 2014-12-31])"
        )


class TestDeterminism:
    def test_no_filters_means_no_parameters(self) -> None:
        assert translate(Filters()) == RegistryQuery(())
        assert RegistryQuery(()).url() == STUDIES_URL

    def test_equal_plans_give_identical_queries_and_urls(self) -> None:
        first = translate(
            Filters.model_validate({"statuses": ["completed", "recruiting"], "phases": ["3", "2"]})
        )
        second = translate(
            Filters.model_validate(
                {"phases": ["Phase II", "PHASE3"], "statuses": ["RECRUITING", "COMPLETED"]}
            )
        )
        assert first == second
        assert first.url(pageSize="1") == second.url(pageSize="1")
        assert [key for key, _ in first.params] == sorted(key for key, _ in first.params)


class TestCohorts:
    def test_a_comparison_becomes_one_query_per_cohort_in_order(self) -> None:
        comparison = plan(
            {
                "kind": "comparison",
                "cohorts": [
                    {"label": "Keytruda", "filters": {"interventions": ["Keytruda"]}},
                    {"label": "Opdivo", "filters": {"interventions": ["Opdivo"]}},
                ],
            },
            start_year_from=2018,
        )
        [(first_label, first), (second_label, second)] = cohort_queries(comparison)
        assert (first_label, second_label) == ("Keytruda", "Opdivo")
        assert dict(first.params)["query.intr"] == "(keytruda)"
        assert dict(second.params)["filter.advanced"] == "(AREA[StartDate]RANGE[2018-01-01, MAX])"

    def test_anything_else_is_one_unlabeled_query(self) -> None:
        assert [label for label, _ in cohort_queries(plan({"kind": "count"}))] == [None]


class TestFields:
    @pytest.mark.parametrize(
        ("analysis", "expected"),
        [
            ({"kind": "count"}, set()),
            ({"kind": "distribution", "group_by": "country"}, {"LocationCountry"}),
            (
                {
                    "kind": "time_series",
                    "time_field": "completion_date",
                    "series_by": "sponsor_class",
                },
                {"CompletionDate", "LeadSponsorClass", "LeadSponsorName"},
            ),
            (
                {"kind": "histogram", "measure": "duration_months"},
                {"PrimaryCompletionDate", "CompletionDate"},
            ),
            (
                {"kind": "scatter", "x": "enrollment", "y": "site_count"},
                {"EnrollmentCount", "LocationCountry"},
            ),
            (
                {"kind": "network", "node_types": ["intervention", "intervention"]},
                {"InterventionMeshTerm", "ArmGroupInterventionName"},
            ),
        ],
    )
    def test_only_what_the_analysis_reads_is_requested(
        self, analysis: dict[str, Any], expected: set[str]
    ) -> None:
        fields = fields_for(plan(analysis))
        assert fields[: len(BASE_FIELDS)] == BASE_FIELDS
        assert expected <= set(fields)
        assert len(fields) == len(set(fields))
