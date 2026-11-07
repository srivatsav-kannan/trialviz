"""The Claude interpreter with the API replaced by a fake.

These run offline and cost nothing. They cover what the wrapper itself is responsible
for: the exact request it sends, turning slot assignments into a proposal, the spend cap
(enforced before any call), and turning every failure into InterpreterUnavailable so the
fallback chain can take over. Whether Claude reads questions well is a different
question, answered by the live tests and the interpretation eval.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from tests.support import TODAY, clarification, notes_about, resolved
from trialviz.llm.budget import SpendLedger, prices_for
from trialviz.query.filters import Filters
from trialviz.query.interpreters.base import InterpreterUnavailable, IssueKind
from trialviz.query.interpreters.claude import (
    COHORT_SLOT_PATHS,
    FALLBACK_BETA,
    OUTPUT_SCHEMA,
    SLOT_GUIDE,
    SLOT_PATHS,
    SYSTEM_PROMPT,
    ClaudeInterpreter,
)
from trialviz.query.request import PLAN_SECTIONS, QueryRequest
from trialviz.query.resolution import NoteCode
from trialviz.query.resolver import QueryResolver

MODEL = "claude-opus-5-5"
_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def reply(
    slots: list[dict[str, Any]] | None = None,
    *,
    cohorts: list[dict[str, Any]] | None = None,
    issue: dict[str, str] | None = None,
    unexpressed: list[str] | None = None,
    stop_reason: str = "end_turn",
    text: str | None = None,
) -> SimpleNamespace:
    body = (
        text
        if text is not None
        else json.dumps(
            {
                "slots": slots or [],
                "cohorts": cohorts,
                "unexpressed": unexpressed or [],
                "issue": issue,
            }
        )
    )
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=body),
        ],
        usage=SimpleNamespace(
            input_tokens=3000,
            output_tokens=400,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class FakeMessages:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def ledger(tmp_path: Path) -> SpendLedger:
    return SpendLedger(tmp_path / "spend.json", cap_usd=20.0)


def interpreter(
    messages: FakeMessages, ledger: SpendLedger, model: str = MODEL
) -> ClaudeInterpreter:
    return ClaudeInterpreter(messages, ledger, model=model, effort="low", max_tokens=8000)


async def interpret(outcome: Any, ledger: SpendLedger, **request: Any) -> Any:
    fake = FakeMessages(outcome)
    result = await interpreter(fake, ledger).interpret(
        QueryRequest.model_validate({"query": "q", **request}), TODAY
    )
    return result, fake


class TestTheRequestSent:
    async def test_shape(self, ledger: SpendLedger) -> None:
        _, fake = await interpret(
            reply(), ledger, query="phase 3 trials for this drug", drug_name="Keytruda"
        )
        [call] = fake.calls
        assert call["model"] == MODEL
        assert call["output_config"] == {
            "effort": "low",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        }
        assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
        assert call["system"][0]["text"] == SYSTEM_PROMPT
        assert "tool_choice" not in call, "forced tool choice is rejected by this model"
        message = call["messages"][0]["content"]
        assert "Today's date: 2026-11-07" in message
        assert '"interventions": [\n      "Keytruda"' in message
        assert message.endswith("<question>\nphase 3 trials for this drug\n</question>")

    async def test_the_system_prompt_is_identical_across_requests_so_it_caches(
        self, ledger: SpendLedger
    ) -> None:
        _, first = await interpret(reply(), ledger, query="one", drug_name="A")
        _, second = await interpret(reply(), ledger, query="two", condition="B")
        assert first.calls[0]["system"] == second.calls[0]["system"]


class TestReadingTheReply:
    async def test_assignments_become_a_proposal_with_typed_values(
        self, ledger: SpendLedger
    ) -> None:
        result, _ = await interpret(
            reply(
                [
                    {"slot": "filters.start_year_from", "values": ["2015"]},
                    {"slot": "filters.interventions", "values": ["Keytruda"]},
                    {"slot": "filters.interventions", "values": ["Opdivo"]},
                    {"slot": "analysis.kind", "values": ["time_series"]},
                    {"slot": "analysis.node_types", "values": ["lead_sponsor", "intervention"]},
                ],
                cohorts=[
                    {
                        "label": "A",
                        "slots": [{"slot": "filters.conditions", "values": ["Melanoma"]}],
                    }
                ],
            ),
            ledger,
        )
        proposal = result.proposal
        assert proposal["filters"] == {
            "start_year_from": 2015,
            "interventions": ["Keytruda", "Opdivo"],
        }
        assert proposal["analysis"]["kind"] == "time_series"
        assert proposal["analysis"]["node_types"] == ["lead_sponsor", "intervention"]
        assert proposal["analysis"]["cohorts"] == [
            {"label": "A", "filters": {"conditions": ["Melanoma"]}}
        ]

    async def test_two_values_for_a_single_valued_slot_are_rejected_not_picked(
        self, ledger: SpendLedger
    ) -> None:
        fake = FakeMessages(
            reply(
                [
                    {"slot": "analysis.kind", "values": ["count"]},
                    {"slot": "filters.start_year_from", "values": ["2015", "2018"]},
                ]
            )
        )
        resolver = QueryResolver(interpreter(fake, ledger), today=lambda: TODAY)
        outcome = resolved(await resolver.resolve(QueryRequest(query="q")))
        assert outcome.plan.filters.start_year_from is None
        assert notes_about(outcome, "filters.start_year_from")

    async def test_constraints_no_slot_can_express_are_reported_not_dropped(
        self, ledger: SpendLedger
    ) -> None:
        fake = FakeMessages(
            reply(
                [{"slot": "analysis.kind", "values": ["count"]}],
                unexpressed=["more than 500 participants"],
            )
        )
        resolver = QueryResolver(interpreter(fake, ledger), today=lambda: TODAY)
        outcome = resolved(await resolver.resolve(QueryRequest(query="big asthma trials")))
        [note] = outcome.notes
        assert note.code is NoteCode.CONSTRAINT_NOT_APPLIED
        assert '"more than 500 participants"' in note.message

    async def test_an_issue_reaches_the_caller_as_a_clarification(
        self, ledger: SpendLedger
    ) -> None:
        fake = FakeMessages(
            reply(issue={"kind": "out_of_scope", "message": "Registry data has no prices."})
        )
        resolver = QueryResolver(interpreter(fake, ledger), today=lambda: TODAY)
        outcome = clarification(await resolver.resolve(QueryRequest(query="cheapest PD-1 drug?")))
        assert outcome.reason.value == IssueKind.OUT_OF_SCOPE.value
        assert outcome.message == "Registry data has no prices."

    @pytest.mark.parametrize(
        ("response", "reason"),
        [
            (reply(stop_reason="refusal"), "declined"),
            (reply(stop_reason="max_tokens"), "cut off"),
            (reply(text="{not json"), "unreadable"),
            (reply(text="[]"), "no proposal"),
        ],
    )
    async def test_unusable_replies_hand_over_to_the_fallback(
        self, response: SimpleNamespace, reason: str, ledger: SpendLedger
    ) -> None:
        with pytest.raises(InterpreterUnavailable, match=reason):
            await interpret(response, ledger)


class TestSpendCap:
    async def test_actual_cost_is_recorded_at_the_conservative_rate(
        self, ledger: SpendLedger
    ) -> None:
        await interpret(reply(), ledger)
        prices = prices_for(MODEL)
        assert prices is not None
        assert ledger.spent_usd() == pytest.approx(
            prices.cost(input_tokens=3000, output_tokens=400)
        )

    async def test_a_call_that_could_cross_the_cap_is_never_made(self, tmp_path: Path) -> None:
        nearly_spent = SpendLedger(tmp_path / "spend.json", cap_usd=0.10)
        await nearly_spent.settle(0.0, 0.05)
        fake = FakeMessages(reply())
        with pytest.raises(InterpreterUnavailable, match="spend cap reached"):
            await interpreter(fake, nearly_spent).interpret(QueryRequest(query="q"), TODAY)
        assert fake.calls == []
        assert nearly_spent.spent_usd() == pytest.approx(0.05)

    async def test_a_model_without_a_known_price_is_never_called(self, ledger: SpendLedger) -> None:
        fake = FakeMessages(reply())
        with pytest.raises(InterpreterUnavailable, match="no price is known"):
            await interpreter(fake, ledger, model="claude-unknown-9").interpret(
                QueryRequest(query="q"), TODAY
            )
        assert fake.calls == []

    async def test_a_dropped_connection_is_charged_in_full(self, ledger: SpendLedger) -> None:
        with pytest.raises(InterpreterUnavailable, match="could not be reached"):
            await interpret(anthropic.APIConnectionError(request=_REQUEST), ledger)
        assert ledger.spent_usd() > 0.15, "the worst case is charged, since billing is unknown"

    async def test_a_rejected_key_costs_nothing(self, ledger: SpendLedger) -> None:
        error = anthropic.AuthenticationError(
            "bad key", response=httpx.Response(401, request=_REQUEST), body=None
        )
        with pytest.raises(InterpreterUnavailable, match="key was rejected"):
            await interpret(error, ledger)
        assert ledger.spent_usd() == 0.0


class TestSchemaStaysInSync:
    def test_every_plan_slot_except_cohorts_can_be_assigned(self) -> None:
        all_paths = {f"{s}.{n}" for s, model in PLAN_SECTIONS.items() for n in model.model_fields}
        assert set(SLOT_PATHS) == all_paths - {"analysis.cohorts"}
        assert set(COHORT_SLOT_PATHS) == {f"filters.{n}" for n in Filters.model_fields}

    def test_the_prompt_explains_every_slot(self) -> None:
        assert set(SLOT_GUIDE) == set(SLOT_PATHS)

    def test_the_schema_uses_only_what_structured_output_supports(self) -> None:
        text = json.dumps(OUTPUT_SCHEMA)
        for keyword in (
            "minimum",
            "maximum",
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "pattern",
        ):
            assert f'"{keyword}"' not in text

        def objects(node: Any) -> list[dict[str, Any]]:
            if isinstance(node, dict):
                found = [node] if node.get("type") == "object" else []
                return found + [o for value in node.values() for o in objects(value)]
            if isinstance(node, list):
                return [o for item in node for o in objects(item)]
            return []

        for obj in objects(OUTPUT_SCHEMA):
            assert obj["additionalProperties"] is False
            assert set(obj["required"]) == set(obj["properties"])
