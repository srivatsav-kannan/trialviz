"""Choosing the chart: one recommendation, its alternatives, and why each was or was not used.

The choice is made by rules, never by a model, so the same data always gets the same
chart and the response can say why:

1. The dataset's shape decides which charts can draw it at all.
2. Each candidate checks facts about the actual data and either fits or gives the reason
   it does not ("a donut needs 5 or fewer parts that make up a whole").
3. Fitting candidates are scored, and the highest score is recommended. Scores encode
   simple, documented preferences, such as lengths before colors for ranking, lines for
   change over time, and grouped bars only while groups stay readable.
4. A chart the caller asked for (`presentation.chart_type`) wins whenever it fits. When it
   does not, the recommendation stands and a note says why the request was not used.

Every fitting candidate other than the recommendation is offered as an alternative over
the same data, so a frontend can let people switch views without another request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from trialviz.aggregation.datasets import (
    Categorical,
    Dataset,
    Distribution,
    Flow,
    Graph,
    Matrix,
    Points,
    Scalar,
    TimeSeries,
    Trials,
)
from trialviz.query.plan import Presentation
from trialviz.vocabulary import ChartType, SortOrder

TIMELINE_LIMIT = 60
"""Most trials a timeline shows before a table reads better."""

MIN_BOX_VALUES = 5
"""Fewest values for a box to mean something. Smaller groups are still drawn, flagged."""


@dataclass(frozen=True)
class Facts:
    """What the rules know about the data. Computed once from the dataset."""

    shape: str
    groups: int = 0
    rows: int = 0
    columns: int = 0
    periods: int = 0
    series: int = 0
    ordered: bool = False
    geographic: bool = False
    parts_of_whole: bool = False
    """Every trial is in exactly one group, so the groups are shares of one total."""
    long_labels: bool = False
    ranking: bool = False
    """The question asks for an order (top N, most, sorted by size)."""
    uneven_rows: bool = False
    """Row totals differ by more than 2 to 1, so shares compare better than counts."""
    values: int = 0
    spreads: int = 0
    boxable_spreads: int = 0
    """Groups with enough values (5 or more) to draw a meaningful box."""
    points: int = 0
    has_z: bool = False
    nodes: int = 0
    edges: int = 0
    links: int = 0
    dated_trials: int = 0


@dataclass(frozen=True)
class Candidate:
    chart: ChartType
    shape: str
    unfit: Callable[[Facts], str | None]
    """Returns why the chart cannot draw this data, or None when it can."""
    score: Callable[[Facts], int]
    why: Callable[[Facts], str]
    """Why the chart suits the data, said to the caller."""


@dataclass(frozen=True)
class Choice:
    chart: ChartType
    reason: str


@dataclass
class Recommendation:
    recommended: Choice
    alternatives: list[Choice]
    ruled_out: list[Choice]
    preference_note: str | None = None


def _never(_: Facts) -> str | None:
    return None


def _unless(condition: Callable[[Facts], bool], reason: str) -> Callable[[Facts], str | None]:
    return lambda f: None if condition(f) else reason


def _all(*checks: Callable[[Facts], str | None]) -> Callable[[Facts], str | None]:
    def check(f: Facts) -> str | None:
        return next((r for c in checks if (r := c(f)) is not None), None)

    return check


CANDIDATES: list[Candidate] = [
    Candidate(ChartType.STAT, "scalar", _never, lambda f: 5, lambda f: "the answer is one number"),
    # Counts per category.
    Candidate(
        ChartType.BAR_CHART,
        "categorical",
        _unless(lambda f: f.groups <= 30, "more than 30 bars cannot be read side by side"),
        lambda f: 3 + 2 * f.ordered - 2 * f.long_labels,
        lambda f: (
            "the groups have a natural order, kept left to right"
            if f.ordered
            else "bars compare counts precisely"
        ),
    ),
    Candidate(
        ChartType.HORIZONTAL_BAR_CHART,
        "categorical",
        _never,
        lambda f: 3 + 2 * (f.ranking or f.long_labels),
        lambda f: (
            "a ranking reads best as bars sorted by length, with room for long names"
            if f.ranking or f.long_labels
            else "bars compare counts precisely"
        ),
    ),
    Candidate(
        ChartType.DONUT_CHART,
        "categorical",
        _all(
            _unless(
                lambda f: f.parts_of_whole,
                "a trial can be in several of these groups, while a donut needs shares of "
                "one whole",
            ),
            _unless(lambda f: 2 <= f.groups <= 5, "a donut stays readable only with 2 to 5 parts"),
        ),
        lambda f: 2,
        lambda f: "a few groups that together make up every trial",
    ),
    Candidate(
        ChartType.TREEMAP,
        "categorical",
        _unless(lambda f: f.groups >= 6, "a treemap adds nothing over bars for 5 or fewer groups"),
        lambda f: 1,
        lambda f: "many groups at once, sized by count",
    ),
    Candidate(
        ChartType.CHOROPLETH_MAP,
        "categorical",
        _unless(lambda f: f.geographic, "a map needs groups that are countries"),
        lambda f: 2 + 3 * (f.groups >= 15 and not f.ranking),
        lambda f: "the groups are countries, so a map shows where trials are",
    ),
    Candidate(
        ChartType.TABLE,
        "categorical",
        _never,
        lambda f: 0,
        lambda f: "exact values for every group",
    ),
    # Counts over two categories.
    Candidate(
        ChartType.GROUPED_BAR_CHART,
        "matrix",
        _unless(
            lambda f: min(f.rows, f.columns) <= 5 and f.rows * f.columns <= 60,
            "grouped bars stay readable only with 5 or fewer bars per group",
        ),
        lambda f: 4 - 3 * f.uneven_rows,
        lambda f: "a few bars per group, compared side by side",
    ),
    Candidate(
        ChartType.STACKED_BAR_CHART,
        "matrix",
        _all(
            _unless(
                lambda f: f.parts_of_whole,
                "a trial can be in several of these groups, while stacking needs each trial "
                "in one segment",
            ),
            _unless(
                lambda f: f.columns <= 10, "more than 10 segments per bar cannot be told apart"
            ),
        ),
        lambda f: 3,
        lambda f: "each bar's total and its makeup at once",
    ),
    Candidate(
        ChartType.NORMALIZED_STACKED_BAR_CHART,
        "matrix",
        _all(
            _unless(
                lambda f: f.parts_of_whole,
                "a trial can be in several of these groups, while shares need each trial in "
                "one segment",
            ),
            _unless(
                lambda f: f.columns <= 10, "more than 10 segments per bar cannot be told apart"
            ),
        ),
        lambda f: 3 + 3 * f.uneven_rows,
        lambda f: (
            "the groups differ a lot in size, so shares compare more fairly than counts"
            if f.uneven_rows
            else "each group's makeup as shares"
        ),
    ),
    Candidate(
        ChartType.HEATMAP,
        "matrix",
        _unless(
            lambda f: f.rows >= 2 and f.columns >= 2,
            "a heatmap needs at least 2 rows and 2 columns",
        ),
        lambda f: 2 + 3 * (min(f.rows, f.columns) > 5 or f.rows * f.columns > 60),
        lambda f: "too many combinations for bars, so color shows the counts",
    ),
    Candidate(
        ChartType.TABLE,
        "matrix",
        _never,
        lambda f: 0,
        lambda f: "exact values for every combination",
    ),
    # Counts over time.
    Candidate(
        ChartType.TIME_SERIES,
        "time",
        _all(
            _unless(lambda f: f.periods >= 3, "a line needs at least 3 periods to show change"),
            _unless(lambda f: f.series <= 8, "more than 8 lines cannot be told apart"),
        ),
        lambda f: 5,
        lambda f: "change over time reads best as a line",
    ),
    Candidate(
        ChartType.STACKED_AREA_CHART,
        "time",
        _all(
            _unless(lambda f: f.series >= 2, "a stacked area needs at least 2 series"),
            _unless(
                lambda f: f.parts_of_whole,
                "a trial can be in several series, while stacking needs each trial in one",
            ),
            _unless(lambda f: f.series <= 12, "more than 12 bands cannot be told apart"),
            _unless(lambda f: f.periods >= 3, "an area needs at least 3 periods"),
        ),
        lambda f: 3,
        lambda f: "the total over time and how it splits",
    ),
    Candidate(
        ChartType.BAR_CHART,
        "time",
        _all(
            _unless(lambda f: f.series <= 1, "use grouped bars for several series"),
            _unless(lambda f: f.periods <= 30, "more than 30 periods read better as a line"),
        ),
        lambda f: 3 + 3 * (f.periods < 3),
        lambda f: "too few periods for a line, so bars" if f.periods < 3 else "one bar per period",
    ),
    Candidate(
        ChartType.GROUPED_BAR_CHART,
        "time",
        _all(
            _unless(lambda f: f.series >= 2, "grouped bars need at least 2 series"),
            _unless(
                lambda f: f.periods <= 12 and f.series <= 4,
                "grouped bars stay readable only up to 12 periods and 4 series",
            ),
        ),
        lambda f: 3 + 3 * (f.periods < 3),
        lambda f: "few periods, each split side by side",
    ),
    Candidate(
        ChartType.HEATMAP,
        "time",
        _unless(lambda f: f.series >= 2, "a heatmap needs at least 2 series"),
        lambda f: 2 + 4 * (f.series > 8),
        lambda f: "too many series for lines, so color shows each period's count",
    ),
    Candidate(
        ChartType.TABLE, "time", _never, lambda f: 0, lambda f: "exact values for every period"
    ),
    # Numeric values.
    Candidate(
        ChartType.HISTOGRAM,
        "distribution",
        _unless(lambda f: f.values >= 10, "a histogram needs at least 10 values"),
        lambda f: 4 + (f.spreads <= 1),
        lambda f: "the shape of the spread across all trials",
    ),
    Candidate(
        ChartType.BOX_PLOT,
        "distribution",
        _all(
            _unless(lambda f: f.spreads >= 2, "a box plot compares at least 2 groups"),
            _unless(
                lambda f: f.boxable_spreads >= 2, "at least 2 groups need 5 or more values each"
            ),
        ),
        lambda f: 3 + 3 * (f.spreads >= 2),
        lambda f: "the spread compared across groups",
    ),
    Candidate(
        ChartType.TABLE, "distribution", _never, lambda f: 0, lambda f: "exact values for every bin"
    ),
    # Points.
    Candidate(
        ChartType.SCATTER_PLOT,
        "points",
        _unless(lambda f: f.points >= 5, "a scatter plot needs at least 5 trials with both values"),
        lambda f: 4,
        lambda f: "one point per trial shows how the two measures relate",
    ),
    Candidate(
        ChartType.BUBBLE_CHART,
        "points",
        _all(
            _unless(lambda f: f.has_z, "a bubble chart needs a third measure for size"),
            _unless(lambda f: f.points >= 5, "a bubble chart needs at least 5 trials"),
        ),
        lambda f: 5,
        lambda f: "the third measure as point size, keeping the chart flat",
    ),
    Candidate(
        ChartType.SCATTER_PLOT_3D,
        "points",
        _all(
            _unless(lambda f: f.has_z, "a 3D scatter needs a third measure"),
            _unless(lambda f: f.points >= 5, "a 3D scatter needs at least 5 trials"),
        ),
        lambda f: 3,
        lambda f: "all three measures as positions, explored by rotating",
    ),
    Candidate(
        ChartType.TABLE, "points", _never, lambda f: 0, lambda f: "exact values for every trial"
    ),
    # Networks.
    Candidate(
        ChartType.NETWORK_GRAPH,
        "graph",
        _unless(lambda f: f.edges >= 1, "no two entities share a trial, so there are no links"),
        lambda f: 5 - 3 * (f.nodes > 80),
        lambda f: "entities as nodes, linked by the trials they share",
    ),
    Candidate(
        ChartType.NETWORK_GRAPH_3D,
        "graph",
        _unless(lambda f: f.edges >= 1, "no two entities share a trial, so there are no links"),
        lambda f: 3 + 3 * (f.nodes > 80),
        lambda f: "a dense network spread out in depth, explored by rotating",
    ),
    Candidate(
        ChartType.TABLE, "graph", _never, lambda f: 0, lambda f: "every link with its trial count"
    ),
    # Flows.
    Candidate(
        ChartType.SANKEY,
        "flow",
        _unless(lambda f: f.links >= 1, "no trials move between the stages"),
        lambda f: 5,
        lambda f: "how trials move from one stage to the next",
    ),
    Candidate(
        ChartType.TABLE, "flow", _never, lambda f: 0, lambda f: "every flow with its trial count"
    ),
    # Individual trials.
    Candidate(
        ChartType.TIMELINE,
        "trials",
        _all(
            _unless(lambda f: f.dated_trials >= 1, "no trial has a start date"),
            _unless(
                lambda f: f.rows <= TIMELINE_LIMIT,
                f"a timeline stays readable only up to {TIMELINE_LIMIT} trials",
            ),
        ),
        lambda f: 5,
        lambda f: "each trial drawn from its start to its completion",
    ),
    Candidate(
        ChartType.TABLE,
        "trials",
        _never,
        lambda f: 1 + 5 * (f.rows > TIMELINE_LIMIT),
        lambda f: (
            "too many trials for a timeline, so a sortable table"
            if f.rows > TIMELINE_LIMIT
            else "every trial with its details"
        ),
    ),
]


def facts_of(dataset: Dataset, presentation: Presentation, single_valued: bool) -> Facts:
    """Facts about the data that the rules read. `single_valued` says whether every trial
    sits in exactly one group of the dimension being drawn."""
    ranking = presentation.top_n is not None or presentation.sort is SortOrder.VALUE_DESC
    match dataset:
        case Scalar():
            return Facts(shape="scalar")
        case Categorical():
            labels = [g.label for g in dataset.groups]
            return Facts(
                shape="categorical",
                groups=len(dataset.groups),
                ordered=dataset.ordered,
                geographic=dataset.geographic,
                parts_of_whole=single_valued,
                long_labels=bool(labels) and sum(map(len, labels)) / len(labels) > 14,
                ranking=ranking or (not dataset.ordered and len(labels) > 5),
            )
        case Matrix():
            totals = [
                sum(c.count for (r, _), c in dataset.cells.items() if r == row)
                for row, _ in dataset.rows
            ]
            positive = [t for t in totals if t]
            return Facts(
                shape="matrix",
                rows=len(dataset.rows),
                columns=len(dataset.columns),
                parts_of_whole=single_valued,
                uneven_rows=bool(positive) and max(positive) > 2 * min(positive),
            )
        case TimeSeries():
            return Facts(
                shape="time",
                periods=len(dataset.periods),
                series=len(dataset.series),
                ordered=True,
                parts_of_whole=single_valued,
            )
        case Distribution():
            sizes = [len(s.values) for s in dataset.spreads]
            return Facts(
                shape="distribution",
                values=sum(sizes),
                spreads=len(sizes),
                boxable_spreads=sum(size >= MIN_BOX_VALUES for size in sizes),
            )
        case Points():
            return Facts(shape="points", points=len(dataset.points), has_z=dataset.z is not None)
        case Graph():
            return Facts(shape="graph", nodes=len(dataset.nodes), edges=len(dataset.edges))
        case Flow():
            return Facts(shape="flow", links=len(dataset.links))
        case Trials():
            return Facts(
                shape="trials",
                rows=len(dataset.rows),
                dated_trials=sum(r.start is not None for r in dataset.rows),
            )
    raise AssertionError(f"no facts for {type(dataset).__name__}")


def recommend(
    facts: Facts, requested: ChartType | None, requested_by: str = "the caller"
) -> Recommendation:
    candidates = [c for c in CANDIDATES if c.shape == facts.shape]
    fitting: list[tuple[int, int, Candidate]] = []
    ruled_out: list[Choice] = []
    for order, candidate in enumerate(candidates):
        reason = candidate.unfit(facts)
        if reason is None:
            fitting.append((candidate.score(facts), -order, candidate))
        else:
            ruled_out.append(Choice(candidate.chart, reason))
    fitting.sort(key=lambda item: (item[0], item[1]), reverse=True)
    ranked = [c for _, _, c in fitting]

    note = None
    if requested is not None:
        match = next((c for c in ranked if c.chart is requested), None)
        if match is not None:
            ranked.remove(match)
            ranked.insert(0, match)
        else:
            why = next((r.reason for r in ruled_out if r.chart is requested), None) or (
                "it does not draw this kind of data"
            )
            note = (
                f"A {requested.value.replace('_', ' ')} was requested, but {why}, so a "
                f"{ranked[0].chart.value.replace('_', ' ')} is shown instead."
            )

    top = ranked[0]
    recommended = Choice(
        top.chart, f"requested by {requested_by}" if requested is top.chart else top.why(facts)
    )
    alternatives = [Choice(c.chart, c.why(facts)) for c in ranked[1:]]
    return Recommendation(recommended, alternatives, ruled_out, note)
