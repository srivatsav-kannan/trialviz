"""The vocabulary of analyses: what can be counted, grouped, measured, and drawn.

These names are this service's own, chosen to be readable in requests ("group_by":
"country"). Each one maps to registry fields later, in the retrieval and aggregation
stages; here they only need to be well defined and parseable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from trialviz.vocabulary._parsing import enum_parser


class AnalysisKind(StrEnum):
    """The shape of the answer. Each kind has its own parameters (see `query.plan`)."""

    COUNT = "count"
    DISTRIBUTION = "distribution"
    TIME_SERIES = "time_series"
    COMPARISON = "comparison"
    HISTOGRAM = "histogram"
    SCATTER = "scatter"
    NETWORK = "network"
    FLOW = "flow"
    TRIAL_LIST = "trial_list"


parse_analysis_kind = enum_parser(
    AnalysisKind,
    {
        "trend": AnalysisKind.TIME_SERIES,
        "compare": AnalysisKind.COMPARISON,
        "sankey": AnalysisKind.FLOW,
        "timeline": AnalysisKind.TRIAL_LIST,
        "list": AnalysisKind.TRIAL_LIST,
    },
    noun="analysis kind",
)


class Dimension(StrEnum):
    """A categorical property of a trial that results can be grouped or linked by."""

    PHASE = "phase"
    STATUS = "status"
    SPONSOR_CLASS = "sponsor_class"
    LEAD_SPONSOR = "lead_sponsor"
    INTERVENTION = "intervention"
    INTERVENTION_TYPE = "intervention_type"
    CONDITION = "condition"
    COUNTRY = "country"
    STUDY_TYPE = "study_type"
    START_YEAR = "start_year"
    INVESTIGATOR = "investigator"
    SITE = "site"

    @property
    def label(self) -> str:
        return _DIMENSION_INFO[self].label

    @property
    def multi_valued(self) -> bool:
        """Whether one trial can hold several values (several countries, several drugs).

        Multi-valued dimensions are the only ones that can link to themselves in a
        network: two drugs can share a trial, two lead sponsors cannot.
        """
        return _DIMENSION_INFO[self].multi_valued


@dataclass(frozen=True)
class _DimensionInfo:
    label: str
    multi_valued: bool


# Phase is single-valued on purpose: a Phase 1/2 trial is grouped as one "Phase 1/2"
# bucket rather than counted once under each phase.
_DIMENSION_INFO = {
    Dimension.PHASE: _DimensionInfo("Phase", multi_valued=False),
    Dimension.STATUS: _DimensionInfo("Overall status", multi_valued=False),
    Dimension.SPONSOR_CLASS: _DimensionInfo("Sponsor class", multi_valued=False),
    Dimension.LEAD_SPONSOR: _DimensionInfo("Lead sponsor", multi_valued=False),
    Dimension.INTERVENTION: _DimensionInfo("Intervention", multi_valued=True),
    Dimension.INTERVENTION_TYPE: _DimensionInfo("Intervention type", multi_valued=True),
    Dimension.CONDITION: _DimensionInfo("Condition", multi_valued=True),
    Dimension.COUNTRY: _DimensionInfo("Country", multi_valued=True),
    Dimension.STUDY_TYPE: _DimensionInfo("Study type", multi_valued=False),
    Dimension.START_YEAR: _DimensionInfo("Start year", multi_valued=False),
    Dimension.INVESTIGATOR: _DimensionInfo("Investigator", multi_valued=True),
    Dimension.SITE: _DimensionInfo("Site", multi_valued=True),
}

parse_dimension = enum_parser(
    Dimension,
    {
        "trial phase": Dimension.PHASE,
        "overall status": Dimension.STATUS,
        "sponsor type": Dimension.SPONSOR_CLASS,
        "sponsor category": Dimension.SPONSOR_CLASS,
        "sponsor": Dimension.LEAD_SPONSOR,
        "drug": Dimension.INTERVENTION,
        "disease": Dimension.CONDITION,
        "year": Dimension.START_YEAR,
        "principal investigator": Dimension.INVESTIGATOR,
        "facility": Dimension.SITE,
    },
    noun="dimension",
)


class Measure(StrEnum):
    """A numeric property of a trial, for histograms and scatter plots."""

    ENROLLMENT = "enrollment"
    DURATION_MONTHS = "duration_months"
    SITE_COUNT = "site_count"

    @property
    def label(self) -> str:
        return _MEASURE_LABELS[self][0]

    @property
    def unit(self) -> str:
        return _MEASURE_LABELS[self][1]


_MEASURE_LABELS = {
    Measure.ENROLLMENT: ("Enrollment", "participants"),
    Measure.DURATION_MONTHS: ("Duration", "months"),
    Measure.SITE_COUNT: ("Number of sites", "sites"),
}

parse_measure = enum_parser(
    Measure,
    {
        "participants": Measure.ENROLLMENT,
        "sample size": Measure.ENROLLMENT,
        "duration": Measure.DURATION_MONTHS,
        "sites": Measure.SITE_COUNT,
    },
    noun="measure",
)


class TimeField(StrEnum):
    """Which date a time series counts trials by."""

    START_DATE = "start_date"
    COMPLETION_DATE = "completion_date"


parse_time_field = enum_parser(TimeField, noun="time field")


class TimeGranularity(StrEnum):
    """How finely a time series buckets dates."""

    YEAR = "year"
    MONTH = "month"


parse_time_granularity = enum_parser(
    TimeGranularity,
    {
        "yearly": TimeGranularity.YEAR,
        "annual": TimeGranularity.YEAR,
        "monthly": TimeGranularity.MONTH,
    },
    noun="time granularity",
)


class ChartType(StrEnum):
    """Every chart the service can recommend or draw. Which ones fit a given result is
    decided by the visualization stage, from the data's shape and facts about it."""

    STAT = "stat"
    BAR_CHART = "bar_chart"
    HORIZONTAL_BAR_CHART = "horizontal_bar_chart"
    DONUT_CHART = "donut_chart"
    TREEMAP = "treemap"
    CHOROPLETH_MAP = "choropleth_map"
    TABLE = "table"
    GROUPED_BAR_CHART = "grouped_bar_chart"
    STACKED_BAR_CHART = "stacked_bar_chart"
    NORMALIZED_STACKED_BAR_CHART = "normalized_stacked_bar_chart"
    HEATMAP = "heatmap"
    TIME_SERIES = "time_series"
    STACKED_AREA_CHART = "stacked_area_chart"
    HISTOGRAM = "histogram"
    BOX_PLOT = "box_plot"
    SCATTER_PLOT = "scatter_plot"
    BUBBLE_CHART = "bubble_chart"
    SCATTER_PLOT_3D = "scatter_plot_3d"
    NETWORK_GRAPH = "network_graph"
    NETWORK_GRAPH_3D = "network_graph_3d"
    SANKEY = "sankey"
    TIMELINE = "timeline"


