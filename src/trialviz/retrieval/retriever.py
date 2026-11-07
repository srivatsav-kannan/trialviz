"""Fetching the records a plan needs, by fixed rules.

How many requests a plan becomes is decided here, deterministically, from the plan's
shape and the size of the result set:

1. **One query per cohort.** A comparison with three cohorts runs three queries, each
   requiring the shared filters and that cohort's own. Anything else runs one query.
2. **Named trials and mentions.** When a cohort filters on a drug or condition, a second
   query fetches the trials that only mention it in descriptions. The two queries split
   the registry's default search exactly, with no overlap. Under the default
   `name_matching: named`, mentions are fetched and reported but not counted. Under
   `mentioned`, they are counted too.
3. **Count first.** Each query starts with a count-only request, which is cheap and
   decides what to fetch.
4. **Fetch only what the analysis reads.** A count needs records only to cite, so it
   fetches `citations_per_datum` of them. Every other analysis fetches every matching
   record, up to `max_records` per cohort.
5. **Above the cap, fetch a reproducible subset.** If more records match than the limit,
   the first ones are fetched in a fixed order (most recent start date first) and the set
   is marked truncated, with the exact total kept, so later stages can say exactly what a
   figure was computed from.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from trialviz.query.filters import Filters
from trialviz.query.plan import DEFAULT_NAME_MATCHING, CountSpec, NameMatching, Plan
from trialviz.retrieval.client import RateLimiter, RegistryClient, ResponseCache, Study
from trialviz.retrieval.translate import (
    MENTION_FIELDS,
    RegistryQuery,
    cohort_layers,
    fields_for,
    mention_query,
    translate,
)
from trialviz.settings import Settings

TRUNCATED_ORDER = "StartDate:desc"
"""Order used when only part of a result set is fetched. The API returns unsorted results
by default, which would make a partial fetch differ from run to run."""


@dataclass(frozen=True)
class RecordSet:
    """The records fetched for one registry query."""

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
class CohortRecords:
    label: str | None
    """The cohort label for a comparison, otherwise None."""
    named: RecordSet
    """Trials that name the cohort's drugs and conditions in their naming fields."""
    mentions: RecordSet | None
    """Trials that only mention them in descriptions. None when the cohort filters on no
    drug or condition."""
    counts_mentions: bool
    """Whether mentions count toward this cohort's results (name_matching: mentioned)."""

    @property
    def query(self) -> RegistryQuery:
        return self.named.query

    @property
    def studies(self) -> tuple[Study, ...]:
        """The records results are computed from."""
        if self.counts_mentions and self.mentions:
            return self.named.studies + self.mentions.studies
        return self.named.studies

    @property
    def total(self) -> int:
        """Exact number of trials the results describe."""
        if self.counts_mentions and self.mentions:
            return self.named.total + self.mentions.total
        return self.named.total

    @property
    def truncated(self) -> bool:
        return len(self.studies) < self.total

    @property
    def order(self) -> str | None:
        return self.named.order or (self.mentions.order if self.mentions else None)

    @property
    def requests(self) -> tuple[str, ...]:
        return self.named.requests + (self.mentions.requests if self.mentions else ())


@dataclass(frozen=True)
class Retrieval:
    data_version: str
    """The registry's dataTimestamp at fetch time. Results are reproducible per version."""
    fields: tuple[str, ...]
    record_limit: int
    name_matching: NameMatching
    cohorts: tuple[CohortRecords, ...]


class Retriever:
    def __init__(self, client: RegistryClient, *, max_records: int) -> None:
        self._client = client
        self._max_records = max_records

    def record_limit(self, plan: Plan) -> int:
        """How many records each cohort needs (rule 4 above)."""
        if isinstance(plan.analysis, CountSpec):
            return plan.presentation.citations_per_datum or 0
        return self._max_records

    async def retrieve(self, plan: Plan) -> Retrieval:
        fields = fields_for(plan)
        limit = self.record_limit(plan)
        matching = plan.retrieval.name_matching or DEFAULT_NAME_MATCHING
        cohorts = await asyncio.gather(
            *(
                self._cohort(label, layers, fields, limit, matching)
                for label, layers in cohort_layers(plan)
            )
        )
        return Retrieval(
            data_version=await self._client.data_version(),
            fields=fields,
            record_limit=limit,
            name_matching=matching,
            cohorts=tuple(cohorts),
        )

    async def count(self, query: RegistryQuery) -> int:
        """Exact match count for an arbitrary query, for stages that need more totals."""
        return await self._client.count(query)

    async def _cohort(
        self,
        label: str | None,
        layers: tuple[Filters, ...],
        fields: tuple[str, ...],
        limit: int,
        matching: NameMatching,
    ) -> CohortRecords:
        counts_mentions = matching is NameMatching.MENTIONED
        named = await self._records(translate(*layers), fields, limit)
        mentions = None
        if (query := mention_query(*layers)) is not None:
            # Counted mentions share the cohort's limit with named trials. Reported
            # mentions follow the same limit on their own.
            mention_limit = max(0, limit - len(named.studies)) if counts_mentions else limit
            mention_fields = tuple(dict.fromkeys([*fields, *MENTION_FIELDS]))
            mentions = await self._records(query, mention_fields, mention_limit)
        return CohortRecords(label, named, mentions, counts_mentions)

    async def _records(
        self, query: RegistryQuery, fields: tuple[str, ...], limit: int
    ) -> RecordSet:
        total = await self._client.count(query)
        count_request = query.url(countTotal="true", fields="NCTId", pageSize="1")
        if total == 0 or limit == 0:
            return RecordSet(query, total, (), None, (count_request,))
        order = TRUNCATED_ORDER if total > limit else None
        fetched = await self._client.fetch(query, fields=fields, limit=limit, sort=order)
        return RecordSet(
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
