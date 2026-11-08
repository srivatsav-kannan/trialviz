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
4. **Fetch only what the analysis reads.** A count, or a comparison of cohort totals,
   needs records only to cite, since its numbers are the registry's counts, so it fetches
   `citations_per_datum` of them. Mention-only trials that are reported but not counted
   need only enough records to quote. Every other analysis fetches every matching record,
   up to `max_records` per cohort, shared between the cohorts of a comparison.
5. **Conditions include their MeSH descendants.** The registry cannot search the MeSH tree,
   so for each condition two small samples are read: trials that name it, to learn the
   MeSH concept it maps to, and the cohort's near misses, to find MeSH terms under that
   concept ("Carcinoma, Non-Small-Cell Lung" under "Lung Neoplasms"). Those terms are then
   added to the condition's search as exact alternatives.
6. **Above the cap, fetch a reproducible subset.** If more records match than the limit,
   the first ones are fetched in a fixed order (most recent start date first) and the set
   is marked truncated, with the exact total kept, so later stages can say exactly what a
   figure was computed from.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass

import httpx

from trialviz.query.filters import Filters
from trialviz.query.plan import (
    DEFAULT_NAME_MATCHING,
    ComparisonSpec,
    CountSpec,
    NameMatching,
    Plan,
)
from trialviz.retrieval.client import RateLimiter, RegistryClient, ResponseCache, Study
from trialviz.retrieval.translate import (
    MENTION_FIELDS,
    Descendants,
    RegistryQuery,
    cohort_layers,
    fields_for,
    mention_query,
    translate,
)
from trialviz.settings import Settings

MENTION_SAMPLE = 30
"""Mention-only trials fetched when they are reported but not counted: enough to quote."""
CONCEPT_SAMPLE = 200
"""Trials naming a condition read to learn the MeSH concept the name maps to."""
NEAR_MISS_SAMPLE = 2000
"""Near-miss trials read to find MeSH terms under that concept."""
MESH_FIELDS = ("NCTId", "ConditionMeshTerm", "ConditionAncestor")

