"""Visualization specs: the recommended chart and its alternatives, ready to render.

Each spec has a `type`, an `encoding` that maps visual channels to row fields, the `data`
rows, chart-specific `options`, and, for charts Vega-Lite can draw, a complete Vega-Lite
spec. Alternatives that draw the same rows leave `data` out and reuse the recommended
chart's rows. Only an alternative with a different row shape (a box plot alongside a
histogram) carries its own.

Every datum carries `trial_count`, `nct_ids` (every contributing trial) and `citations`
(up to `citations_per_datum`, each a field path and exact excerpt from the registry).
"""

from __future__ import annotations

import statistics
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from trialviz.aggregation.datasets import (
    Categorical,
    Dataset,
    Distribution,
    Flow,
    Graph,
    Group,
    Matrix,
    Points,
    Scalar,
    Spread,
    TimeSeries,
    Trials,
)
from trialviz.aggregation.evidence import Citation
from trialviz.aggregation.extract import skewed
from trialviz.visualization.contract import DATA_SCHEMA
from trialviz.visualization.geo import COUNTRY_CODES
from trialviz.visualization.layout import force_layout
from trialviz.vocabulary import ChartType, Measure, SortOrder, TimeGranularity

COUNT = "trial_count"
WORLD_TOPOLOGY = "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json"
DEFAULT_DISPLAY_LIMIT = 20

FieldType = Literal["nominal", "ordinal", "quantitative", "temporal"]
Rows = list[dict[str, Any]] | dict[str, list[dict[str, Any]]]


class Channel(BaseModel):
    """One visual channel, such as an axis or the color, mapped to a field of the rows."""

    model_config = ConfigDict(frozen=True)

    field: str = Field(description="The row field this channel reads.")
    type: FieldType = Field(description="How the field is read, in Vega-Lite's terms.")
    title: str = Field(description="The axis or legend title.")
    sort: list[str] | Literal["ascending", "descending"] | None = Field(
        None, description="An explicit order of categories, or a sort direction."
    )
    scale: Literal["linear", "symlog"] | None = Field(
        None,
        description="symlog: a log-like scale that also handles zero, for heavily skewed measures.",
    )
    stack: Literal["zero", "normalize"] | None = Field(
        None, description="How marks stack: from zero, or normalized to shares."
    )


class View(BaseModel):
    """A chart over the answer's data, ready to render."""

    type: ChartType
    reason: str = Field(description="Why this chart suits the data.")
    encoding: dict[str, Channel] = Field(
        description="Visual channels (x, y, color, size and so on) mapped to row fields."
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Chart-specific settings, such as `display_limit`, the map's TopoJSON "
        "source, or the Sankey stages.",
    )
    data: Annotated[
        Rows | None, WithJsonSchema({"anyOf": [*DATA_SCHEMA["anyOf"], {"type": "null"}]})
    ] = Field(
        None, description="Rows for this view. Null means it reuses the visualization's rows."
    )
    vega_lite: dict[str, Any] | None = Field(
        None, description="A complete Vega-Lite spec, for charts Vega-Lite can draw."
    )


class Visualization(View):
    """The recommended chart, which always carries its own rows."""

    title: str = Field(description="What was counted and how it was grouped.")
    subtitle: str | None = Field(
        None, description="Constraints that do not fit the title, such as countries or years."
    )
    data: Annotated[Rows, WithJsonSchema(DATA_SCHEMA)] = Field(
        description="The rows the chart draws. Their fields depend on the chart."
    )


# --- Rows ---------------------------------------------------------------------------------


def _datum(group: Group, limit: int) -> dict[str, Any]:
    return {
        COUNT: group.count,
        "nct_ids": list(group.nct_ids),
        "citations": [c.model_dump(mode="json") for c in group.citations_for(limit)],
    }


def _citations_of_trials(citations: list[Citation], trials: int) -> list[dict[str, Any]]:
    """Every citation for the first `trials` trials cited, so each one is fully shown."""
    kept = list(dict.fromkeys(c.nct_id for c in citations))[:trials]
    return [c.model_dump(mode="json") for c in citations if c.nct_id in kept]


