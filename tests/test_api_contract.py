"""What a frontend built from the documentation alone relies on.

A developer handed only the README, docs/api.md and /openapi.json should be able to draw
every answer, as a separate developer did when the service was audited this way (see
docs/testing.md). These tests hold the service to what those documents promise, over every
saved answer in examples/: the published response model, the rows each chart type carries,
encodings that name fields the rows really have, citations a client can link, and an API
that a page on another origin can call.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trialviz.answer import Answer
from trialviz.api import create_app
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.settings import Settings
from trialviz.vocabulary.analysis import ChartType

ROOT = Path(__file__).parent.parent
SAMPLES = sorted((ROOT / "examples").glob("*.json"))

DATUM = {"trial_count", "nct_ids", "citations"}
CITATION = {"nct_id", "field", "excerpt", "kind", "url"}

# The "Rows by chart" table in docs/api.md, as field sets. Dimension fields such as
# "country" vary with the question, so they are checked through the encoding instead.
ROWS: dict[str, dict[str, set[str]]] = {
    "stat": {"rows": {"label", "trial_count", "citations"}},
    "bar_chart": {"rows": DATUM},
    "horizontal_bar_chart": {"rows": {"key", *DATUM}},
    "donut_chart": {"rows": {"key", *DATUM}},
    "treemap": {"rows": {"key", *DATUM}},
    "choropleth_map": {"rows": {"key", "iso_a3", "iso_numeric", *DATUM}},
    # A table draws whatever rows its answer has, a list or nodes with edges or links.
    "table": {},
    "grouped_bar_chart": {"rows": DATUM},
    "stacked_bar_chart": {"rows": DATUM},
    "normalized_stacked_bar_chart": {"rows": DATUM},
    "heatmap": {"rows": DATUM},
    "time_series": {"rows": {"period", *DATUM}},
    "stacked_area_chart": {"rows": {"period", *DATUM}},
    "histogram": {"rows": {"bin_start", "bin_end", "label", "open_ended", *DATUM}},
    "box_plot": {
        "rows": {"n", "min", "q1", "median", "q3", "max", "whisker_low", "whisker_high"}
        | {"mean", "small_sample", "outliers", *DATUM}
    },
    "scatter_plot": {"rows": {"nct_id", "title", "citations"}},
    "bubble_chart": {"rows": {"nct_id", "title", "citations"}},
    "scatter_plot_3d": {"rows": {"nct_id", "title", "citations"}},
    "network_graph": {
        "nodes": {"id", "label", "type", "degree", "community", "x", "y", "x3", "y3", "z3"} | DATUM,
        "edges": {"source", "target", *DATUM},
    },
    "sankey": {
        "nodes": {"id", "stage", "stage_label", "label", *DATUM},
        "links": {"source", "target", *DATUM},
    },
    "timeline": {
        "rows": {"nct_id", "title", "phase", "status", "sponsor", "start", "end"}
        | {"enrollment", "url", "citations"}
    },
}
ROWS["network_graph_3d"] = ROWS["network_graph"]


def _views() -> Iterator[tuple[str, dict[str, Any], Any]]:
    """Every chart in every saved answer, with the rows it draws: its own, or the
    recommendation's when an alternative's `data` is null, as docs/api.md says."""
    for path in SAMPLES:
        answer = json.loads(path.read_text())["response"]
        if answer["status"] != "resolved":
            continue
        shared = answer["visualization"]["data"]
        for view in [answer["visualization"], *answer["alternatives"]]:
            yield path.stem, view, shared if view["data"] is None else view["data"]


def _parts(data: Any) -> dict[str, list[dict[str, Any]]]:
    return data if isinstance(data, dict) else {"rows": data}


def test_every_saved_answer_is_the_published_response_model_and_served_unchanged() -> None:
    client = TestClient(create_app(_settings(), interpreter=RuleBasedInterpreter()))
    listed = {item["name"] for item in client.get("/v1/samples").json()}
    assert listed == {path.stem for path in SAMPLES}
    for path in SAMPLES:
        saved = json.loads(path.read_text())["response"]
        Answer.model_validate(saved)
        served = client.get(f"/v1/samples/{path.stem}")
        assert served.status_code == 200 and served.json() == saved, path.stem


def test_every_chart_carries_the_rows_docs_api_describes() -> None:
    for sample, view, data in _views():
        assert data, (sample, view["type"])
        for part, required in ROWS[view["type"]].items():
            rows = _parts(data)[part]
            assert rows, (sample, view["type"], part)
            for row in rows:
                missing = required - row.keys()
                assert not missing, (sample, view["type"], part, missing)
                for citation in row.get("citations", []):
                    assert citation.keys() >= CITATION, (sample, citation)
                    assert citation["url"].endswith(citation["nct_id"]), (sample, citation)


def test_every_encoding_names_a_field_its_rows_carry() -> None:
    for sample, view, data in _views():
        fields = {key for rows in _parts(data).values() for row in rows for key in row}
        for channel, spec in view["encoding"].items():
            assert spec["field"] in fields, (sample, view["type"], channel, spec["field"])


def test_docs_name_every_row_field_and_the_samples_draw_every_chart() -> None:
    api = (ROOT / "docs" / "api.md").read_text()
    table = api[api.index("### Rows by chart") : api.index("## Response to `/v1/interpret`")]
    documented = set(re.findall(r"`([a-z0-9_]+)`", table))
    for parts in ROWS.values():
        for fields in parts.values():
            assert fields <= documented | {"rows"}, fields - documented
    drawn = {view["type"] for _, view, _ in _views()}
    assert drawn == {chart.value for chart in ChartType}, {c.value for c in ChartType} - drawn


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"anthropic_api_key": None, "ctgov_cache_dir": None}
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


ORIGIN = {"origin": "http://localhost:5173"}


@pytest.mark.parametrize("path", ["/v1/query", "/v1/interpret"])
def test_a_page_on_another_origin_can_ask_questions(path: str) -> None:
    client = TestClient(create_app(_settings(), interpreter=RuleBasedInterpreter()))
    preflight = client.options(
        path,
        headers={
            **ORIGIN,
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert "POST" in preflight.headers["access-control-allow-methods"]


def test_a_page_on_another_origin_can_read_errors_and_limits() -> None:
    client = TestClient(
        create_app(_settings(questions_per_minute=1), interpreter=RuleBasedInterpreter())
    )
    question = {"query": "How are melanoma trials distributed across phases?"}
    answered = client.post("/v1/interpret", json=question, headers=ORIGIN)
    refused = client.post("/v1/interpret", json=question, headers=ORIGIN)
    assert refused.status_code == 429
    for response in (answered, refused):
        assert response.headers["access-control-allow-origin"] == "*"
        exposed = response.headers["access-control-expose-headers"].lower()
        assert {"retry-after", "ratelimit-remaining", "ratelimit-reset"} <= set(
            h.strip() for h in exposed.split(",")
        )
