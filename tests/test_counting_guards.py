"""Two counting errors an independent audit of the saved answers found, rebuilt from
synthetic records so they cannot return.

The audit recomputed every saved answer against the registry. Breast cancer's
"Individual" sponsors showed 0 where the registry has 4, because a comparison only counted
exactly the cells its fetched subset happened to contain. And the melanoma drug network
lost trials that write "Pembrolizumab (KEYTRUDA®)" while it filed an unrelated drug under
pembrolizumab, because a biological left out by the type filter left its MeSH term unpaired.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from trialviz.aggregation.build import build
from trialviz.aggregation.datasets import Matrix
from trialviz.aggregation.exact import apply_exact_counts
from trialviz.aggregation.extract import drugs
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.plan import NameMatching
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import ResolvedQuery
from trialviz.query.resolver import QueryResolver
from trialviz.retrieval.retriever import CohortRecords, RecordSet, RequestLog, Retrieval
from trialviz.retrieval.translate import RegistryQuery


def _trial(index: int, sponsor_class: str) -> dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": f"NCT{index:08d}"},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": "S", "class": sponsor_class}},
        }
    }


async def test_a_comparison_counts_every_cell_including_ones_its_subset_never_saw() -> None:
    request = {
        "query": "Compare sponsor classes",
        "inference": "off",
        "analysis": {
            "kind": "comparison",
            "group_by": "sponsor_class",
            "cohorts": [
                {"label": "Breast", "filters": {"conditions": ["Breast cancer"]}},
                {"label": "Prostate", "filters": {"conditions": ["Prostate cancer"]}},
            ],
        },
    }
    resolver = QueryResolver(RuleBasedInterpreter(), today=lambda: date(2026, 11, 7))
    resolution = await resolver.resolve(QueryRequest.model_validate(request))
    assert isinstance(resolution, ResolvedQuery)
    # Both subsets are capped. Only Prostate's happens to include an individual sponsor.
    breast = (_trial(1, "INDUSTRY"), _trial(2, "OTHER"))
    prostate = (_trial(3, "INDUSTRY"), _trial(4, "INDIV"))
    cohorts = tuple(
        CohortRecords(
            label, RecordSet(RegistryQuery((("query.cond", label),)), 99, s, None, ()), None, False
        )
        for label, s in (("Breast", breast), ("Prostate", prostate))
    )
    retrieval = Retrieval("2026-11-06T10:00:05", (), 2, NameMatching.NAMED, cohorts)

    # The registry: every cohort has 4 individual sponsors and nothing ambiguous.
    def registry(query: RegistryQuery) -> int:
        clause = dict(query.params)["filter.advanced"]
        return 0 if "AMBIG" in clause or "MISSING" in clause else 4

    async def count(query: RegistryQuery, log: RequestLog) -> int:
        log.append(str(query.params))
        return registry(query)

    async def first_year(*_: object) -> str | None:
        return None

    built = build(resolution.plan, retrieval)
    await apply_exact_counts(resolution.plan, retrieval, built, count, first_year, [])
    matrix = built.dataset
    assert isinstance(matrix, Matrix)
    assert matrix.cells[("Breast", "INDIV")].count == 4
    assert matrix.cells[("Breast", "UNKNOWN")].count == 4, "a value neither subset showed"
    # Values the registry counted as empty everywhere are dropped again.
    assert "AMBIG" not in {key for key, _ in matrix.columns}


def _with(interventions: list[dict[str, Any]], meshes: list[str]) -> dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT00000001"},
            "armsInterventionsModule": {"interventions": interventions},
        },
        "derivedSection": {
            "interventionBrowseModule": {"meshes": [{"term": term} for term in meshes]}
        },
    }


def test_a_bracketed_brand_is_the_drug_and_a_left_out_biological_keeps_its_term() -> None:
    # A second drug without a MeSH term keeps the one-unpaired rule from hiding the bracket.
    bracketed = _with(
        [
            {"type": "DRUG", "name": "Pembrolizumab (KEYTRUDA®)"},
            {"type": "DRUG", "name": "XYZ-101"},
        ],
        ["Pembrolizumab"],
    )
    assert [d.label for d in drugs(bracketed)] == ["Pembrolizumab", "XYZ-101"]

    # Pembrolizumab is a biological here, so a drug-only network leaves it out. Its MeSH
    # term is still its own and must not pass to the one unpaired drug.
    mixed = _with(
        [
            {"type": "BIOLOGICAL", "name": "Pembrolizumab"},
            {"type": "DRUG", "name": "ATRA"},
        ],
        ["Pembrolizumab"],
    )
    assert [d.label for d in drugs(mixed, types=frozenset({"DRUG"}))] == ["ATRA"]
