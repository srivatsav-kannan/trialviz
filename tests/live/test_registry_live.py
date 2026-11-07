"""Real requests to ClinicalTrials.gov. Run with `pytest --run-live`.

The offline tests pin the exact parameters the translator writes. These confirm the
registry reads them the way the translator assumes: synonyms expand, injected syntax is
inert, every filter type is accepted, and a full fetch returns every match exactly once.
Requests are spaced at the client's normal rate, so the module takes about half a minute.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from trialviz.query.filters import Filters
from trialviz.query.plan import Plan
from trialviz.retrieval.client import RateLimiter, RegistryClient
from trialviz.retrieval.retriever import Retriever
from trialviz.retrieval.translate import translate

pytestmark = pytest.mark.live


@pytest.fixture
async def client() -> AsyncIterator[RegistryClient]:
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "trialviz-tests/0.1"}) as http:
        yield RegistryClient(http, RateLimiter(40))


async def count(client: RegistryClient, **filters: object) -> int:
    return await client.count(translate(Filters.model_validate(filters)))


async def test_brand_and_code_names_find_the_same_trials_as_the_generic_name(
    client: RegistryClient,
) -> None:
    generic = await count(client, interventions=["pembrolizumab"])
    assert generic > 1000
    assert await count(client, interventions=["Keytruda"]) == generic
    assert await count(client, interventions=["MK-3475"]) == generic


async def test_injected_syntax_is_searched_as_words_not_executed(client: RegistryClient) -> None:
    phase_three = await count(client, phases=["PHASE3"])
    injected = await count(client, keywords=["AREA[Phase]PHASE3"])
    # Executed as syntax it would match every phase 3 trial (about 50,000). As words it
    # matches only records that mention "area", "phase" and "phase3" (under 2,000).
    assert injected < phase_three / 10
    assert await count(client, conditions=['Crohn"s disease']) > 0, "a stray quote must not 400"


async def test_every_filter_type_is_accepted_together(client: RegistryClient) -> None:
    total = await count(
        client,
        conditions=["melanoma"],
        interventions=["pembrolizumab"],
        sponsors=["Merck Sharp & Dohme LLC"],
        keywords=["adjuvant"],
        phases=["PHASE3"],
        statuses=["COMPLETED", "ACTIVE_NOT_RECRUITING"],
        sponsor_classes=["INDUSTRY"],
        intervention_types=["BIOLOGICAL", "DRUG"],
        study_types=["INTERVENTIONAL"],
        countries=["United States"],
        start_year_from=2015,
        start_year_to=2020,
    )
    assert 0 < total < 50


async def test_alternatives_widen_and_cohort_layers_narrow(client: RegistryClient) -> None:
    pembrolizumab = await count(client, interventions=["pembrolizumab"])
    either = await count(client, interventions=["pembrolizumab", "nivolumab"])
    both = await client.count(
        translate(Filters(interventions=["pembrolizumab"]), Filters(interventions=["nivolumab"]))
    )
    assert both < pembrolizumab < either


async def test_a_complete_fetch_returns_every_match_exactly_once(client: RegistryClient) -> None:
    plan = Plan.model_validate(
        {
            "filters": {"conditions": ["melanoma"], "phases": ["PHASE3"], "start_year_from": 2020},
            "analysis": {"kind": "distribution", "group_by": "sponsor_class"},
            "presentation": {"citations_per_datum": 3},
        }
    )
    result = await Retriever(client, max_records=10_000).retrieve(plan)
    [cohort] = result.cohorts
    ids = [study["protocolSection"]["identificationModule"]["nctId"] for study in cohort.studies]
    assert 0 < len(ids) == cohort.total < 1000
    assert len(set(ids)) == len(ids)
    sponsor_classes = {
        study["protocolSection"]["sponsorCollaboratorsModule"]["leadSponsor"]["class"]
        for study in cohort.studies
    }
    assert "INDUSTRY" in sponsor_classes
