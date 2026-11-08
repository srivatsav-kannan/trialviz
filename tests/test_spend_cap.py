"""The Claude spend cap under the conditions that would break a naive one: concurrent
calls, and calls that fail after the request may already have been billed."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import anthropic
import httpx
import pytest

from trialviz.llm.budget import BudgetExceeded, SpendLedger
from trialviz.query.interpreters.base import InterpreterUnavailable
from trialviz.query.interpreters.claude import ClaudeInterpreter
from trialviz.query.request import QueryRequest

_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


async def test_concurrent_calls_cannot_jointly_cross_the_cap(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=1.0)
    outcomes = await asyncio.gather(
        *(ledger.reserve(0.3) for _ in range(5)), return_exceptions=True
    )
    assert sum(o is None for o in outcomes) == 3
    assert sum(isinstance(o, BudgetExceeded) for o in outcomes) == 2

    # A finished call releases its reservation and records only what it really cost.
    # Without the release, the same ledger would stay locked at $0.90 reserved.
    for _ in range(3):
        await ledger.settle(0.3, 0.01)
    await ledger.reserve(0.9)
    # The spending, not the reservations, survives a restart.
    restarted = SpendLedger(tmp_path / "spend.json", cap_usd=1.0)
    assert restarted.spent_usd() == pytest.approx(0.03)


class _Failing:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def create(self, **_: object) -> object:
        raise self.error


@pytest.mark.parametrize(
    ("error", "charged"),
    [
        # The request may have reached the API and been billed, so the cap assumes it was.
        (anthropic.APIConnectionError(request=_REQUEST), "worst case"),
        # The API answered with an error, which is not billed.
        (
            anthropic.InternalServerError(
                "overloaded", response=httpx.Response(529, request=_REQUEST), body=None
            ),
            "nothing",
        ),
        (
            anthropic.AuthenticationError(
                "bad key", response=httpx.Response(401, request=_REQUEST), body=None
            ),
            "nothing",
        ),
        # Anything unexpected (an SDK change, a bug) may have been billed, and must still
        # reach the fallback chain as an unavailable interpreter instead of a server error.
        (TypeError("unexpected keyword argument"), "worst case"),
    ],
)
async def test_failed_calls_are_charged_by_whether_they_could_have_been_billed(
    tmp_path: Path, error: Exception, charged: str
) -> None:
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=20.0)
    interpreter = ClaudeInterpreter(
        _Failing(error), ledger, model="claude-opus-5-5", effort="low", max_tokens=8000
    )
    with pytest.raises(InterpreterUnavailable):
        await interpreter.interpret(QueryRequest(query="q"), date(2026, 11, 7))
    if charged == "worst case":
        assert ledger.spent_usd() > 8000 * 25 / 1_000_000, "the full output allowance is charged"
    else:
        assert ledger.spent_usd() == 0
    # Whatever happened, the reservation is released: the rest of the cap is still usable.
    await ledger.reserve(20.0 - ledger.spent_usd() - 0.001)
