"""What a frontend built from the documentation alone relies on.

A developer handed only the README, docs/api.md and /openapi.json should be able to draw
every answer, as a separate developer did when the service was audited this way (see
docs/testing.md). These tests hold the service to what those documents promise, over every
saved answer in examples/: the published response model, the row models the OpenAPI
document publishes for each chart, ids that tie a clicked mark to its row, encodings that
name fields the rows really have, CSV downloads, and an API that a page on another origin
can call.
"""

from __future__ import annotations

import csv
import io
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
from trialviz.visualization.contract import ROWS, validate_rows
from trialviz.vocabulary.analysis import ChartType

ROOT = Path(__file__).parent.parent
SAMPLES = sorted((ROOT / "examples").glob("*.json"))


def _table_chart(answer: dict[str, Any]) -> ChartType:
    """A table draws the rows of the chart it accompanies."""
    return ChartType(answer["visualization"]["type"])


def _views() -> Iterator[tuple[str, dict[str, Any], Any]]:
    """Every chart in every saved answer, with the rows it draws: its own, or the
    recommendation's when an alternative's `data` is null, as docs/api.md says."""
    for path in SAMPLES:
        answer = json.loads(path.read_text())["response"]
        if answer["status"] != "resolved":
            continue
        shared = answer["visualization"]["data"]
        for view in [answer["visualization"], *answer["alternatives"]]:
            data = shared if view["data"] is None else view["data"]
            chart = ChartType(view["type"])
            if chart is ChartType.TABLE:
                chart = _table_chart(answer) if view["data"] is None else _shape_of(data)
            yield path.stem, view | {"shape": chart}, data


def _shape_of(rows: Any) -> ChartType:
    """The chart whose row model fits a table carrying rows of its own."""
    first = rows[0] if isinstance(rows, list) and rows else {}
    return ChartType.BOX_PLOT if "median" in first else ChartType.HISTOGRAM


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


def test_every_chart_carries_the_rows_its_published_model_describes() -> None:
    for sample, view, data in _views():
        assert data, (sample, view["type"])
        validate_rows(view["shape"], data)
        for rows in _parts(data).values():
            ids = [row["id"] for row in rows]
            assert len(ids) == len(set(ids)), (sample, view["type"], "ids repeat")
            for row in rows:
                for citation in row.get("citations", []):
                    assert citation["url"].endswith(citation["nct_id"]), (sample, citation)
        if view["vega_lite"] and "values" in view["vega_lite"].get("data", {}):
            # A clicked mark carries its row's id, as docs/api.md promises.
            values = view["vega_lite"]["data"]["values"]
            assert [v["id"] for v in values] == [row["id"] for row in data], sample


def test_every_encoding_names_a_field_its_rows_carry() -> None:
    for sample, view, data in _views():
        fields = {key for rows in _parts(data).values() for row in rows for key in row}
        for channel, spec in view["encoding"].items():
            assert spec["field"] in fields, (sample, view["type"], channel, spec["field"])


def test_docs_name_every_row_field_and_the_samples_draw_every_chart() -> None:
    api = (ROOT / "docs" / "api.md").read_text()
    table = api[api.index("### Rows by chart") : api.index("### Drawing an answer")]
    documented = set(re.findall(r"`([a-z0-9_]+)`", table))
    for model in ROWS.values():
        for name in model.model_fields:
            nested = name in {"nodes", "edges", "links"}
            assert nested or name in documented, (model.__name__, name)
    drawn = {view["type"] for _, view, _ in _views()}
    assert drawn == {chart.value for chart in ChartType}, {c.value for c in ChartType} - drawn


def test_the_published_schema_types_every_chart_row() -> None:
    schema = (
        TestClient(create_app(_settings(), interpreter=RuleBasedInterpreter()))
        .get("/openapi.json")
        .json()
    )
    components = schema["components"]["schemas"]
    text = json.dumps(schema)
    assert set(re.findall(r"#/components/schemas/([A-Za-z_]+)", text)) <= set(components)
    data = components["Visualization"]["properties"]["data"]["anyOf"]
    named = {
        ref.split("/")[-1]
        for ref in re.findall(r'"(#/components/schemas/[A-Za-z]+)"', json.dumps(data))
    }
    assert named == {model.__name__ for model in ROWS.values()}


def test_every_sample_downloads_as_csv() -> None:
    client = TestClient(create_app(_settings(), interpreter=RuleBasedInterpreter()))
    for path in SAMPLES:
        answer = json.loads(path.read_text())["response"]
        data = answer["visualization"]["data"]
        parts = ["links", "nodes"] if isinstance(data, dict) else ["links"]
        for part in parts:
            response = client.get(f"/v1/samples/{path.stem}/csv", params={"part": part})
            assert response.status_code == 200, path.stem
            header, *lines = list(csv.reader(io.StringIO(response.text)))
            assert "citations" not in header and "id" in header, (path.stem, header)
            rows = (
                data
                if isinstance(data, list)
                else data["nodes" if part == "nodes" else ("edges" if "edges" in data else "links")]
            )
            assert len(lines) == len(rows), (path.stem, part)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "anthropic_api_key": None,
        "ctgov_cache_dir": None,
        "answer_store_dir": None,
    }
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
