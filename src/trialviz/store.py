"""Saved answers, so an answer has a link that reopens it and a repeated question costs
nothing.

An answer's id is a hash of the request and the registry's data version, so the same
request against the same data always has the same id. Before answering, the service looks
the id up and returns the saved answer if there is one, without calling the language model
or the registry. A new data version gives new ids, so a saved answer is never served for
data it was not computed from.

On a deployed service the answers live in Postgres beside the spending ledger, since its
disk is wiped on restart; locally they are files. Only the newest answers are kept. Each
answer's request is kept far longer, since it is a few hundred bytes, so a link to an answer
that has expired can still say what was asked and offer to ask it again with today's data.
A rerun is a new answer with a new id, because the registry has moved on since. The store
is a convenience, so its failures never fail a question: a lookup that fails is a miss, and
a save that fails is skipped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import zlib
from pathlib import Path
from typing import Any, Protocol

import psycopg

from trialviz.query.request import QueryRequest

log = logging.getLogger(__name__)

FORMAT = 1
"""Bumped when the answer format changes, so answers saved before are not served."""

REQUESTS_KEPT = 100_000
"""Requests outlive their answers by this much: 100,000 of a few hundred bytes each."""

PRUNE_REQUESTS_EVERY = 100
"""Saves between prunings of the request folder, so a save does not list 100,000 files."""


def key_for(request: QueryRequest, data_version: str) -> str:
    canonical = json.dumps(
        {
            "format": FORMAT,
            "data_version": data_version,
            "request": request.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:20]


class AnswerStore(Protocol):
    async def get(self, answer_id: str) -> dict[str, Any] | None: ...
    async def has(self, answer_id: str) -> bool: ...
    async def request(self, answer_id: str) -> dict[str, Any] | None: ...
    async def put(
        self, answer_id: str, answer: dict[str, Any], request: dict[str, Any]
    ) -> None: ...


class NoStore:
    """Saves nothing, for a service run without a database or answer folder."""

    async def get(self, answer_id: str) -> dict[str, Any] | None:
        return None

    async def has(self, answer_id: str) -> bool:
        return False

    async def request(self, answer_id: str) -> dict[str, Any] | None:
        return None

    async def put(self, answer_id: str, answer: dict[str, Any], request: dict[str, Any]) -> None:
        return None


def _pack(answer: dict[str, Any]) -> bytes:
    return zlib.compress(json.dumps(answer, separators=(",", ":")).encode())


def _unpack(blob: bytes) -> dict[str, Any]:
    answer: dict[str, Any] = json.loads(zlib.decompress(blob))
    return answer


class FileStore:
    """Answers as compressed files in a folder, the newest `keep` of them, and their requests
    in a `requests` folder beside them."""

    def __init__(self, directory: Path, keep: int) -> None:
        self._directory, self._keep = directory, keep
        self._requests = directory / "requests"
        self._saves = 0

    async def has(self, answer_id: str) -> bool:
        return await asyncio.to_thread((self._directory / f"{answer_id}.json.z").is_file)

    async def get(self, answer_id: str) -> dict[str, Any] | None:
        path = self._directory / f"{answer_id}.json.z"
        try:
            return _unpack(await asyncio.to_thread(path.read_bytes))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, zlib.error) as error:
            log.warning("saved answer %s unreadable: %s", answer_id, error)
            return None

    async def request(self, answer_id: str) -> dict[str, Any] | None:
        path = self._requests / f"{answer_id}.json"
        try:
            request: dict[str, Any] = json.loads(await asyncio.to_thread(path.read_text))
            return request
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            log.warning("saved request %s unreadable: %s", answer_id, error)
            return None

    async def put(self, answer_id: str, answer: dict[str, Any], request: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._write, answer_id, _pack(answer), json.dumps(request))
        except OSError as error:
            log.warning("answer %s not saved: %s", answer_id, error)

    def _write(self, answer_id: str, blob: bytes, request: str) -> None:
        self._requests.mkdir(parents=True, exist_ok=True)
        (self._requests / f"{answer_id}.json").write_text(request)
        (self._directory / f"{answer_id}.json.z").write_bytes(blob)
        _prune(self._directory, "*.json.z", self._keep)
        self._saves += 1
        if self._saves % PRUNE_REQUESTS_EVERY == 0:
            _prune(self._requests, "*.json", REQUESTS_KEPT)


def _prune(directory: Path, pattern: str, keep: int) -> None:
    saved = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime_ns)
    for old in saved[: max(0, len(saved) - keep)]:
        old.unlink(missing_ok=True)


_SCHEMA = """
create table if not exists saved_answers (
    id text primary key,
    saved_at timestamptz not null default now(),
    body bytea not null
);
create index if not exists saved_answers_by_age on saved_answers (saved_at);
create table if not exists saved_requests (
    id text primary key,
    saved_at timestamptz not null default now(),
    request text not null
);
create index if not exists saved_requests_by_age on saved_requests (saved_at)
"""


class PostgresStore:
    """Answers as compressed rows in Postgres, the newest `keep` of them. Each operation
    opens a short connection, as the ledger does, to spare a small database's connections."""

    def __init__(self, dsn: str, keep: int, *, connect_timeout: int = 5) -> None:
        self._dsn, self._keep, self._timeout = dsn, keep, connect_timeout
        self._ready = False

    async def _connect(self) -> psycopg.AsyncConnection[tuple[Any, ...]]:
        connection = await psycopg.AsyncConnection.connect(
            self._dsn, connect_timeout=self._timeout, autocommit=True
        )
        if not self._ready:
            await connection.execute(_SCHEMA)
            self._ready = True
        return connection

    async def get(self, answer_id: str) -> dict[str, Any] | None:
        try:
            async with await self._connect() as connection:
                cursor = await connection.execute(
                    "select body from saved_answers where id = %s", (answer_id,)
                )
                row = await cursor.fetchone()
            return _unpack(bytes(row[0])) if row else None
        except (psycopg.Error, ValueError, zlib.error) as error:
            log.warning("saved answer %s unreadable: %s", answer_id, error)
            return None

    async def has(self, answer_id: str) -> bool:
        try:
            async with await self._connect() as connection:
                cursor = await connection.execute(
                    "select 1 from saved_answers where id = %s", (answer_id,)
                )
                return await cursor.fetchone() is not None
        except psycopg.Error as error:
            log.warning("saved answer %s unreadable: %s", answer_id, error)
            return False

    async def request(self, answer_id: str) -> dict[str, Any] | None:
        try:
            async with await self._connect() as connection:
                cursor = await connection.execute(
                    "select request from saved_requests where id = %s", (answer_id,)
                )
                row = await cursor.fetchone()
            request: dict[str, Any] | None = json.loads(row[0]) if row else None
            return request
        except (psycopg.Error, ValueError) as error:
            log.warning("saved request %s unreadable: %s", answer_id, error)
            return None

    async def put(self, answer_id: str, answer: dict[str, Any], request: dict[str, Any]) -> None:
        try:
            async with await self._connect() as connection:
                for table, column, value, keep in (
                    ("saved_answers", "body", _pack(answer), self._keep),
                    ("saved_requests", "request", json.dumps(request), REQUESTS_KEPT),
                ):
                    await connection.execute(
                        f"insert into {table} (id, {column}) values (%s, %s) "
                        "on conflict (id) do nothing",
                        (answer_id, value),
                    )
                    await connection.execute(
                        f"delete from {table} where id in (select id from {table} "
                        "order by saved_at desc offset %s)",
                        (keep,),
                    )
        except psycopg.Error as error:
            log.warning("answer %s not saved: %s", answer_id, error)
