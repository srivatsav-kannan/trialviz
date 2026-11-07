"""Fetching the records a plan needs, by fixed rules.

How many requests a plan becomes is decided here, deterministically, from the plan's
shape and the size of the result set:

1. **One query per cohort.** A comparison with three cohorts runs three queries, each
   requiring the shared filters and that cohort's own. Anything else runs one query.
2. **Count first.** Each query starts with a count-only request, which is cheap and
   decides what to fetch.
3. **Fetch only what the analysis reads.** A count needs records only to cite, so it
   fetches `citations_per_datum` of them. Every other analysis fetches every matching
   record, up to `max_records`.
4. **Above the cap, fetch a reproducible subset.** If more records match than
   `max_records`, the first `max_records` are fetched in a fixed order (most recent start
   date first) and the cohort is marked as truncated, with the exact total kept, so later
   stages can say exactly what a figure was computed from.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from trialviz.query.plan import CountSpec, Plan
from trialviz.retrieval.client import RateLimiter, RegistryClient, ResponseCache, Study
from trialviz.retrieval.translate import RegistryQuery, cohort_queries, fields_for
from trialviz.settings import Settings

TRUNCATED_ORDER = "StartDate:desc"
"""Order used when only part of a result set is fetched. The API returns unsorted results
by default, which would make a partial fetch differ from run to run."""


@dataclass(frozen=True)
class CohortRecords:
    label: str | None
    """The cohort label for a comparison, otherwise None."""
    query: RegistryQuery
    total: int
    """Exact number of matching studies in the registry."""
    studies: tuple[Study, ...]
    order: str | None
    """The sort applied when fetching, or None when every match was fetched."""
    requests: tuple[str, ...]

    @property
    def truncated(self) -> bool:
        return len(self.studies) < self.total


@dataclass(frozen=True)
class Retrieval:
    data_version: str
    """The registry's dataTimestamp at fetch time. Results are reproducible per version."""
    fields: tuple[str, ...]
    record_limit: int
    cohorts: tuple[CohortRecords, ...]


class Retriever:
    def __init__(self, client: RegistryClient, *, max_records: int) -> None:
        self._client = client
        self._max_records = max_records

    def record_limit(self, plan: Plan) -> int:
        """How many records each cohort needs (rule 3 above)."""
        if isinstance(plan.analysis, CountSpec):
            return plan.presentation.citations_per_datum or 0
        return self._max_records

    async def retrieve(self, plan: Plan) -> Retrieval:
        fields = fields_for(plan)
        limit = self.record_limit(plan)
        cohorts = await asyncio.gather(
            *(self._cohort(label, query, fields, limit) for label, query in cohort_queries(plan))
        )
        return Retrieval(
            data_version=await self._client.data_version(),
            fields=fields,
            record_limit=limit,
            cohorts=tuple(cohorts),
        )

    async def count(self, query: RegistryQuery) -> int:
        """Exact match count for an arbitrary query, for stages that need more totals."""
        return await self._client.count(query)

    async def _cohort(
        self, label: str | None, query: RegistryQuery, fields: tuple[str, ...], limit: int
    ) -> CohortRecords:
        total = await self._client.count(query)
        count_request = query.url(countTotal="true", fields="NCTId", pageSize="1")
        if total == 0 or limit == 0:
            return CohortRecords(label, query, total, (), None, (count_request,))
        order = TRUNCATED_ORDER if total > limit else None
        fetched = await self._client.fetch(query, fields=fields, limit=limit, sort=order)
        return CohortRecords(
            label=label,
            query=query,
            total=fetched.total or total,
            studies=fetched.studies,
            order=order,
            requests=(count_request, *fetched.requests),
        )


def build_retriever(settings: Settings, http: httpx.AsyncClient | None = None) -> Retriever:
    """Wire a retriever from settings. The caller owns `http` if it passes one."""
    client = RegistryClient(
        http
        or httpx.AsyncClient(
            timeout=settings.ctgov_timeout_seconds,
            headers={"User-Agent": "trialviz/0.1", "Accept": "application/json"},
        ),
        RateLimiter(settings.ctgov_requests_per_minute),
        cache=ResponseCache(settings.ctgov_cache_dir) if settings.ctgov_cache_dir else None,
        api_root=settings.ctgov_api_root,
    )
    return Retriever(client, max_records=settings.retrieval_max_records)
