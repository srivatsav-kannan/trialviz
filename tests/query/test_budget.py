"""The spend ledger: a hard cap that holds under concurrency and across restarts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from trialviz.llm.budget import BudgetExceeded, SpendLedger


async def test_concurrent_reservations_cannot_jointly_exceed_the_cap(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=1.0)
    results = await asyncio.gather(*(ledger.reserve(0.3) for _ in range(5)), return_exceptions=True)
    granted = [r for r in results if r is None]
    refused = [r for r in results if isinstance(r, BudgetExceeded)]
    assert (len(granted), len(refused)) == (3, 2)


async def test_settling_releases_the_reservation_and_records_the_real_cost(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=1.0)
    await ledger.reserve(0.9)
    with pytest.raises(BudgetExceeded):
        await ledger.reserve(0.2)
    await ledger.settle(0.9, 0.05)
    await ledger.reserve(0.9)  # room again, because only $0.05 was really spent
    assert ledger.spent_usd() == pytest.approx(0.05)


async def test_spending_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    await SpendLedger(path, cap_usd=1.0).settle(0.0, 0.95)
    with pytest.raises(BudgetExceeded, match=r"\$0.95 of \$1.00"):
        await SpendLedger(path, cap_usd=1.0).reserve(0.10)
