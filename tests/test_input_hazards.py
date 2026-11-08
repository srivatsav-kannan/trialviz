"""Inputs where a quiet mistake would change the answer instead of failing.

Two kinds: hostile requests, which must be refused with a pointer to the exact problem,
and registry vocabulary traps, where a plausible-looking parse would silently query the
wrong trials. Each case below is either a bug that happened during the build or one
that the real registry data makes easy to hit.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from trialviz.api import create_app
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.settings import Settings
from trialviz.vocabulary import COUNTRIES, Phase, parse_country, parse_phases, scan_countries
from trialviz.vocabulary.trials import parse_status

HOSTILE_REQUESTS: list[tuple[dict[str, Any], str, str]] = [
    ({"filters": {"countries": ["Korea"]}}, "filters.countries[0]", "South Korea or North Korea"),
    ({"filters": {"phases": ["Phase 9"]}}, "filters.phases", "Unknown phase 'Phase 9'"),
    ({"drug_nmae": "Keytruda"}, "drug_nmae", "Extra inputs are not permitted"),
    (
        {"drug_name": "Keytruda", "filters": {"interventions": ["Opdivo"]}},
        "",
        "drug_name and filters.interventions are both set",
    ),
    ({"start_year": 2020, "end_year": 2015}, "filters", "runs backwards"),
    ({"filters": {"start_year_from": "2015"}}, "filters.start_year_from", "valid integer"),
    ({"analysis": {"kind": "count", "group_by": "phase"}}, "analysis", "does not apply to a count"),
    (
        {"analysis": {"node_types": ["lead_sponsor", "sponsor"]}},
        "analysis.node_types",
        "each trial has only one lead_sponsor",
    ),
    (
        {
            "analysis": {
                "cohorts": [
                    {"label": "A", "filters": {"interventions": ["Keytruda", "Opdivo"]}},
                    {"label": "B", "filters": {"interventions": ["opdivo", "KEYTRUDA"]}},
                ]
            }
        },
        "analysis.cohorts",
        "identical filters",
    ),
]

# The developer's .env plays no part: no Claude key, no cache, no saved answers.
SETTINGS = Settings(  # type: ignore[call-arg]
    _env_file=None, anthropic_api_key=None, ctgov_cache_dir=None, answer_store_dir=None
)


def test_hostile_requests_are_refused_with_a_pointer_to_the_problem() -> None:
    client = TestClient(create_app(SETTINGS, interpreter=RuleBasedInterpreter()))
    misses = []
    for extra, path, fragment in HOSTILE_REQUESTS:
        response = client.post("/v1/interpret", json={"query": "q", **extra})
        details = response.json().get("error", {}).get("details", [])
        pointed = any(d["path"] == path and fragment in d["message"] for d in details)
        if response.status_code != 422 or not pointed:
            misses.append((extra, response.status_code, details))
    assert misses == []


def test_registry_vocabulary_traps() -> None:
    # "N/A" contains a slash but is one phase. This was once split like "1/2".
    assert parse_phases(["N/A"]) == [Phase.NA]
    assert parse_phases(["Phase I/II", "III", "phase-3"]) == [
        Phase.PHASE1,
        Phase.PHASE2,
        Phase.PHASE3,
    ]
    with pytest.raises(ValueError):
        parse_phases(["1/5"])

    # Words that mean several statuses must not be quietly narrowed to one.
    for word in ("active", "ongoing"):
        with pytest.raises(ValueError):
            parse_status(word)

    # Registry spellings differ from common names, and filters match bytes exactly.
    assert parse_country("Türkiye") == "Turkey (Türkiye)"
    assert parse_country("ivory coast") == "Côte d’Ivoire"
    assert (
        parse_country("Bonaire, Saint Eustatius and Saba") == "Bonaire, Saint Eustatius and Saba "
    )
    for ambiguous in ("Korea", "Congo"):
        with pytest.raises(ValueError, match=" or "):
            parse_country(ambiguous)
    # No alias may shadow another country's registry name.
    assert [name for name in COUNTRIES if parse_country(name) != name] == []

    # Free-text scanning must not read places inside other places.
    def scan(text: str) -> list[str]:
        return [name for _, _, name in scan_countries(text)]

    assert scan("sites in New Jersey and New South Wales") == []
    assert scan("Papua New Guinea") == ["Papua New Guinea"]
    assert scan("trials in Nigeria and Niger") == ["Nigeria", "Niger"]


class _Broken:
    name = "broken"

    async def interpret(self, request: object, today: object) -> object:
        raise RuntimeError("a bug")


def test_every_failure_has_the_one_error_shape() -> None:
    client = TestClient(create_app(SETTINGS, interpreter=RuleBasedInterpreter()))
    broken = TestClient(create_app(SETTINGS, interpreter=_Broken()), raise_server_exceptions=False)  # type: ignore[arg-type]
    cases = [
        (client.get("/v1/nope"), 404, "not_found"),
        (client.get("/v1/query"), 405, "method_not_allowed"),
        (client.get("/v1/samples/no_such_sample"), 404, "sample_not_found"),
        (broken.post("/v1/interpret", json={"query": "q"}), 500, "internal_error"),
    ]
    for response, status, code in cases:
        assert response.status_code == status
        body = response.json()
        assert set(body) == {"error"} and set(body["error"]) == {"code", "message", "details"}
        assert body["error"]["code"] == code
