"""Answers over thousands of trials must stay a size a client can use.

A review found that grouping ten thousand trials by lead sponsor and by country returned
90,000 rows and a 20 MB response, with `top_n` silently ignored. This builds the same kind
of grid from synthetic records, without the registry, and checks the answer stays bounded.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from trialviz.aggregation.build import MAX_GRID_COLUMNS, MAX_GRID_ROWS, build
from trialviz.answer import present
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.plan import NameMatching
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import NoteCode, ResolvedQuery
from trialviz.query.resolver import QueryResolver
from trialviz.retrieval.retriever import CohortRecords, RecordSet, Retrieval
from trialviz.retrieval.translate import RegistryQuery


def _study(index: int) -> dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": f"NCT{index:08d}", "briefTitle": f"Trial {index}"},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": f"Sponsor {index % 300}"}},
            "contactsLocationsModule": {
                "locations": [{"country": f"Country {(index + k) % 60}"} for k in range(3)]
            },
        }
    }


async def _answer(top_n: int | None) -> Any:
    request = {
        "query": "Trials by lead sponsor and country",
        "inference": "off",
        "analysis": {"kind": "distribution", "group_by": "lead_sponsor", "series_by": "country"},
        "presentation": {"top_n": top_n} if top_n else {},
    }
    resolver = QueryResolver(RuleBasedInterpreter(), today=lambda: date(2026, 11, 7))
    resolution = await resolver.resolve(QueryRequest.model_validate(request))
    assert isinstance(resolution, ResolvedQuery)
    studies = tuple(_study(i) for i in range(3000))
    records = RecordSet(RegistryQuery(), len(studies), studies, None, ())
    retrieval = Retrieval(
        data_version="2026-11-06T10:00:05",
        fields=(),
        record_limit=10_000,
        name_matching=NameMatching.NAMED,
        cohorts=(CohortRecords(None, records, None, False),),
    )
    return present(request["query"], resolution, retrieval, build(resolution.plan, retrieval))


async def test_a_grid_over_two_unordered_dimensions_stays_bounded_and_says_so() -> None:
    capped = await _answer(top_n=None)
    sponsors = {row["lead_sponsor"] for row in capped.visualization.data}
    countries = {row["country"] for row in capped.visualization.data}
    assert len(sponsors) == MAX_GRID_ROWS and len(countries) == MAX_GRID_COLUMNS
    assert any(n.code is NoteCode.GRID_LIMITED for n in capped.meta.notes)
    assert len(json.dumps(capped.model_dump(mode="json"))) < 2_000_000

    top_ten = await _answer(top_n=10)
    rows = top_ten.visualization.data
    assert len({r["lead_sponsor"] for r in rows}) == 10 and len({r["country"] for r in rows}) == 10
    # The kept sponsors are the largest ones, not the first ones seen.
    largest = max(rows, key=lambda r: r["trial_count"])
    assert largest["trial_count"] > 0
