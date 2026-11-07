"""Resolution: merging the caller's fields with an interpreter's proposal.

Every test here drives the resolver with a scripted interpreter, so the proposal is
exactly what the test says. Most of them script an interpreter that is wrong in some
specific way (contradicting a field, inventing a value, proposing a pair that cannot hold
together, guessing the wrong kind of analysis) and check that the resolver's guarantees
survive: caller fields win, every plan value has a provenance, every discarded guess is
explained, and an unanswerable request becomes a precise clarification rather than a guess.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.support import (
    TODAY,
    ScriptedInterpreter,
    clarification,
    note_codes,
    notes_about,
    plan_slots,
    resolve,
    resolved,
)
from trialviz.query.interpreters.base import (
    FallbackInterpreter,
    InterpreterIssue,
    IssueKind,
)
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import ClarificationReason, NoteCode, Source
from trialviz.query.resolver import QueryResolver
from trialviz.vocabulary import AnalysisKind, Dimension, Phase, TimeGranularity

Q = "a question whose wording the scripted interpreter ignores"


class TestCallerFieldsWin:
    async def test_a_contradicting_proposal_changes_nothing_but_a_note(self) -> None:
        outcome = resolved(
            await resolve(
                {"query": Q, "filters": {"phases": ["PHASE2"]}, "analysis": {"kind": "count"}},
                {"filters": {"phases": ["PHASE3"]}, "analysis": {"kind": "time_series"}},
            )
        )
        assert outcome.plan.filters.phases == [Phase.PHASE2]
        assert outcome.plan.analysis.kind is AnalysisKind.COUNT
        assert outcome.provenance["filters.phases"] is Source.USER
        [phase_note] = notes_about(outcome, "filters.phases")
        assert "Phase 3" in phase_note and "Phase 2" in phase_note
        assert notes_about(outcome, "analysis.kind"), "the overridden kind is reported too"

    async def test_agreement_in_different_spelling_and_order_is_not_a_conflict(self) -> None:
        outcome = resolved(
            await resolve(
                {
                    "query": Q,
                    "filters": {"phases": ["Phase III", "2"], "interventions": ["Pembrolizumab"]},
                    "analysis": {"kind": "count"},
                },
                {"filters": {"phases": ["PHASE3", "PHASE2"], "interventions": ["pembrolizumab"]}},
            )
        )
        assert note_codes(outcome) == []

    async def test_a_proposal_that_extends_a_list_is_still_overridden_and_reported(self) -> None:
        # The caller asked about one drug; the question mentions two. The caller's field is
        # binding, and the dropped drug is named so the caller can widen it if they meant to.
        outcome = resolved(
            await resolve(
                {"query": Q, "drug_name": "Pembrolizumab", "analysis": {"kind": "count"}},
                {"filters": {"interventions": ["pembrolizumab", "lenvatinib"]}},
            )
        )
        assert outcome.plan.filters.interventions == ["Pembrolizumab"]
        [message] = notes_about(outcome, "filters.interventions")
        assert "lenvatinib" in message

    async def test_zero_is_a_value_not_an_absence(self) -> None:
        outcome = resolved(
            await resolve(
                {
                    "query": Q,
                    "analysis": {"kind": "count"},
                    "presentation": {"citations_per_datum": 0},
                },
                {"presentation": {"citations_per_datum": 5}},
            )
        )
        assert outcome.plan.presentation.citations_per_datum == 0
        assert outcome.provenance["presentation.citations_per_datum"] is Source.USER


class TestInferredValuesAreValidated:
    async def test_an_invented_enum_value_is_dropped_and_the_rest_of_the_reading_kept(
        self,
    ) -> None:
        outcome = resolved(
            await resolve(
                {"query": Q},
                {
                    "filters": {"phases": ["PHASE5"], "statuses": ["recruiting"]},
                    "analysis": {"kind": "count"},
                },
            )
        )
        assert outcome.plan.filters.phases is None
        assert outcome.plan.filters.statuses == ["RECRUITING"]
        [message] = notes_about(outcome, "filters.phases")
        assert "Unknown phase 'PHASE5'" in message

    @pytest.mark.parametrize(
        ("section", "name", "value"),
        [
            ("filters", "start_year_from", "2015ish"),
            ("filters", "start_year_from", True),
            ("filters", "start_year_from", 1066),
            ("filters", "countries", ["Atlantis"]),
            ("filters", "countries", ["Korea"]),
            ("filters", "interventions", "Keytruda"),
            ("analysis", "node_types", ["intervention"]),
            ("analysis", "node_types", ["lead_sponsor", "lead_sponsor"]),
            ("presentation", "top_n", 500),
        ],
    )
    async def test_malformed_values_are_dropped_individually(
        self, section: str, name: str, value: object
    ) -> None:
        outcome = resolved(
            await resolve({"query": Q}, {section: {name: value}, "analysis": {"kind": "count"}})
            if section != "analysis"
            else await resolve(
                {"query": Q, "analysis": {"kind": "network", "node_types": ["condition", "drug"]}},
                {section: {name: value}},
            )
        )
        path = f"{section}.{name}"
        assert outcome.provenance.get(path) is not Source.INFERRED
        [message] = notes_about(outcome, path)
        assert message.startswith(f"Ignored the interpreted {path}:")

    async def test_unknown_fields_and_sections_are_reported_not_trusted(self) -> None:
        outcome = resolved(
            await resolve(
                {"query": Q},
                {
                    "filters": {"drug": ["Keytruda"]},
                    "ranking": {"by": "efficacy"},
                    "analysis": {"kind": "count"},
                },
            )
        )
        messages = " ".join(note.message for note in outcome.notes)
        assert "filters.drug is not a known field" in messages
        assert "'ranking' is not a plan section" in messages
        assert outcome.plan.filters.is_empty()


class TestPairRulesAcrossSources:
    async def test_an_inferred_bound_that_inverts_the_callers_range_is_dropped(self) -> None:
        outcome = resolved(
            await resolve(
                {"query": Q, "start_year": 2015, "analysis": {"kind": "count"}},
                {"filters": {"start_year_to": 2010}},
            )
        )
        assert outcome.plan.filters.start_year_from == 2015
        assert outcome.plan.filters.start_year_to is None
        [message] = notes_about(outcome, "filters.start_year_to")
        assert "runs backwards" in message

    async def test_when_both_bounds_are_inferred_the_less_important_one_goes(self) -> None:
        outcome = resolved(
            await resolve(
                {"query": Q, "analysis": {"kind": "count"}},
                {"filters": {"start_year_from": 2020, "start_year_to": 2015}},
            )
        )
        assert (outcome.plan.filters.start_year_from, outcome.plan.filters.start_year_to) == (
            2020,
            None,
        )

    async def test_an_inferred_grouping_equal_to_the_callers_series_cannot_survive(self) -> None:
        # Pair rules drop the inferred half. That leaves a distribution with no grouping,
        # which must become a clarification, not a plan with a hole in it.
        outcome = clarification(
            await resolve(
                {"query": Q, "analysis": {"series_by": "phase"}},
                {"analysis": {"kind": "distribution", "group_by": "phase"}},
            )
        )
        assert outcome.reason is ClarificationReason.MISSING_PARAMETER
        assert outcome.missing == ["analysis.group_by"]
        assert NoteCode.INFERRED_VALUE_REJECTED in note_codes(outcome)


class TestChoosingTheAnalysis:
    async def test_the_question_cannot_impose_a_kind_the_callers_parameters_rule_out(
        self,
    ) -> None:
        outcome = clarification(
            await resolve(
                {"query": Q, "analysis": {"group_by": "country"}},
                {"analysis": {"kind": "time_series"}},
            )
        )
        assert outcome.reason is ClarificationReason.CONFLICTING_ANALYSIS
        assert "distribution or comparison" in outcome.message
        assert outcome.missing == ["analysis.kind"]

    @pytest.mark.parametrize(
        ("analysis", "expected"),
        [
            ({"group_by": "phase"}, AnalysisKind.DISTRIBUTION),
            ({"granularity": "month"}, AnalysisKind.TIME_SERIES),
            ({"measure": "enrollment"}, AnalysisKind.HISTOGRAM),
            ({"x": "enrollment", "y": "duration_months"}, AnalysisKind.SCATTER),
            ({"node_types": ["lead_sponsor", "intervention"]}, AnalysisKind.NETWORK),
            (
                {
                    "cohorts": [
                        {"label": "A", "filters": {"interventions": ["A"]}},
                        {"label": "B", "filters": {"interventions": ["B"]}},
                    ]
                },
                AnalysisKind.COMPARISON,
            ),
        ],
    )
    async def test_parameters_that_single_out_a_kind_imply_it(
        self, analysis: dict[str, Any], expected: AnalysisKind
    ) -> None:
        outcome = resolved(await resolve({"query": Q, "analysis": analysis, "inference": "off"}))
        assert outcome.plan.analysis.kind is expected
        assert outcome.provenance["analysis.kind"] is Source.DERIVED

    async def test_a_parameter_several_kinds_share_implies_nothing(self) -> None:
        outcome = clarification(
            await resolve({"query": Q, "analysis": {"series_by": "phase"}, "inference": "off"})
        )
        assert outcome.missing == ["analysis.kind"]

    async def test_inferred_parameters_that_do_not_fit_the_callers_kind_are_dropped(
        self,
    ) -> None:
        outcome = resolved(
            await resolve(
                {"query": Q, "analysis": {"kind": "count"}},
                {"analysis": {"group_by": "phase", "granularity": "month"}},
            )
        )
        assert set(notes_about(outcome, "analysis.group_by")) | set(
            notes_about(outcome, "analysis.granularity")
        )
        assert NoteCode.INFERRED_VALUE_NOT_APPLICABLE in note_codes(outcome)
        assert "analysis.group_by" not in outcome.provenance

    async def test_defaults_fill_only_what_nobody_set(self) -> None:
        outcome = resolved(
            await resolve(
                {"query": Q, "analysis": {"granularity": "month"}},
                {"analysis": {"kind": "time_series"}},
            )
        )
        assert outcome.plan.analysis.granularity is TimeGranularity.MONTH
        assert outcome.provenance["analysis.granularity"] is Source.USER
        assert outcome.provenance["analysis.time_field"] is Source.DEFAULT
        assert outcome.provenance["presentation.citations_per_datum"] is Source.DEFAULT

    @pytest.mark.parametrize(
        ("kind", "missing", "hint"),
        [
            ("distribution", ["analysis.group_by"], "phase, status"),
            ("histogram", ["analysis.measure"], "enrollment, duration_months or site_count"),
            ("scatter", ["analysis.x", "analysis.y"], "horizontal axis"),
            ("network", ["analysis.node_types"], "lead_sponsor"),
            ("comparison", ["analysis.cohorts"], "two or more cohorts"),
        ],
    )
    async def test_a_kind_without_its_required_parameters_asks_for_them(
        self, kind: str, missing: list[str], hint: str
    ) -> None:
        outcome = clarification(await resolve({"query": Q}, {"analysis": {"kind": kind}}))
        assert outcome.missing == missing
        assert hint in outcome.message

    async def test_a_single_cohort_is_not_a_comparison(self) -> None:
        outcome = clarification(
            await resolve(
                {"query": Q},
                {
                    "analysis": {
                        "kind": "comparison",
                        "cohorts": [{"label": "A", "filters": {"interventions": ["A"]}}],
                    }
                },
            )
        )
        assert outcome.missing == ["analysis.cohorts"]
        assert NoteCode.INFERRED_VALUE_REJECTED in note_codes(outcome)


class TestComparisons:
    async def test_a_shared_filter_on_the_cohorts_own_field_is_flagged(self) -> None:
        # Shared filters apply to every cohort, so "interventions = Keytruda" shared plus a
        # "Opdivo" cohort means trials with both drugs. That is legal but rarely intended.
        outcome = resolved(
            await resolve(
                {"query": Q, "drug_name": "Keytruda"},
                {
                    "analysis": {
                        "kind": "comparison",
                        "cohorts": [
                            {"label": "Keytruda", "filters": {"interventions": ["Keytruda"]}},
                            {"label": "Opdivo", "filters": {"interventions": ["Opdivo"]}},
                        ],
                    }
                },
            )
        )
        flagged = [n for n in outcome.notes if n.code is NoteCode.COHORT_NARROWED_BY_SHARED_FILTER]
        assert len(flagged) == 2
        assert "'Opdivo'" in flagged[1].message


class TestInterpreterAvailability:
    async def test_inference_off_never_calls_the_interpreter(self) -> None:
        interpreter = ScriptedInterpreter({"filters": {"phases": ["PHASE3"]}})
        resolver = QueryResolver(interpreter, today=lambda: TODAY)
        outcome = resolved(
            await resolver.resolve(
                QueryRequest.model_validate(
                    {"query": "phase 3 trials", "analysis": {"kind": "count"}, "inference": "off"}
                )
            )
        )
        assert interpreter.calls == []
        assert outcome.plan.filters.phases is None
        assert note_codes(outcome) == [NoteCode.QUESTION_NOT_INTERPRETED]
        assert outcome.interpreter is None

    async def test_an_unavailable_interpreter_degrades_to_the_fields_with_the_reason(
        self,
    ) -> None:
        outcome = resolved(
            await resolve(
                {"query": Q, "analysis": {"kind": "count"}}, unavailable="spend cap reached"
            )
        )
        [note] = outcome.notes
        assert note.code is NoteCode.INTERPRETER_UNAVAILABLE
        assert "spend cap reached" in note.message

    async def test_a_fallback_is_reported_with_the_reason_and_the_reader(self) -> None:
        chain = FallbackInterpreter(
            ScriptedInterpreter(unavailable="the Anthropic API returned 529", name="claude"),
            ScriptedInterpreter({"analysis": {"kind": "count"}}, name="rules"),
        )
        resolver = QueryResolver(chain, today=lambda: TODAY)
        outcome = resolved(await resolver.resolve(QueryRequest(query=Q)))
        assert outcome.interpreter == "rules"
        [note] = outcome.notes
        assert note.code is NoteCode.INTERPRETER_FALLBACK
        assert "claude was unavailable (the Anthropic API returned 529)" in note.message

    async def test_an_unresolved_reference_asks_instead_of_guessing(self) -> None:
        outcome = clarification(
            await resolve(
                {"query": "How many trials for this drug?"},
                {"analysis": {"kind": "count"}},
                issue=InterpreterIssue(IssueKind.AMBIGUOUS_REFERENCE, "Which drug?"),
            )
        )
        assert outcome.reason is ClarificationReason.AMBIGUOUS_REFERENCE
        assert outcome.message == "Which drug?"
        assert outcome.interpreter == "scripted"


class TestDeterminism:
    async def test_the_same_request_resolves_to_the_same_bytes(self) -> None:
        request = {"query": Q, "filters": {"statuses": ["completed", "recruiting"]}}
        proposal: dict[str, Any] = {
            "filters": {"countries": ["usa", "Japan"], "phases": ["3", "2"]},
            "analysis": {"kind": "distribution", "group_by": "country"},
        }
        first = resolved(await resolve(request, proposal))
        second = resolved(await resolve(request, proposal))
        assert first.model_dump_json() == second.model_dump_json()
        assert list(first.provenance) == sorted(first.provenance)

    async def test_plan_slots_and_provenance_cover_each_other(self) -> None:
        outcome = resolved(
            await resolve(
                {"query": Q, "drug_name": "Keytruda", "analysis": {"series_by": "phase"}},
                {"analysis": {"kind": "time_series"}, "filters": {"countries": ["Japan"]}},
            )
        )
        assert set(plan_slots(outcome)) == set(outcome.provenance)
        assert outcome.plan.analysis.series_by is Dimension.PHASE
