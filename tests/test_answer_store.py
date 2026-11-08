"""Saved answers on both stores: what is saved comes back exactly, only the newest are kept,
and a store that fails never fails a question.

The Postgres store runs against a real Postgres (see the `postgres_dsn` fixture).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from trialviz.store import AnswerStore, FileStore, PostgresStore

StoreFactory = Callable[[int], AnswerStore]


@pytest.fixture(params=["file", "postgres"])
def stores(request: pytest.FixtureRequest, tmp_path: Path) -> StoreFactory:
    if request.param == "file":
        return lambda keep: FileStore(tmp_path / "answers", keep)
    dsn: str = request.getfixturevalue("postgres_dsn")
    with psycopg.connect(dsn) as connection:
        connection.execute("drop table if exists saved_answers")
    return lambda keep: PostgresStore(dsn, keep)


async def test_answers_come_back_exactly_and_only_the_newest_are_kept(
    stores: StoreFactory,
) -> None:
    store = stores(2)
    answers = {f"{n:020x}": {"status": "resolved", "n": n, "text": "é" * n} for n in range(3)}
    for key, answer in answers.items():
        await store.put(key, answer)
    first, *kept = answers
    assert await store.get(first) is None, "the oldest answer was dropped"
    for key in kept:
        assert await stores(2).get(key) == answers[key], "a new process reads the same answer"


async def test_a_failing_store_is_a_miss_and_never_an_error(tmp_path: Path) -> None:
    unreachable = PostgresStore("postgresql://nobody@127.0.0.1:9/none", 10, connect_timeout=1)
    await unreachable.put("0" * 20, {"status": "resolved"})
    assert await unreachable.get("0" * 20) is None

    blocked = tmp_path / "not-a-folder"
    blocked.write_text("a file where the folder should be")
    files = FileStore(blocked, 10)
    await files.put("0" * 20, {"status": "resolved"})
    assert await files.get("0" * 20) is None
