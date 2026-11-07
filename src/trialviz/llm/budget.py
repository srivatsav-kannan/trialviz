"""A hard spending cap for language-model calls.

The cap is enforced before each call, against the worst case: the ledger reserves what
the call would cost if it used every allowed output token, refuses the call if that could
cross the cap, and settles to the real cost afterwards. Spending is persisted to disk, so
the cap holds across restarts and test runs, not just within one process.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TokenPrices:
    """US dollars per million tokens."""

    input: float
    output: float
    cache_write: float
    cache_read: float

    def cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        return (
            input_tokens * self.input
            + output_tokens * self.output
            + cache_write_tokens * self.cache_write
            + cache_read_tokens * self.cache_read
        ) / 1_000_000

    def at_least(self, other: TokenPrices) -> TokenPrices:
        return TokenPrices(
            max(self.input, other.input),
            max(self.output, other.output),
            max(self.cache_write, other.cache_write),
            max(self.cache_read, other.cache_read),
        )


# First-party API list prices (cache writes at the 5-minute rate, 1.25x input).
MODEL_PRICES: dict[str, TokenPrices] = {
    "claude-opus-5-5": TokenPrices(input=4.0, output=20.0, cache_write=5.0, cache_read=0.20),
    "claude-sonnet-5-5": TokenPrices(input=2.0, output=10.0, cache_write=2.5, cache_read=0.20),
}

# A declined request can be rerun server-side on a fallback model with different prices.
# Charging every call at the higher of the two rate cards keeps the cap honest either way.
FALLBACK_PRICES = TokenPrices(input=5.0, output=25.0, cache_write=6.25, cache_read=0.50)


def prices_for(model: str) -> TokenPrices | None:
    prices = MODEL_PRICES.get(model)
    return prices.at_least(FALLBACK_PRICES) if prices else None


class BudgetExceeded(Exception):
    pass


class SpendLedger:
    def __init__(self, path: Path, cap_usd: float) -> None:
        self._path = path
        self._cap = cap_usd
        self._reserved = 0.0
        self._lock = asyncio.Lock()

    @property
    def cap_usd(self) -> float:
        return self._cap

    def spent_usd(self) -> float:
        if not self._path.exists():
            return 0.0
        return float(json.loads(self._path.read_text())["spent_usd"])

    async def reserve(self, worst_case_usd: float) -> None:
        """Hold `worst_case_usd` against the cap, or raise BudgetExceeded."""
        async with self._lock:
            committed = self.spent_usd() + self._reserved
            if committed + worst_case_usd > self._cap:
                raise BudgetExceeded(
                    f"spend cap reached: ${committed:.2f} of ${self._cap:.2f} committed, and "
                    f"this call could cost up to ${worst_case_usd:.2f}"
                )
            self._reserved += worst_case_usd

    async def settle(self, reserved_usd: float, actual_usd: float) -> None:
        """Release a reservation and record what the call really cost."""
        async with self._lock:
            self._reserved = max(0.0, self._reserved - reserved_usd)
            self._write(self.spent_usd() + actual_usd)

    def _write(self, spent: float) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"spent_usd": round(spent, 6)}))
        os.replace(temporary, self._path)