def _citations(
    citations: list[Citation] | tuple[Citation, ...], limit: int
) -> list[dict[str, Any]]:
    return [c.model_dump(mode="json") for c in list(citations)[:limit]]


def _sorted_groups(dataset: Categorical, sort: SortOrder | None) -> list[Group]:
    order = sort or (SortOrder.NATURAL if dataset.ordered else SortOrder.VALUE_DESC)
    groups = list(dataset.groups)
    if order is SortOrder.VALUE_DESC:
        groups.sort(key=lambda g: (-g.count, g.label.casefold()))
    elif order is SortOrder.VALUE_ASC:
        groups.sort(key=lambda g: (g.count, g.label.casefold()))
    elif order is SortOrder.LABEL_ASC:
        groups.sort(key=lambda g: g.label.casefold())
    return groups


def rows(dataset: Dataset, chart: ChartType, limit: int, sort: SortOrder | None) -> Rows:
    match dataset:
        case Scalar():
            return [
                {
                    "id": "total",
                    "label": dataset.label,
                    COUNT: dataset.total,
                    "citations": _citations_of_trials(dataset.citations, limit),
                }
            ]
        case Categorical():
            out = []
            for group in _sorted_groups(dataset, sort):
                row: dict[str, Any] = {
                    "id": group.key,
                    dataset.field: group.label,
                    "key": group.key,
                    **_datum(group, limit),
                }
                if dataset.geographic:
                    codes = COUNTRY_CODES.get(group.key)
                    row["iso_a3"], row["iso_numeric"] = codes if codes else (None, None)
                out.append(row)
            return out
        case Matrix():
            return [
                {
                    "id": f"{row_key}|{column_key}",
                    dataset.row_field: row_label,
                    dataset.column_field: column_label,
                    **_datum(dataset.cells.get((row_key, column_key), Group("", "")), limit),
                }
                for row_key, row_label in dataset.rows
                for column_key, column_label in dataset.columns
            ]
        case TimeSeries():
            series = dataset.series or [("", "")]
            return [
                {
                    "id": f"{period}|{key}" if key else period,
                    "period": period,
                    **({dataset.series_field: label} if dataset.series_field else {}),
                    **_datum(dataset.cells.get((period, key), Group("", "")), limit),
                }
                for period in dataset.periods
                for key, label in series
            ]
        case Distribution():
            if _as_boxes(dataset, chart):
                return [_box(dataset, spread, limit) for spread in dataset.spreads]
            return [
                {
                    "id": b.group.key,
                    "open_ended": b.open_ended,
                    "bin_start": b.start,
                    "bin_end": b.end,
                    "label": b.group.label,
                    **_datum(b.group, limit),
                }
                for b in dataset.bins
            ]
        case Points():
            return [
                {
                    "id": p.nct_id,
                    "nct_id": p.nct_id,
                    "title": p.title,
                    dataset.x.value: p.x,
                    dataset.y.value: p.y,
                    **({dataset.z.value: p.z} if dataset.z else {}),
                    **({dataset.color_field: p.color} if dataset.color_field else {}),
                    "citations": _citations(p.citations, max(limit, 3)),
                }
                for p in dataset.points
            ]
        case Graph():
            return _graph_rows(dataset, limit)
        case Flow():
            return {
                "nodes": [
                    {
                        "id": n.id,
                        "stage": n.stage,
                        "stage_label": dataset.stages[n.stage],
                        "label": n.label,
                        **_datum(n.group, limit),
                    }
                    for n in dataset.nodes
                ],
                "links": [
                    {
                        "id": f"{link.source}>{link.target}",
                        "source": link.source,
                        "target": link.target,
                        **_datum(link.group, limit),
                    }
                    for link in dataset.links
                ],
            }
        case Trials():
            return [
                {
                    "id": r.nct_id,
                    "nct_id": r.nct_id,
                    "title": r.title,
                    "phase": r.phase,
                    "status": r.status,
                    "sponsor": r.sponsor,
                    "start": r.start,
                    "end": r.end,
                    "enrollment": r.enrollment,
                    "url": f"https://clinicaltrials.gov/study/{r.nct_id}",
                    "citations": _citations(r.citations, 2),
                }
                for r in dataset.rows
            ]
    raise AssertionError(f"no rows for {type(dataset).__name__}")