parse_chart_type = enum_parser(
    ChartType,
    {
        "bar": ChartType.BAR_CHART,
        "column chart": ChartType.BAR_CHART,
        "line": ChartType.TIME_SERIES,
        "line chart": ChartType.TIME_SERIES,
        "area": ChartType.STACKED_AREA_CHART,
        "pie": ChartType.DONUT_CHART,
        "pie chart": ChartType.DONUT_CHART,
        "map": ChartType.CHOROPLETH_MAP,
        "scatter": ChartType.SCATTER_PLOT,
        "network": ChartType.NETWORK_GRAPH,
        "graph": ChartType.NETWORK_GRAPH,
        "3d network": ChartType.NETWORK_GRAPH_3D,
        "3d scatter": ChartType.SCATTER_PLOT_3D,
        "gantt": ChartType.TIMELINE,
        "boxplot": ChartType.BOX_PLOT,
    },
    noun="chart type",
)


class SortOrder(StrEnum):
    """`natural` keeps a dimension's inherent order (Phase 1 before Phase 2, years ascending)."""

    NATURAL = "natural"
    VALUE_DESC = "value_desc"
    VALUE_ASC = "value_asc"
    LABEL_ASC = "label_asc"


parse_sort_order = enum_parser(SortOrder, noun="sort order")
