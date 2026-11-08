"""What a frontend built from the documentation alone relies on.

A developer handed only the README, docs/api.md and /openapi.json should be able to draw
every answer, as a separate developer did when the service was audited this way (see
docs/testing.md). These tests hold every saved answer in examples/ to what those documents
promise: the saved files still match the response the code produces today, and every
chart's rows match the row model the OpenAPI document publishes for it, with ids that tie a
clicked mark to its row. Rows are built as plain dictionaries and the models are declared
apart from them, so nothing but this test keeps the two in step.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from trialviz.answer import Answer
from trialviz.api import create_app
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.settings import Settings
from trialviz.visualization.contract import validate_rows
from trialviz.vocabulary.analysis import ChartType

ROOT = Path(__file__).parent.parent
SAMPLES = sorted((ROOT / "examples").glob("*.json"))


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
                # A table draws the rows of the chart it accompanies, unless it has its own.
                chart = (
                    ChartType(answer["visualization"]["type"])
                    if view["data"] is None
                    else _shape_of(data)
                )
            yield path.stem, view | {"shape": chart}, data


def _shape_of(rows: Any) -> ChartType:
    """The chart whose row model fits a table carrying rows of its own."""
    first = rows[0] if isinstance(rows, list) and rows else {}
    return ChartType.BOX_PLOT if "median" in first else ChartType.HISTOGRAM


def _parts(data: Any) -> dict[str, list[dict[str, Any]]]:
    return data if isinstance(data, dict) else {"rows": data}


def test_every_saved_answer_is_the_published_response_model_and_served_unchanged() -> None:
    settings = Settings(_env_file=None, anthropic_api_key=None, answer_store_dir=None)  # type: ignore[call-arg]
    client = TestClient(create_app(settings, interpreter=RuleBasedInterpreter()))
    listed = {item["name"] for item in client.get("/v1/samples").json()}
    assert listed == {path.stem for path in SAMPLES}
    for path in SAMPLES:
        saved = json.loads(path.read_text())["response"]
        # A field the model does not know would be dropped here, and the comparison fails.
        assert Answer.model_validate(saved).model_dump(mode="json") == saved, path.stem
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