def _as_boxes(dataset: Distribution, chart: ChartType) -> bool:
    return chart is ChartType.BOX_PLOT or (chart is ChartType.TABLE and bool(dataset.group_field))


def _box(dataset: Distribution, spread: Spread, limit: int) -> dict[str, Any]:
    """Tukey box statistics. Quartiles use the inclusive method, which stays within the
    observed values. The default exclusive method extrapolates past them for small groups,
    which can put a quartile outside its own whisker."""
    values = sorted(v.value for v in spread.values)
    q1, median, q3 = (
        statistics.quantiles(values, n=4, method="inclusive")
        if len(values) >= 2
        else (values[0],) * 3
    )
    reach = 1.5 * (q3 - q1)
    inside = [v for v in values if q1 - reach <= v <= q3 + reach] or values
    outliers = [
        {"nct_id": v.nct_id, "value": v.value}
        for v in spread.values
        if v.value < inside[0] or v.value > inside[-1]
    ]
    group = Group(spread.key, spread.label)
    for value in spread.values:
        group.add(value.nct_id, value.citations)
    return {
        "id": spread.key,
        (dataset.group_field or "group"): spread.label,
        "n": len(values),
        "min": values[0],
        "q1": q1,
        "median": median,
        "q3": q3,
        "max": values[-1],
        "whisker_low": inside[0],
        "whisker_high": inside[-1],
        "mean": round(statistics.fmean(values), 2),
        "small_sample": len(values) < 5,
        "outliers": outliers,
        **_datum(group, limit),
    }


def _graph_rows(dataset: Graph, limit: int) -> dict[str, list[dict[str, Any]]]:
    ids = [n.id for n in dataset.nodes]
    weighted = [(e.source, e.target, float(e.group.count)) for e in dataset.edges]
    flat = force_layout(ids, weighted, 2)
    deep = force_layout(ids, weighted, 3)
    return {
        "nodes": [
            {
                "id": n.id,
                "label": n.label,
                "type": n.type,
                "degree": n.degree,
                "community": n.community,
                "x": flat[n.id][0],
                "y": flat[n.id][1],
                "x3": deep[n.id][0],
                "y3": deep[n.id][1],
                "z3": deep[n.id][2],
                **_datum(n.group, limit),
            }
            for n in dataset.nodes
        ],
        "edges": [
            {"id": f"{e.source}|{e.target}", "source": e.source, "target": e.target}
            | _datum(e.group, limit)
            for e in dataset.edges
        ],
    }


# --- Encodings -----------------------------------------------------------------------------


def _count(stack: Literal["zero", "normalize"] | None = None) -> Channel:
    title = "Share of trials" if stack == "normalize" else "Trials"
    return Channel(field=COUNT, type="quantitative", title=title, stack=stack)


def _measure(measure: Measure, values: list[float]) -> Channel:
    is_skewed = skewed(values)
    return Channel(
        field=measure.value,
        type="quantitative",
        title=f"{measure.label} ({measure.unit})",
        scale="symlog" if is_skewed else "linear",
    )


