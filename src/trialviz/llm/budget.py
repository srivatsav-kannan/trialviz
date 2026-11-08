"""A hard spending cap for language-model calls.

The cap is enforced before each call, against the worst case. A ledger sets aside what the
call would cost if it used every allowed output token (a hold), refuses the call if that
could cross the cap, and settles the hold to the real cost afterwards. There is a total cap
and an optional daily cap, so a burst of traffic cannot spend the whole budget in one day.

Two ledgers implement this. `FileLedger` keeps the total in a JSON file, for local runs.
`PostgresLedger` keeps it in a database, for a deployed service, whose disk is wiped on
every restart and which may run several processes at once: holds and charges are rows,
every reservation runs under one database lock, and a hold left behind by a process that
died mid-call is charged in full once it expires, since that call may have been billed.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import psycopg


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
    """The call is refused: it could cross a cap, or the ledger cannot be reached."""


@dataclass(frozen=True)
class Hold:
    """Money set aside for one call until it is settled."""

    id: str
    usd: float


@dataclass(frozen=True)
class SpendStatus:
    """What has been charged, and what is left once the holds of calls still in progress
    are set aside. Both ledgers report the same way."""

    spent_usd: float
    spent_today_usd: float
    cap_usd: float
    daily_cap_usd: float | None
    held_usd: float = 0.0
    held_today_usd: float = 0.0

    @property
    def remaining_usd(self) -> float:
        left = self.cap_usd - self.spent_usd - self.held_usd
        if self.daily_cap_usd is not None:
            left = min(left, self.daily_cap_usd - self.spent_today_usd - self.held_today_usd)
        return max(0.0, left)


class Ledger(Protocol):
    async def reserve(self, worst_case_usd: float) -> Hold:
        """Set `worst_case_usd` aside against the caps, or raise BudgetExceeded."""
        ...

    async def settle(self, hold: Hold, actual_usd: float) -> None:
        """Release a hold and record what the call really cost."""
        ...

    async def status(self) -> SpendStatus: ...


def _refusal(committed: float, cap: float, usd: float, which: str) -> BudgetExceeded:
    return BudgetExceeded(
        f"{which} spend cap reached: ${committed:.2f} of ${cap:.2f} committed, and this call "
        f"could cost up to ${usd:.2f}"
    )


class FileLedger:
    """Spending in a JSON file, with holds kept in memory. Good for one local process."""

    def __init__(self, path: Path, cap_usd: float, daily_cap_usd: float | None = None) -> None:
        self._path = path
        self._cap = cap_usd
        self._daily_cap = daily_cap_usd
        self._holds: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def spent_usd(self) -> float:
        return _spent(self._read())

    def _read(self) -> dict[str, object]:
        """The file's contents. A file that cannot be read refuses every call, as an
        unreachable database does for the Postgres ledger."""
        try:
            if not self._path.exists():
                return {}
            data = json.loads(self._path.read_text())
        except (OSError, ValueError) as error:
            raise BudgetExceeded(
                f"the spend ledger is unreadable ({type(error).__name__})"
            ) from error
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data))
        os.replace(temporary, self._path)

    async def reserve(self, worst_case_usd: float) -> Hold:
        async with self._lock:
            data = await asyncio.to_thread(self._read)
            held = sum(self._holds.values())
            committed = _spent(data) + held
            if committed + worst_case_usd > self._cap:
                raise _refusal(committed, self._cap, worst_case_usd, "total")
            today = _spent_today(data) + held
            if self._daily_cap is not None and today + worst_case_usd > self._daily_cap:
                raise _refusal(today, self._daily_cap, worst_case_usd, "daily")
            hold = Hold(uuid.uuid4().hex, worst_case_usd)
            self._holds[hold.id] = worst_case_usd
            return hold

    async def settle(self, hold: Hold, actual_usd: float) -> None:
        async with self._lock:
            if self._holds.pop(hold.id, None) is None:
                return
            data = await asyncio.to_thread(self._read)
            days = data.get("days", {})
            days = dict(days) if isinstance(days, dict) else {}
            days[_today()] = round(float(days.get(_today(), 0.0)) + actual_usd, 6)
            spent = round(_spent(data) + actual_usd, 6)
            await asyncio.to_thread(self._write, {"spent_usd": spent, "days": days})

    async def status(self) -> SpendStatus:
        data = await asyncio.to_thread(self._read)
        # Holds live only as long as this process, so every one of them is today's.
        held = sum(self._holds.values())
        return SpendStatus(_spent(data), _spent_today(data), self._cap, self._daily_cap, held, held)


def _spent(data: dict[str, object]) -> float:
    spent = data.get("spent_usd", 0.0)
    return float(spent) if isinstance(spent, int | float) else 0.0


def _spent_today(data: dict[str, object]) -> float:
    days = data.get("days", {})
    return float(days.get(_today(), 0.0)) if isinstance(days, dict) else 0.0


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _unreachable(error: Exception) -> BudgetExceeded:
    return BudgetExceeded(f"the spend ledger is unreachable ({type(error).__name__})")


_LOCK_KEY = 0x7472_6961_6C76  # "trialv": one advisory lock serializes every reservation
_SCHEMA = """
create table if not exists llm_charges (
    id bigserial primary key,
    charged_at timestamptz not null default now(),
    usd numeric(12, 6) not null check (usd >= 0)
);
create table if not exists llm_holds (
    id text primary key,
    held_at timestamptz not null default now(),
    usd numeric(12, 6) not null check (usd > 0)
);
"""
_TOTALS = """
select
    coalesce((select sum(usd) from llm_charges), 0),
    coalesce((select sum(usd) from llm_holds), 0),
    coalesce((select sum(usd) from llm_charges where charged_at >= %(day)s), 0),
    coalesce((select sum(usd) from llm_holds where held_at >= %(day)s), 0)
