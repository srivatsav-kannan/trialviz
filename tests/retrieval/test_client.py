"""The registry client against a fake API: paging, retries, the rate limit, the cache."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.retrieval.fake_registry import FakeRegistry
from trialviz.retrieval.client import (
    RateLimiter,
    RegistryClient,
    RegistryQueryError,
    RegistryUnavailable,
    ResponseCache,
)
from trialviz.retrieval.translate import RegistryQuery

QUERY = RegistryQuery((("query.intr", "(pembrolizumab)"),))
FIELDS = ("NCTId", "Phase")


class FakeTime:
    """A clock that only moves when something sleeps, so timing tests run instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


def client_for(
    registry: FakeRegistry, time: FakeTime | None = None, cache: ResponseCache | None = None
) -> RegistryClient:
    time = time or FakeTime()
    return RegistryClient(
        registry.client(),
        RateLimiter(6000, clock=time.clock, sleep=time.sleep),
        cache=cache,
        sleep=time.sleep,
    )


class TestPaging:
    async def test_pages_follow_tokens_and_shrink_to_the_limit(self) -> None:
        registry = FakeRegistry(3003)
        fetched = await client_for(registry).fetch(QUERY, fields=FIELDS, limit=2500)
        assert (len(fetched.studies), fetched.total) == (2500, 3003)
        sent = registry.study_requests()
        assert [p["pageSize"] for p in sent] == ["1000", "1000", "500"]
        assert "countTotal" in sent[0] and all("countTotal" not in p for p in sent[1:])
        assert [p.get("pageToken") for p in sent] == [None, "p1000", "p2000"]
        stable = [
            {k: v for k, v in p.items() if k not in {"pageSize", "pageToken", "countTotal"}}
            for p in sent
        ]
        assert all(p == stable[0] for p in stable), "every page must repeat the first page's query"
        assert len(fetched.requests) == 3

    async def test_paging_stops_when_the_results_run_out(self) -> None:
        registry = FakeRegistry(1200)
        fetched = await client_for(registry).fetch(QUERY, fields=FIELDS, limit=10_000)
        assert len(fetched.studies) == fetched.total == 1200
        assert len(registry.study_requests()) == 2

    async def test_a_count_downloads_nothing_but_one_id(self) -> None:
        registry = FakeRegistry(3003)
        assert await client_for(registry).count(QUERY) == 3003
        [sent] = registry.study_requests()
        assert (sent["pageSize"], sent["fields"], sent["countTotal"]) == ("1", "NCTId", "true")


class TestFailures:
    async def test_throttling_and_server_errors_are_retried_with_backoff(self) -> None:
        registry = FakeRegistry(5, failures=[429, 503])
        time = FakeTime()
        assert await client_for(registry, time).count(QUERY) == 5
        assert len(registry.requests) == 3
        assert [s for s in time.sleeps if s >= 1] == [2.0, 4.0]

    async def test_dropped_connections_are_retried(self) -> None:
        registry = FakeRegistry(5, failures=[httpx.ConnectError])
        assert await client_for(registry).count(QUERY) == 5

    async def test_persistent_failure_gives_up_after_the_attempt_budget(self) -> None:
        registry = FakeRegistry(5, failures=[503] * 10)
        with pytest.raises(RegistryUnavailable, match=r"after 4 attempts.*HTTP 503"):
            await client_for(registry).count(QUERY)
        assert len(registry.requests) == 4

    async def test_a_rejected_query_fails_at_once_with_the_apis_explanation(self) -> None:
        registry = FakeRegistry(5, failures=[400])
        with pytest.raises(RegistryQueryError, match="scripted 400"):
            await client_for(registry).count(QUERY)
        assert len(registry.requests) == 1

    async def test_other_client_errors_are_not_retried(self) -> None:
        registry = FakeRegistry(5, failures=[404])
        with pytest.raises(RegistryUnavailable):
            await client_for(registry).count(QUERY)
        assert len(registry.requests) == 1


class TestRateLimit:
    async def test_requests_are_spaced_to_the_configured_rate(self) -> None:
        time = FakeTime()
        limiter = RateLimiter(50, clock=time.clock, sleep=time.sleep)
        for _ in range(4):
            await limiter.wait()
        assert time.sleeps == [1.2, 1.2, 1.2]

    async def test_idle_time_counts_toward_the_next_slot(self) -> None:
        time = FakeTime()
        limiter = RateLimiter(50, clock=time.clock, sleep=time.sleep)
        await limiter.wait()
        time.now += 5
        await limiter.wait()
        assert time.sleeps == []


class TestCache:
    async def test_a_repeated_request_is_served_from_disk(self, tmp_path: Path) -> None:
        registry = FakeRegistry(2500)
        cache = ResponseCache(tmp_path)
        first = await client_for(registry, cache=cache).fetch(QUERY, fields=FIELDS, limit=2500)
        network_calls = len(registry.requests)
        second = await client_for(registry, cache=cache).fetch(QUERY, fields=FIELDS, limit=2500)
        assert second.studies == first.studies
        assert len(registry.requests) == network_calls + 1, "only /version is asked again"

    async def test_a_registry_refresh_invalidates_the_cache(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        await client_for(FakeRegistry(5), cache=cache).count(QUERY)
        refreshed = FakeRegistry(7, data_version="2026-11-07T10:00:05")
        assert await client_for(refreshed, cache=cache).count(QUERY) == 7

    async def test_failures_are_never_cached(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path)
        registry = FakeRegistry(5, failures=[503] * 4)
        with pytest.raises(RegistryUnavailable):
            await client_for(registry, cache=cache).count(QUERY)
        assert await client_for(FakeRegistry(5), cache=cache).count(QUERY) == 5
