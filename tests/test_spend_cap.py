"""The Claude spend cap under the conditions that would break a naive one: concurrent
calls, separate processes sharing one budget, restarts, a process that dies mid-call, a
ledger that cannot be reached, and calls that fail after they may already have been billed.

The cap is tested on both ledgers. The Postgres one runs against a real Postgres (see the
`postgres_dsn` fixture), since its correctness lives in SQL and a database lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
from pathlib import Path

import anthropic
import httpx
import psycopg
import pytest

from trialviz.llm.budget import (
    BudgetExceeded,
    FileLedger,
    Hold,
    Ledger,
    PostgresLedger,
)
from trialviz.query.interpreters.base import InterpreterUnavailable
from trialviz.query.interpreters.claude import ClaudeInterpreter
from trialviz.query.request import QueryRequest

_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

LedgerFactory = Callable[..., Ledger]


@pytest.fixture(params=["file", "postgres"])
def ledgers(request: pytest.FixtureRequest, tmp_path: Path) -> LedgerFactory:
    """Builds ledgers that share one store, as separate processes or restarts would."""
    if request.param == "file":
        return lambda cap, daily=None, **_: FileLedger(tmp_path / "spend.json", cap, daily)
    dsn: str = request.getfixturevalue("postgres_dsn")
    with psycopg.connect(dsn) as connection:
        connection.execute("drop table if exists llm_charges, llm_holds")
    return lambda cap, daily=None, **options: PostgresLedger(dsn, cap, daily, **options)


async def test_concurrent_calls_cannot_jointly_cross_the_cap(ledgers: LedgerFactory) -> None:
    first, second = ledgers(1.0), ledgers(1.0)
    shared = isinstance(first, PostgresLedger)
    contenders = [first, second] * 3 if shared else [first] * 6
    outcomes = await asyncio.gather(
        *(ledger.reserve(0.3) for ledger in contenders), return_exceptions=True
    )
    holds = [o for o in outcomes if isinstance(o, Hold)]
    assert len(holds) == 3, "only three $0.30 holds fit under a $1.00 cap"
    assert sum(isinstance(o, BudgetExceeded) for o in outcomes) == 3

    # Settling releases each hold and records only what the call really cost, so the rest
    # of the cap is usable again. Postgres holds are rows any process can settle.
    for hold in holds:
        await first.settle(hold, 0.01)
    await first.reserve(0.9)
    # Spending survives a restart. A Postgres hold does too, since its call may still be
    # running in another process, while a file ledger's holds live only in its process.
    restarted = ledgers(1.0)
    status = await restarted.status()
    assert status.spent_usd == pytest.approx(0.03 + (0.9 if shared else 0.0))


async def test_the_daily_cap_refuses_before_the_total_cap(ledgers: LedgerFactory) -> None:
    ledger = ledgers(5.0, 0.5)
    await ledger.settle(await ledger.reserve(0.4), 0.4)
    with pytest.raises(BudgetExceeded, match="daily"):
        await ledger.reserve(0.2)
    status = await ledger.status()
    assert status.remaining_usd == pytest.approx(0.1)


async def test_a_hold_left_by_a_dead_process_is_charged_in_full(postgres_dsn: str) -> None:
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("drop table if exists llm_charges, llm_holds")
    died = PostgresLedger(postgres_dsn, 1.0, hold_ttl_seconds=0.0)
    hold = await died.reserve(0.3)
    # The process dies without settling. The next reservation finds the expired hold and
    # charges it in full, since the call may have been billed.
    survivor = PostgresLedger(postgres_dsn, 1.0, hold_ttl_seconds=0.0)
    await survivor.settle(await survivor.reserve(0.1), 0.05)
    assert (await survivor.status()).spent_usd == pytest.approx(0.35)
    # A late settle for the expired hold changes nothing: it was already charged.
    await died.settle(hold, 0.01)
    assert (await survivor.status()).spent_usd == pytest.approx(0.35)


async def test_an_unreachable_ledger_refuses_every_call() -> None:
    ledger = PostgresLedger("postgresql://nobody@127.0.0.1:9/none", 20.0, connect_timeout=1)
    with pytest.raises(BudgetExceeded, match="unreachable"):
        await ledger.reserve(0.1)
    with pytest.raises(BudgetExceeded, match="unreachable"):
        await ledger.status()


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
    ledger = FileLedger(tmp_path / "spend.json", 20.0)
    interpreter = ClaudeInterpreter(
        _Failing(error), ledger, model="claude-opus-5-5", effort="low", max_tokens=8000
    )
    with pytest.raises(InterpreterUnavailable):
        await interpreter.interpret(QueryRequest(query="q"), date(2026, 11, 7))
    if charged == "worst case":
        assert ledger.spent_usd() > 8000 * 25 / 1_000_000, "the full output allowance is charged"
    else:
        assert ledger.spent_usd() == 0
    # Whatever happened, the hold is released: the rest of the cap is still usable.
    await ledger.reserve(20.0 - ledger.spent_usd() - 0.001)