def encoding_and_options(
    chart: ChartType, dataset: Dataset, data: Rows
) -> tuple[dict[str, Channel], dict[str, Any]]:
    match dataset:
        case Scalar():
            return {"value": _count()}, {}
        case Categorical():
            natural = [r[dataset.field] for r in data] if isinstance(data, list) else None
            category = Channel(
                field=dataset.field,
                type="ordinal" if dataset.ordered else "nominal",
                title=dataset.dimension,
                sort=natural if dataset.ordered else "descending",
            )
            return _categorical(chart, category, dataset, data)
        case Matrix():
            row = Channel(
                field=dataset.row_field,
                type="ordinal" if dataset.ordered_rows else "nominal",
                title=dataset.row_dimension,
                sort=[label for _, label in dataset.rows],
            )
            column = Channel(
                field=dataset.column_field,
                type="ordinal" if dataset.ordered_columns else "nominal",
                title=dataset.column_dimension,
                sort=[label for _, label in dataset.columns],
            )
            return _matrix(chart, row, column)
        case TimeSeries():
            time = Channel(
                field="period",
                type="temporal"
                if chart in (ChartType.TIME_SERIES, ChartType.STACKED_AREA_CHART)
                else "ordinal",
                title=f"{dataset.time_label} {_unit(dataset.granularity)}",
            )
            series = (
                Channel(
                    field=dataset.series_field,
                    type="nominal",
                    title=dataset.series_dimension or "",
                    sort=[label for _, label in dataset.series],
                )
                if dataset.series_field
                else None
            )
            return _time(chart, time, series)
        case Distribution():
            if chart is ChartType.TABLE:
                # A grouped spread's table gives each group's statistics; otherwise the bins.
                stats = ["n", "min", "q1", "median", "q3", "max", "mean"]
                columns = (
                    [dataset.group_field or "group", *stats]
                    if _as_boxes(dataset, chart)
                    else ["label", COUNT]
                )
                return {}, {"columns": columns}
            if chart is ChartType.BOX_PLOT:
                group = Channel(
                    field=dataset.group_field or "group",
                    type="nominal",
                    title=dataset.group_dimension or "",
                    # Groups keep the dataset's order, so phases run Early Phase 1 to Phase 4.
                    sort=[spread.label for spread in dataset.spreads],
                )
                unit = f"{dataset.measure.label} ({dataset.measure.unit})"
                return (
                    {
                        "x": group,
                        **{
                            name: Channel(
                                field=name,
                                type="quantitative",
                                title=unit,
                                scale="symlog" if dataset.skewed else "linear",
                            )
                            for name in ("whisker_low", "q1", "median", "q3", "whisker_high")
                        },
                    },
                    {"outliers_field": "outliers"},
                )
            unit = f"{dataset.measure.label} ({dataset.measure.unit})"
            return (
                {
                    "x": Channel(
                        field="bin_start",
                        type="quantitative",
                        title=unit,
                        scale="symlog" if dataset.skewed else "linear",
                    ),
                    "x2": Channel(field="bin_end", type="quantitative", title=unit),
                    "y": _count(),
                },
                {"skewed": dataset.skewed, "bins_precomputed": True},
            )
        case Points():
            if chart is ChartType.TABLE:
                measures = [m.value for m in (dataset.x, dataset.y, dataset.z) if m]
                color = [dataset.color_field] if dataset.color_field else []
                return {}, {"columns": ["nct_id", "title", *measures, *color]}
            xs, ys = [p.x for p in dataset.points], [p.y for p in dataset.points]
            encoding = {"x": _measure(dataset.x, xs), "y": _measure(dataset.y, ys)}
            # A third measure is bubble size or the 3D depth. A flat scatter has no place
            # for it, so it carries none, and its bubble alternative shows the third measure.
            third = {ChartType.BUBBLE_CHART: "size", ChartType.SCATTER_PLOT_3D: "z"}.get(chart)
            if dataset.z and third:
                zs = [p.z for p in dataset.points if p.z is not None]
                encoding[third] = _measure(dataset.z, zs)
            if dataset.color_field:
                encoding["color"] = Channel(
                    field=dataset.color_field, type="nominal", title=dataset.color_dimension or ""
                )
            encoding["tooltip"] = Channel(field="title", type="nominal", title="Trial")
            return encoding, {}
        case Graph():
            three = chart is ChartType.NETWORK_GRAPH_3D
            encoding = {
                "node_id": Channel(field="id", type="nominal", title="Entity"),
                "node_label": Channel(field="label", type="nominal", title="Name"),
                "node_size": _count(),
                "node_color": Channel(field="community", type="nominal", title="Cluster"),
                "edge_source": Channel(field="source", type="nominal", title="From"),
                "edge_target": Channel(field="target", type="nominal", title="To"),
                "edge_width": Channel(field=COUNT, type="quantitative", title="Shared trials"),
                "x": Channel(field="x3" if three else "x", type="quantitative", title="x"),
                "y": Channel(field="y3" if three else "y", type="quantitative", title="y"),
            }
            if three:
                encoding["z"] = Channel(field="z3", type="quantitative", title="z")
            return encoding, {
                "relation": dataset.relation,
                "node_types": list(dataset.node_types),
                "positions": "precomputed force layout scaled to -1..1, optional to use",
            }
        case Flow():
            return (
                {
                    "source": Channel(field="source", type="nominal", title="From"),
                    "target": Channel(field="target", type="nominal", title="To"),
                    "value": _count(),
                    "node_label": Channel(field="label", type="nominal", title="Stage value"),
                },
                {"stages": dataset.stages},
            )
        case Trials():
            return (
                {
                    "x": Channel(field="start", type="temporal", title="Start"),
                    "x2": Channel(field="end", type="temporal", title="Completion"),
                    "y": Channel(field="title", type="nominal", title="Trial"),
                    "color": Channel(field="phase", type="nominal", title="Phase"),
                },
                {
                    "columns": [
                        "nct_id",
                        "title",
                        "phase",
                        "status",
                        "sponsor",
                        "start",
                        "end",
                        "enrollment",
                    ]
                },
            )
    raise AssertionError(f"no encoding for {type(dataset).__name__}")


