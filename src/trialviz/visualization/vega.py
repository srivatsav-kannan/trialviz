"""Vega-Lite specs generated from our own, for every chart Vega-Lite can draw.

The translation is mechanical: it reads the view's encoding and makes no decisions of its
own. Citations and trial ID lists are left out of the embedded data to keep the spec
small. They stay in the response's rows, keyed the same way, for tooltips and drill-down.
Treemaps, 3D charts, networks and Sankey diagrams have no Vega-Lite form and get None.
"""

from __future__ import annotations

from typing import Any

from trialviz.visualization.spec import COUNT, Channel, Rows
from trialviz.vocabulary import ChartType

SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
_HEAVY = {"citations", "nct_ids", "outliers"}
# 1000 as "1k". Vega's "~s" format picks one prefix for the whole axis, so 5 becomes "0k".
_SHORT_NUMBER = "datum.value >= 1000 ? format(datum.value / 1000, '~') + 'k' : datum.value"


def _channel(channel: Channel, **extra: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {"field": channel.field, "type": channel.type, "title": channel.title}
    if isinstance(channel.sort, list):
        spec["sort"] = channel.sort
    if channel.scale == "symlog":
        spec["scale"] = {"type": "symlog"}
    if channel.stack:
        spec["stack"] = channel.stack
    return spec | extra


def _tooltip(*channels: Channel) -> list[dict[str, Any]]:
    return [{"field": c.field, "type": c.type, "title": c.title} for c in channels]


def to_vega_lite(
    chart: ChartType, encoding: dict[str, Channel], data: Rows, title: str, options: dict[str, Any]
) -> dict[str, Any] | None:
    if not isinstance(data, list):
        return None
    values = [{k: v for k, v in row.items() if k not in _HEAVY} for row in data]
    if chart is ChartType.CHOROPLETH_MAP:
        return _choropleth(encoding, values, title, options)
    body = _body(chart, encoding, options)
    if body is None:
        return None
    spec: dict[str, Any] = {"$schema": SCHEMA, "title": title, "data": {"values": values}, **body}
    _label_symlog_axes(spec, values)
    _read_dates_as_utc(spec, values)
    if limit := options.get("display_limit"):
        sort_field = (
            encoding.get("x") if chart is ChartType.HORIZONTAL_BAR_CHART else encoding.get("y")
        )
        field = sort_field.field if sort_field else COUNT
        spec["transform"] = [
            {
                "window": [{"op": "rank", "as": "_rank"}],
                "sort": [{"field": field, "order": "descending"}],
            },
            {"filter": f"datum._rank <= {limit}"},
        ]
    return spec


def _label_symlog_axes(spec: dict[str, Any], values: list[dict[str, Any]]) -> None:
    """Vega's default ticks on a symlog axis are linear, so they bunch at the top and leave
    the small values unlabeled. Label 0, 1, 2, 5, 10, 20, 50 and so on instead. Every layer
    drawing on the same axis gets the same ticks, covering the largest value any of them
    shows."""
    axes: dict[str, list[dict[str, Any]]] = {}
    for encoding in _encodings(spec):
        for name, channel in encoding.items():
            if isinstance(channel, dict) and channel.get("scale") == {"type": "symlog"}:
                axes.setdefault(name, []).append(channel)
    for name, channels in axes.items():
        # The box plot's whiskers end at y2, which shares the y axis.
        fields = {c["field"] for c in channels} | {
            e[f"{name}2"]["field"] for e in _encodings(spec) if f"{name}2" in e
        }
        top = max(
            (row[f] for row in values for f in fields if isinstance(row.get(f), int | float)),
            default=0,
        )
        for channel in channels:
            channel["axis"] = {
                "values": _nice_ticks(top),
                "labelExpr": _SHORT_NUMBER,
                "labelOverlap": "greedy",
            }


_UTC_UNITS = {4: "utcyear", 7: "utcyearmonth", 10: "utcyearmonthdate"}


def _read_dates_as_utc(spec: dict[str, Any], values: list[dict[str, Any]]) -> None:
    """Periods and dates ("2019", "2019-03", "2019-03-01") are parsed as UTC midnight, so
    shown in local time they fall on the day or year before anywhere west of UTC. A UTC
    time unit keeps the axis, ticks and tooltips on the date the data says."""
    for encoding in _encodings(spec):
        channels = [c for c in encoding.values() if isinstance(c, dict)]
        channels += [t for t in encoding.get("tooltip", []) if isinstance(t, dict)]
        for channel in channels:
            if channel.get("type") != "temporal":
                continue
            # Timelines mix "2024-06-03" with "2030-12", so the finest precision present wins.
            lengths = {
                len(str(row[channel["field"]])) for row in values if row.get(channel["field"])
            }
            if lengths and (unit := _UTC_UNITS.get(max(lengths))):
                channel["timeUnit"] = unit


def _encodings(spec: dict[str, Any]) -> list[dict[str, Any]]:
    found = [spec["encoding"]] if "encoding" in spec else []
    for layer in spec.get("layer", []):
        found += _encodings(layer)
    return found


def _nice_ticks(top: float) -> list[int]:
    ticks, step = [0], 1
    while step <= top:
        ticks += [m * step for m in (1, 2, 5) if m * step <= top]
        step *= 10
    return ticks


def _choropleth(
    e: dict[str, Channel], values: list[dict[str, Any]], title: str, options: dict[str, Any]
) -> dict[str, Any]:
    """World countries from TopoJSON, colored by joining each country's numeric ISO code to
    the rows. Countries with no trials stay grey."""
    topology = {"url": options["topology"], "format": {"type": "topojson", "feature": "countries"}}
    lookup = {"data": {"values": values}, "key": "iso_numeric", "fields": [COUNT, e["label"].field]}
    return {
        "$schema": SCHEMA,
        "title": title,
        "projection": {"type": "equalEarth"},
        "layer": [
            {"data": topology, "mark": {"type": "geoshape", "fill": "#e8e8e8", "stroke": "white"}},
            {
                "data": topology,
                "transform": [
                    {"lookup": "id", "from": lookup},
                    {"filter": f"isValid(datum.{COUNT})"},
                ],
                "mark": {"type": "geoshape", "stroke": "white"},
                "encoding": {
                    "color": _channel(e["color"]),
                    "tooltip": _tooltip(e["label"], e["color"]),
                },
            },
        ],
    }


def _body(
    chart: ChartType, e: dict[str, Channel], options: dict[str, Any]
) -> dict[str, Any] | None:
    match chart:
        case ChartType.BAR_CHART:
            return {
                "mark": "bar",
                "encoding": {
                    "x": _channel(e["x"]),
                    "y": _channel(e["y"]),
                    "tooltip": _tooltip(e["x"], e["y"]),
                },
            }
        case ChartType.HORIZONTAL_BAR_CHART:
            return {
                "mark": "bar",
                "encoding": {
                    "y": _channel(e["y"], sort="-x"),
                    "x": _channel(e["x"]),
                    "tooltip": _tooltip(e["y"], e["x"]),
                },
            }
        case ChartType.DONUT_CHART:
            return {
                "mark": {"type": "arc", "innerRadius": 60},
                "encoding": {
                    "theta": _channel(e["theta"]),
                    "color": _channel(e["color"]),
                    "tooltip": _tooltip(e["color"], e["theta"]),
                },
            }
        case ChartType.GROUPED_BAR_CHART:
            return {
                "mark": "bar",
                "encoding": {
                    "x": _channel(e["x"]),
                    "y": _channel(e["y"]),
                    "color": _channel(e["color"]),
                    "xOffset": _channel(e["x_offset"]),
                    "tooltip": _tooltip(e["x"], e["color"], e["y"]),
                },
            }
        case ChartType.STACKED_BAR_CHART | ChartType.NORMALIZED_STACKED_BAR_CHART:
            return {
                "mark": "bar",
                "encoding": {
                    "x": _channel(e["x"]),
                    "y": _channel(e["y"]),
                    "color": _channel(e["color"]),
                    "tooltip": _tooltip(e["x"], e["color"], e["y"]),
                },
            }
        case ChartType.HEATMAP:
            return {
                "mark": "rect",
                "encoding": {
                    "x": _channel(e["x"]),
                    "y": _channel(e["y"]),
                    "color": _channel(e["color"]),
                    "tooltip": _tooltip(e["x"], e["y"], e["color"]),
                },
            }
        case ChartType.TIME_SERIES:
            enc = {
                "x": _channel(e["x"]),
                "y": _channel(e["y"]),
                "tooltip": _tooltip(e["x"], e["y"]),
            }
            if "color" in e:
                enc["color"] = _channel(e["color"])
            return {"mark": {"type": "line", "point": True}, "encoding": enc}
        case ChartType.STACKED_AREA_CHART:
            enc = {
                "x": _channel(e["x"]),
                "y": _channel(e["y"]),
                "tooltip": _tooltip(e["x"], e["y"]),
            }
            if "color" in e:
                enc["color"] = _channel(e["color"])
            return {"mark": "area", "encoding": enc}
        case ChartType.HISTOGRAM:
            return {
                "mark": "bar",
                "encoding": {
                    "x": _channel(e["x"], bin={"binned": True}),
                    "x2": {"field": e["x2"].field},
                    "y": _channel(e["y"]),
                    "tooltip": _tooltip(e["x"], e["x2"], e["y"]),
                },
            }
        case ChartType.BOX_PLOT:
            x = _channel(e["x"])
            return {
                "layer": [
                    {
                        "mark": "rule",
                        "encoding": {
                            "x": x,
                            "y": _channel(e["whisker_low"]),
                            "y2": {"field": "whisker_high"},
                        },
                    },
                    {
                        "mark": {"type": "bar", "size": 18},
                        "encoding": {
                            "x": x,
                            "y": _channel(e["q1"]),
                            "y2": {"field": "q3"},
                            "tooltip": [
                                {"field": e["x"].field, "type": e["x"].type, "title": e["x"].title},
                                {"field": "n", "type": "quantitative", "title": "Trials"},
                                *(
                                    {
                                        "field": f,
                                        "type": "quantitative",
                                        "title": f.replace("_", " "),
                                    }
                                    for f in ("whisker_low", "q1", "median", "q3", "whisker_high")
                                ),
                            ],
                        },
                    },
                    {
                        "mark": {"type": "tick", "color": "white", "size": 18},
                        "encoding": {"x": x, "y": _channel(e["median"])},
                    },
                ]
            }
        case ChartType.SCATTER_PLOT | ChartType.BUBBLE_CHART:
            enc = {
                "x": _channel(e["x"]),
                "y": _channel(e["y"]),
                "tooltip": _tooltip(e["tooltip"], e["x"], e["y"]),
            }
            if "color" in e:
                enc["color"] = _channel(e["color"])
            if "size" in e:
                enc["size"] = _channel(e["size"])
            return {"mark": {"type": "circle", "opacity": 0.7}, "encoding": enc}
        case ChartType.TIMELINE:
            return {
                "mark": "bar",
                "encoding": {
                    "x": _channel(e["x"]),
                    "x2": {"field": e["x2"].field},
                    "y": _channel(e["y"], sort={"field": "start"}),
                    "color": _channel(e["color"]),
                    "tooltip": _tooltip(e["y"], e["x"], e["x2"], e["color"]),
                },
            }
    return None