RequestLog = list[str]
"""Every registry request made for one answer, in order, as full URLs."""

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
    sampled: bool = False
    """Whether records were fetched only to cite, because the results are registry counts."""
    descendants: Descendants | None = None
    """MeSH terms added to each condition's search because they sit under it."""
    lookups: tuple[str, ...] = ()
    """Requests made to find those terms."""

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
        """Whether results describe a subset of the matching trials."""
        return not self.sampled and len(self.studies) < self.total

    @property
    def order(self) -> str | None:
        return self.named.order or (self.mentions.order if self.mentions else None)

    @property
    def requests(self) -> tuple[str, ...]:
        mentions = self.mentions.requests if self.mentions else ()
        return self.lookups + self.named.requests + mentions


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

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def counts_only(plan: Plan) -> bool:
        """Whether the results are registry counts, so records are needed only to cite."""
        analysis = plan.analysis
        return isinstance(analysis, CountSpec) or (
            isinstance(analysis, ComparisonSpec) and analysis.group_by is None
        )

    def record_limit(self, plan: Plan) -> int:
        """How many records each cohort needs (rule 4 above)."""
        if self.counts_only(plan):
            return plan.presentation.citations_per_datum or 0
        if isinstance(plan.analysis, ComparisonSpec):
            return max(1000, self._max_records // len(plan.analysis.cohorts))
        return self._max_records

    async def retrieve(self, plan: Plan) -> Retrieval:
        fields = fields_for(plan)
        limit = self.record_limit(plan)
        matching = plan.retrieval.name_matching or DEFAULT_NAME_MATCHING
        sampled = self.counts_only(plan)
        cohorts = await asyncio.gather(
            *(
                self._cohort(label, layers, fields, limit, matching, sampled)
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

    async def count(self, query: RegistryQuery, log: RequestLog) -> int:
        """Exact match count for an arbitrary query, for stages that need more totals. The
        request is added to `log`, the answer's record of every registry call."""
        log.append(query.url(countTotal="true", fields="NCTId", pageSize="1"))
        return await self._client.count(query)

    async def first_year(self, query: RegistryQuery, date_area: str, log: RequestLog) -> str | None:
        """The year of the earliest date in `date_area` among the query's matches."""
        fetched = await self._client.fetch(
            query, fields=("NCTId", date_area), limit=1, sort=f"{date_area}:asc"
        )
        log.extend(fetched.requests)
        if not fetched.studies:
            return None
        status = fetched.studies[0].get("protocolSection", {}).get("statusModule", {})
        key = "startDateStruct" if date_area == "StartDate" else "completionDateStruct"
        date = status.get(key, {}).get("date")
        return str(date)[:4] if date else None

    async def _cohort(
        self,
        label: str | None,
        layers: tuple[Filters, ...],
        fields: tuple[str, ...],
        limit: int,
        matching: NameMatching,
        sampled: bool,
    ) -> CohortRecords:
        counts_mentions = matching is NameMatching.MENTIONED
        descendants, lookups = await self._descendants(layers)
        named = await self._records(translate(*layers, descendants=descendants), fields, limit)
        mentions = None
        if (query := mention_query(*layers, descendants=descendants)) is not None:
            # Counted mentions share the cohort's limit with named trials. Reported
            # mentions are only quoted, so a few records are enough.
            mention_limit = (
                max(0, limit - len(named.studies)) if counts_mentions else MENTION_SAMPLE
            )
            mention_fields = tuple(dict.fromkeys([*fields, *MENTION_FIELDS]))
            mentions = await self._records(query, mention_fields, mention_limit)
        return CohortRecords(
            label, named, mentions, counts_mentions, sampled, descendants, tuple(lookups)
        )

    async def _descendants(self, layers: tuple[Filters, ...]) -> tuple[Descendants, RequestLog]:
        """For each condition in the layers, the MeSH terms that sit under the MeSH concept
        it maps to (rule 5 above), and the requests made to find them.

        The concept is read from a sample of trials naming the condition: among the MeSH
        nodes that cover at least three quarters of them, counting ancestors, it is the one
        most often used directly as a trial's condition term. For "lung cancer" that is
        "Lung Neoplasms", which sits over 88 percent of the sample, while broader nodes such
        as "Lung Diseases" cover as many trials but are rarely a trial's own term.

        A near-miss trial with exactly one MeSH condition term, whose ancestors include the
        concept, shows that term sits under it. Trials with several terms say nothing about
        which one is under the concept, since their ancestors are pooled: a trial listing
        "Adenocarcinoma of Lung" and plain "Adenocarcinoma" would otherwise make every
        adenocarcinoma a lung cancer.
        """
        names = list(dict.fromkeys(n for layer in layers for n in (layer.conditions or [])))
        near_miss_query = mention_query(*layers)
        found: dict[str, tuple[str, ...]] = {}
        log: RequestLog = []
        if not names or near_miss_query is None:
            return found, log
        near = await self._client.fetch(
            near_miss_query, fields=MESH_FIELDS, limit=NEAR_MISS_SAMPLE, sort=TRUNCATED_ORDER
        )
        log.extend(near.requests)
        records = [(_meshes(s, "meshes"), _meshes(s, "ancestors")) for s in near.studies]
        for name in names:
            sample = await self._client.fetch(
                translate(Filters(conditions=[name])),
                fields=MESH_FIELDS,
                limit=CONCEPT_SAMPLE,
                sort=TRUNCATED_ORDER,
            )
            log.extend(sample.requests)
            concept = _concept(sample.studies)
            if concept is None:
                found[name] = ()
                continue
            found[name] = tuple(
                sorted(
                    {
                        term
                        for meshes, ancestors in records
                        if len(meshes) == 1 and concept in ancestors
                        for term in meshes
                    }
                    - {concept}
                )
            )
        return found, log

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


CONCEPT_COVERAGE = 0.75


def _concept(studies: tuple[Study, ...]) -> str | None:
    """The MeSH node a condition name stands for, from trials that name it (see
    `Retriever._descendants`), or None when no node covers enough of them."""
    if not studies:
        return None
    covered: Counter[str] = Counter()
    direct: Counter[str] = Counter()
    for study in studies:
        terms = _meshes(study, "meshes")
        covered.update(terms | _meshes(study, "ancestors"))
        direct.update(terms)
    candidates = [t for t, n in covered.items() if n >= CONCEPT_COVERAGE * len(studies)]
    if not candidates:
        return None
    best = max(candidates, key=lambda t: (direct[t], -covered[t], t))
    return best if direct[best] else None


def _meshes(study: Study, key: str) -> set[str]:
    browse = study.get("derivedSection", {}).get("conditionBrowseModule", {})
    return {str(item["term"]) for item in browse.get(key, []) if item.get("term")}


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
