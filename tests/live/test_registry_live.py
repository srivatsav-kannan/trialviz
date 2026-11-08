"""Real requests to ClinicalTrials.gov that no recording can stand in for. Run with
`pytest --run-live`.

Recordings prove what the registry returned once. These prove the search semantics the
translator relies on still hold today: synonyms expand inside the naming fields,
alternatives widen and cohort layers narrow, every filter type is accepted together, and
names cannot smuggle in query syntax.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from trialviz.query.filters import Filters
from trialviz.retrieval.client import RateLimiter, RegistryClient
from trialviz.retrieval.translate import RegistryQuery, mention_query, translate

pytestmark = pytest.mark.live


@pytest.fixture
async def client() -> AsyncIterator[RegistryClient]:
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "trialviz-tests/0.1"}) as http:
        yield RegistryClient(http, RateLimiter(40))


async def count(client: RegistryClient, *layers: dict[str, object]) -> int:
    return await client.count(translate(*(Filters.model_validate(layer) for layer in layers)))


async def test_search_semantics_the_translator_relies_on(client: RegistryClient) -> None:
    generic = await count(client, {"interventions": ["pembrolizumab"]})
    assert generic > 1000
    # Synonyms expand even when the name must appear in an intervention field.
    assert await count(client, {"interventions": ["Keytruda"]}) == generic
    assert await count(client, {"interventions": ["MK-3475"]}) == generic
    # Values in one filter are alternatives. Layers (shared filters plus a cohort) must all hold.
    either = await count(client, {"interventions": ["pembrolizumab", "nivolumab"]})
    both = await count(
        client, {"interventions": ["pembrolizumab"]}, {"interventions": ["nivolumab"]}
    )
    assert 0 < both < generic < either
    # Plain words in one keyword are all required, so two names narrow rather than widen.
    one = await count(client, {"keywords": ["CAR-T"]})
    together = await count(client, {"keywords": ["CAR-T Non-Hodgkin Lymphoma"]})
    assert 0 < together < one


async def test_named_trials_and_mentions_split_the_default_search_exactly(
    client: RegistryClient,
) -> None:
    # The default search is the registry's own, matching names anywhere in a record.
    shared = Filters(interventions=["Keytruda"], start_year_from=2018)
    cohort = Filters(conditions=["melanoma"])
    default = RegistryQuery(
        (
            ("filter.advanced", "(AREA[StartDate]RANGE[2018-01-01, MAX])"),
            ("query.cond", "(melanoma)"),
            ("query.intr", "(keytruda)"),
        )
    )
    mentions = mention_query(shared, cohort)
    assert mentions is not None
    named_total = await client.count(translate(shared, cohort))
    mention_total = await client.count(mentions)
    assert named_total > 0 and mention_total > 0
    assert named_total + mention_total == await client.count(default)


async def test_every_filter_type_is_accepted_and_names_cannot_inject_syntax(
    client: RegistryClient,
) -> None:
    everything = {
        "conditions": ["melanoma"],
        "interventions": ["pembrolizumab"],
        "sponsors": ["Merck Sharp & Dohme LLC"],
        "keywords": ["adjuvant"],
        "phases": ["PHASE3"],
        "statuses": ["COMPLETED", "ACTIVE_NOT_RECRUITING"],
        "sponsor_classes": ["INDUSTRY"],
        "intervention_types": ["BIOLOGICAL", "DRUG"],
        "study_types": ["INTERVENTIONAL"],
        "countries": ["United States"],
        "start_year_from": 2015,
        "start_year_to": 2020,
    }
    assert 0 < await count(client, everything) < 50

    phase_three = await count(client, {"phases": ["PHASE3"]})
    # Run as syntax this would match every phase 3 trial (about 50,000). As words it
    # matches only records that mention "area", "phase" and "phase3".
    assert await count(client, {"keywords": ["AREA[Phase]PHASE3"]}) < phase_three / 10
    # Stray quotes, brackets and uppercase operators must not fail the request.
    assert await count(client, {"conditions": ['Crohn"s disease (CD']}) > 0
    asthma = await count(client, {"keywords": ["asthma"]})
    assert await count(client, {"keywords": ["RANGE MISSING SEARCH asthma"]}) <= asthma