"""


class PostgresLedger:
    """Spending in Postgres, shared by every process and kept across restarts.

    Each operation opens a short connection, because calls are few and a pool would hold
    connections a small free database cannot spare. Any database error refuses the call
    (fail closed): the service then reads the question without the language model.
    """

    def __init__(
        self,
        dsn: str,
        cap_usd: float,
        daily_cap_usd: float | None = None,
        *,
        hold_ttl_seconds: float = 600.0,
        connect_timeout: int = 5,
    ) -> None:
        self._dsn = dsn
        self._cap = cap_usd
        self._daily_cap = daily_cap_usd
        self._ttl = hold_ttl_seconds
        self._timeout = connect_timeout
        self._ready = False

    async def _connect(self) -> psycopg.AsyncConnection[tuple[Any, ...]]:
        connection = await psycopg.AsyncConnection.connect(self._dsn, connect_timeout=self._timeout)
        if not self._ready:
            async with connection.transaction():
                await connection.execute("select pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                await connection.execute(_SCHEMA)
            self._ready = True
        return connection

    async def _status(self, connection: psycopg.AsyncConnection[tuple[Any, ...]]) -> SpendStatus:
        day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        row = await (await connection.execute(_TOTALS, {"day": day})).fetchone()
        assert row is not None
        spent, held, spent_today, held_today = (float(value) for value in row)
        return SpendStatus(spent, spent_today, self._cap, self._daily_cap, held, held_today)

    async def reserve(self, worst_case_usd: float) -> Hold:
        try:
            async with await self._connect() as connection, connection.transaction():
                await connection.execute("select pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                # A hold this old belongs to a call whose process died before settling,
                # and the call may have been billed, so it becomes a charge in full.
                await connection.execute(
                    "with expired as (delete from llm_holds"
                    " where held_at < now() - make_interval(secs => %s) returning held_at, usd)"
                    " insert into llm_charges (charged_at, usd) select held_at, usd from expired",
                    (self._ttl,),
                )
                status = await self._status(connection)
                committed = status.spent_usd + status.held_usd
                today = status.spent_today_usd + status.held_today_usd
                if committed + worst_case_usd > self._cap:
                    raise _refusal(committed, self._cap, worst_case_usd, "total")
                if self._daily_cap is not None and today + worst_case_usd > self._daily_cap:
                    raise _refusal(today, self._daily_cap, worst_case_usd, "daily")
                hold = Hold(uuid.uuid4().hex, worst_case_usd)
                await connection.execute(
                    "insert into llm_holds (id, usd) values (%s, %s)", (hold.id, hold.usd)
                )
                return hold
        except psycopg.Error as error:
            raise _unreachable(error) from error

    async def settle(self, hold: Hold, actual_usd: float) -> None:
        try:
            async with await self._connect() as connection, connection.transaction():
                await connection.execute("select pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                removed = await (
                    await connection.execute(
                        "delete from llm_holds where id = %s returning id", (hold.id,)
                    )
                ).fetchone()
                # A missing hold already expired and was charged in full.
                if removed is not None:
                    await connection.execute(
                        "insert into llm_charges (usd) values (%s)", (round(actual_usd, 6),)
                    )
        except psycopg.Error as error:
            # The hold stays, and is charged in full when it expires: never under-counted.
            raise _unreachable(error) from error

    async def status(self) -> SpendStatus:
        try:
            async with await self._connect() as connection:
                return await self._status(connection)
        except psycopg.Error as error:
            raise _unreachable(error) from error
