"""The shapes aggregated data comes in. Charts are chosen per shape, not per question.

Every count-like datum is a `Group`: the trials that make it up, the citations showing why
each trial is in the group (both ends of a link, both values of a grid cell), and, when
only part of a large result set was fetched, an exact count taken from the registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trialviz.aggregation.evidence import Citation
from trialviz.vocabulary import Measure, TimeGranularity


@dataclass
class Group:
    key: str
    label: str
    nct_ids: list[str] = field(default_factory=list)
    evidence: dict[str, tuple[Citation, ...]] = field(default_factory=dict)
    """Per trial, every citation that places it in this group."""
    exact_count: int | None = None
    """Set when the fetched records were a subset and the registry was asked directly."""

    @property
    def count(self) -> int:
        return self.exact_count if self.exact_count is not None else len(self.nct_ids)

    def add(self, nct_id: str, citations: tuple[Citation, ...]) -> None:
        """Count a trial once, however many of its values map to this group."""
        if nct_id in self.evidence:
            return
        self.nct_ids.append(nct_id)
        self.evidence[nct_id] = citations

    def citations_for(self, trials: int) -> list[Citation]:
        """Every citation for the first `trials` trials, so each cited trial is fully shown."""
        return [c for nct_id in self.nct_ids[:trials] for c in self.evidence[nct_id]]


@dataclass
class Scalar:
    label: str
    total: int
    citations: list[Citation]


@dataclass
class Categorical:
    dimension: str
    """The grouping's display name, such as "Country" or "Cohort"."""
    field: str
    """The row field name, such as "country"."""
    groups: list[Group]
    ordered: bool
    """Whether the groups have an inherent order (phases, years) that charts should keep."""
    geographic: bool = False


@dataclass
class Matrix:
    row_dimension: str
    row_field: str
    column_dimension: str
    column_field: str
    rows: list[tuple[str, str]]
    columns: list[tuple[str, str]]
    cells: dict[tuple[str, str], Group]
    ordered_rows: bool
    ordered_columns: bool


@dataclass
class TimeSeries:
    granularity: TimeGranularity
    time_label: str
    periods: list[str]
    series_dimension: str | None
    series_field: str | None
    series: list[tuple[str, str]]
    cells: dict[tuple[str, str], Group]
    """Keyed by (period, series key). Without a series, the series key is ""."""


@dataclass
class Value:
    nct_id: str
    value: float
    citations: tuple[Citation, ...]


@dataclass
class Spread:
    key: str
    label: str
    values: list[Value]


@dataclass
class Bin:
    start: float
    end: float
    group: Group
    open_ended: bool = False
    """The last bin also holds every larger value. Its end is where it would close at the
    normal width, so one huge trial does not stretch the axis."""


@dataclass
class Distribution:
    measure: Measure
    group_dimension: str | None
    group_field: str | None
    spreads: list[Spread]
    bins: list[Bin]
    excluded: int
    """Trials with no usable value for the measure."""
    skewed: bool
    """True when the largest value dwarfs the median, so a log scale reads better."""


@dataclass
class Point:
    nct_id: str
    title: str
    x: float
    y: float
    z: float | None
    color: str | None
    citations: tuple[Citation, ...]


@dataclass
class Points:
    x: Measure
    y: Measure
    z: Measure | None
    color_dimension: str | None
    color_field: str | None
    points: list[Point]
    excluded: int


@dataclass
class Node:
    id: str
    label: str
    type: str
    group: Group
    degree: int = 0
    community: int = 0


@dataclass
class Edge:
    source: str
    target: str
    group: Group


@dataclass
class Graph:
    node_types: tuple[str, str]
    relation: str
    """How edges were formed, in words, for the response."""
    nodes: list[Node]
    edges: list[Edge]
    dropped_nodes: int
    dropped_edges: int


@dataclass
class FlowNode:
    id: str
    stage: int
    label: str
    group: Group


@dataclass
class FlowLink:
    source: str
    target: str
    group: Group


@dataclass
class Flow:
    stages: list[str]
    nodes: list[FlowNode]
    links: list[FlowLink]


@dataclass
class TrialRow:
    nct_id: str
    title: str
    phase: str
    status: str
    sponsor: str
    start: str | None
    end: str | None
    enrollment: float | None
    citations: tuple[Citation, ...]


@dataclass
class Trials:
    rows: list[TrialRow]
    undated: int


Dataset = Scalar | Categorical | Matrix | TimeSeries | Distribution | Points | Graph | Flow | Trials
