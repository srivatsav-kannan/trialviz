"""Helpers shared across test suites."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from trialviz.query.interpreters.base import (
    Interpretation,
    InterpreterIssue,
    InterpreterUnavailable,
)
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import ClarificationNeeded, NoteCode, ResolvedQuery
from trialviz.query.resolver import QueryResolver

TODAY = date(2026, 11, 7)


class ScriptedInterpreter:
    """An interpreter that returns a fixed proposal, so resolver tests control the input
    exactly and can play an interpreter that is wrong in specific ways."""

    def __init__(
        self,
        proposal: Mapping[str, object] | None = None,
        *,
        issue: InterpreterIssue | None = None,
        unavailable: str | None = None,
        name: str = "scripted",
    ) -> None:
        self._proposal = proposal or {}
        self._issue = issue
        self._unavailable = unavailable
        self._name = name
        self.calls: list[QueryRequest] = []

    @property
    def name(self) -> str:
        return self._name

    async def interpret(self, request: QueryRequest, today: date) -> Interpretation:
        self.calls.append(request)
        if self._unavailable is not None:
            raise InterpreterUnavailable(self._unavailable)
        return Interpretation(interpreter=self._name, proposal=self._proposal, issue=self._issue)


async def resolve(
    request: Mapping[str, Any], proposal: Mapping[str, object] | None = None, **scripted: Any
) -> ResolvedQuery | ClarificationNeeded:
    interpreter = ScriptedInterpreter(proposal, **scripted)
    resolver = QueryResolver(interpreter, today=lambda: TODAY)
    return await resolver.resolve(QueryRequest.model_validate(request))


def resolved(outcome: ResolvedQuery | ClarificationNeeded) -> ResolvedQuery:
    assert isinstance(outcome, ResolvedQuery), f"expected a plan, got {outcome!r}"
    assert_provenance_complete(outcome)
    return outcome


def clarification(outcome: ResolvedQuery | ClarificationNeeded) -> ClarificationNeeded:
    assert isinstance(outcome, ClarificationNeeded), f"expected a clarification, got {outcome!r}"
    return outcome


def plan_slots(outcome: ResolvedQuery) -> dict[str, Any]:
    """Every non-null top-level slot of the plan, keyed by path."""
    dumped = outcome.plan.model_dump(mode="json")
    return {
        f"{section}.{name}": value
        for section, values in dumped.items()
        for name, value in values.items()
        if value is not None
    }


def assert_provenance_complete(outcome: ResolvedQuery) -> None:
    """Every value in the plan says where it came from, and nothing else claims a source."""
    assert set(outcome.provenance) == set(plan_slots(outcome))


def note_codes(outcome: ResolvedQuery | ClarificationNeeded) -> list[NoteCode]:
    return [note.code for note in outcome.notes]


def notes_about(outcome: ResolvedQuery | ClarificationNeeded, slot: str) -> list[str]:
    return [note.message for note in outcome.notes if note.slot == slot]
