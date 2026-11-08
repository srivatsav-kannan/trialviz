"""An answer's rows as CSV, for a spreadsheet or another tool.

The CSV holds the recommended chart's rows with every field except citations, which do not
fit a cell; lists such as `nct_ids` are joined with semicolons. A network or Sankey diagram
has two tables, so `part` picks its links (the default, labeled with both ends) or its nodes.
Text that a spreadsheet would run as a formula is prefixed with an apostrophe, since trial
titles and sponsor names are free text written by anyone.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Literal

Part = Literal["links", "nodes"]

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def labelled_links(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """A network's edges or a Sankey diagram's links, each with the labels of both ends,
    which is how a link reads outside the chart (a CSV row, an agent's answer)."""
    labels = {node["id"]: node["label"] for node in data["nodes"]}
    return [
        {
            "source_label": labels.get(link["source"], link["source"]),
            "target_label": labels.get(link["target"], link["target"]),
            **link,
        }
        for link in data.get("edges") or data.get("links") or []
    ]


def to_csv(answer: dict[str, Any], part: Part = "links") -> str:
    data = answer["visualization"]["data"]
    if isinstance(data, dict):
        rows = data["nodes"] if part == "nodes" else labelled_links(data)
    else:
        rows = data
    columns = list(dict.fromkeys(key for row in rows for key in row if key != "citations"))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell(row.get(key)) for key in columns})
    return out.getvalue()


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ";".join(
            f"{item['nct_id']}:{item['value']:g}" if isinstance(item, dict) else str(item)
            for item in value
        )
    text = str(value)
    if isinstance(value, str) and text.startswith(_FORMULA_START):
        return f"'{text}"
    return text
