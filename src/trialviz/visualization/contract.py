"""The rows each chart type draws, as models published in the OpenAPI document.

Rows are built as plain dictionaries for speed, so these models are not applied to every
response. They are the contract instead: the OpenAPI document describes `data` with them,
so a generated client gets typed rows, and the contract tests validate every saved answer
against them, so the published shape cannot drift from what the service sends.

Fields named after a dimension or measure ("phase", "country", "enrollment") depend on the
question, so the models allow extra fields and the encoding says which ones a chart reads.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.json_schema import models_json_schema

from trialviz.aggregation.evidence import Citation
from trialviz.vocabulary import ChartType


class _Row(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(
        description="Stable within the answer, and kept in the Vega-Lite values, so a clicked "
        "mark can be matched to its row."
    )


class _Datum(_Row):
    trial_count: int = Field(description="Trials behind this data point.")
    nct_ids: list[str] = Field(
        description="Every trial behind the count, or the fetched ones when the count came "
        "from the registry and can be larger."
    )
    citations: list[Citation] = Field(description="Why each cited trial is counted here.")


class StatRow(_Row):
    """The one row of a `stat`."""

    label: str
    trial_count: int
    citations: list[Citation]


class CategoryRow(_Datum):
    """One group: bar, horizontal bar, donut, treemap and map. The dimension field, such as
    `country`, holds the group's label."""

    key: str = Field(description="The group's value in the registry's vocabulary.")
    iso_a3: str | None = Field(None, description="For countries, the ISO 3166 alpha-3 code.")
    iso_numeric: str | None = Field(None, description="For countries, the ISO numeric code.")


class MatrixRow(_Datum):
    """One combination of two dimensions: grouped, stacked and normalized bars and heatmaps."""


class PeriodRow(_Datum):
    """One period, and series if there is one: time series, stacked area, bars over time."""

    period: str = Field(description='"2019" for a year, "2019-03" for a month.')


class BinRow(_Datum):
    """One histogram bin."""

    bin_start: float
    bin_end: float
    label: str
    open_ended: bool = Field(description="True for a last bin that holds every larger value.")


class Outlier(BaseModel):
    nct_id: str
    value: float


class BoxRow(_Datum):
    """One group's spread. The dimension field holds the group's label."""

    n: int
    min: float
    q1: float
    median: float
    q3: float
    max: float
    whisker_low: float
    whisker_high: float
    mean: float
    small_sample: bool = Field(description="True for fewer than 5 values.")
    outliers: list[Outlier]


class PointRow(_Row):
    """One trial in a scatter, bubble or 3D scatter, with its measure fields."""

    nct_id: str
    title: str
    citations: list[Citation]


class TrialRow(_Row):
    """One trial on a timeline."""

    nct_id: str
    title: str
    phase: str | None
    status: str | None
    sponsor: str | None
    start: str | None
    end: str | None
    enrollment: int | None
    url: str
    citations: list[Citation]


class GraphNode(_Datum):
    label: str
    type: str = Field(description="The dimension this node belongs to.")
    degree: int
    community: int
    x: float
    y: float
    x3: float
    y3: float
    z3: float


class GraphEdge(_Datum):
    source: str
    target: str


class GraphData(BaseModel):
    """A network, in 2D or 3D."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]


class FlowNode(_Datum):
    stage: int
    stage_label: str
    label: str


class FlowLink(_Datum):
    source: str
    target: str


class FlowData(BaseModel):
    """A Sankey diagram's stages and the trials moving between them."""

    nodes: list[FlowNode]
    links: list[FlowLink]


ROWS: dict[ChartType, type[BaseModel]] = {
    ChartType.STAT: StatRow,
    ChartType.BAR_CHART: CategoryRow,
    ChartType.HORIZONTAL_BAR_CHART: CategoryRow,
    ChartType.DONUT_CHART: CategoryRow,
    ChartType.TREEMAP: CategoryRow,
    ChartType.CHOROPLETH_MAP: CategoryRow,
    ChartType.GROUPED_BAR_CHART: MatrixRow,
    ChartType.STACKED_BAR_CHART: MatrixRow,
    ChartType.NORMALIZED_STACKED_BAR_CHART: MatrixRow,
    ChartType.HEATMAP: MatrixRow,
    ChartType.TIME_SERIES: PeriodRow,
    ChartType.STACKED_AREA_CHART: PeriodRow,
    ChartType.HISTOGRAM: BinRow,
    ChartType.BOX_PLOT: BoxRow,
    ChartType.SCATTER_PLOT: PointRow,
    ChartType.BUBBLE_CHART: PointRow,
    ChartType.SCATTER_PLOT_3D: PointRow,
    ChartType.NETWORK_GRAPH: GraphData,
    ChartType.NETWORK_GRAPH_3D: GraphData,
    ChartType.SANKEY: FlowData,
    ChartType.TIMELINE: TrialRow,
}
"""The model for each chart's rows. A `table` draws the rows of the chart it accompanies,
and a bar chart over time uses `PeriodRow`."""

_NESTED: tuple[type[BaseModel], ...] = (GraphData, FlowData)
_LISTED: tuple[type[BaseModel], ...] = tuple(
    dict.fromkeys(m for m in ROWS.values() if m not in _NESTED)
)


def validate_rows(chart: ChartType, data: Any) -> None:
    """Raise if `data` is not the shape `chart` publishes."""
    model = ROWS[chart]
    if model in _NESTED:
        model.model_validate(data)
    elif chart is ChartType.BAR_CHART and data and "period" in data[0]:
        TypeAdapter(list[PeriodRow]).validate_python(data)
    else:
        TypeAdapter(list[model]).validate_python(data)  # type: ignore[valid-type]


DATA_SCHEMA: dict[str, Any] = {
    "description": "The rows the chart draws, whose shape depends on the chart type (see the "
    "row models). Network and Sankey data are objects; every other chart's data is a list.",
    "anyOf": [
        *(
            {"type": "array", "items": {"$ref": f"#/components/schemas/{m.__name__}"}}
            for m in _LISTED
        ),
        *({"$ref": f"#/components/schemas/{m.__name__}"} for m in _NESTED),
    ],
}


def component_schemas() -> dict[str, Any]:
    """The row models as OpenAPI component schemas."""
    _, schema = models_json_schema(
        [(m, "serialization") for m in (*_LISTED, *_NESTED)],
        ref_template="#/components/schemas/{model}",
    )
    defs: dict[str, Any] = schema["$defs"]
    return defs