def _unit(granularity: TimeGranularity) -> str:
    return "month" if granularity is TimeGranularity.MONTH else "year"


def _categorical(
    chart: ChartType, category: Channel, dataset: Categorical, data: Rows
) -> tuple[dict[str, Channel], dict[str, Any]]:
    many = len(dataset.groups) > DEFAULT_DISPLAY_LIMIT
    limit = {"display_limit": DEFAULT_DISPLAY_LIMIT} if many else {}
    match chart:
        case ChartType.BAR_CHART:
            return {"x": category, "y": _count()}, {} if dataset.ordered else limit
        case ChartType.HORIZONTAL_BAR_CHART:
            return {"y": category, "x": _count()}, limit
        case ChartType.DONUT_CHART:
            return {"theta": _count(), "color": category}, {}
        case ChartType.TREEMAP:
            return {"size": _count(), "label": category}, {}
        case ChartType.CHOROPLETH_MAP:
            return (
                {
                    "geo": Channel(field="iso_numeric", type="nominal", title="Country code"),
                    "color": _count(),
                    "label": category,
                },
                {"topology": WORLD_TOPOLOGY, "feature": "countries", "id_matches": "iso_numeric"},
            )
    return {}, {"columns": [category.field, COUNT]}


def _matrix(
    chart: ChartType, row: Channel, column: Channel
) -> tuple[dict[str, Channel], dict[str, Any]]:
    match chart:
        case ChartType.GROUPED_BAR_CHART:
            # The dimension with fewer values becomes the bars within each group.
            fewer_rows = len(row.sort or []) <= len(column.sort or [])
            axis, series = (column, row) if fewer_rows else (row, column)
            return {"x": axis, "y": _count(), "color": series, "x_offset": series}, {}
        case ChartType.STACKED_BAR_CHART:
            return {"x": row, "y": _count("zero"), "color": column}, {}
        case ChartType.NORMALIZED_STACKED_BAR_CHART:
            return {"x": row, "y": _count("normalize"), "color": column}, {}
        case ChartType.HEATMAP:
            return {"x": column, "y": row, "color": _count()}, {}
    return {}, {"columns": [row.field, column.field, COUNT]}


def _time(
    chart: ChartType, time: Channel, series: Channel | None
) -> tuple[dict[str, Channel], dict[str, Any]]:
    color = {"color": series} if series else {}
    match chart:
        case ChartType.TIME_SERIES:
            return {"x": time, "y": _count(), **color}, {}
        case ChartType.STACKED_AREA_CHART:
            return {"x": time, "y": _count("zero"), **color}, {}
        case ChartType.BAR_CHART:
            return {"x": time, "y": _count()}, {}
        case ChartType.GROUPED_BAR_CHART:
            assert series is not None
            return {"x": time, "y": _count(), "color": series, "x_offset": series}, {}
        case ChartType.HEATMAP:
            assert series is not None
            return {"x": time, "y": series, "color": _count()}, {}
    return {}, {"columns": ["period", *([series.field] if series else []), COUNT]}
