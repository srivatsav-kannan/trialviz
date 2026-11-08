"""The resolved plan: a complete, validated description of what to compute.

A plan is what the rest of the pipeline consumes. Unlike the request, nothing in it is
optional in an ambiguous way: the analysis is one concrete kind with exactly the
parameters that kind needs, so downstream code can match on `analysis.kind` and never
check for parameters that do not apply.

`ANALYSIS_SPECS` is the single source of truth for which parameters belong to which
kind, which are required, and what the defaults are. Request validation, the resolver,
and the language-model schema all read it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic.fields import FieldInfo

from trialviz.query.consistency import ANALYSIS_RULES, require_rules, same_meaning
from trialviz.query.fields import (
    ChartTypeField,
    DimensionField,
    GranularityField,
    MeasureField,
    SortOrderField,
    TimeFieldField,
)
from trialviz.query.filters import Cohort, Filters
from trialviz.vocabulary import (
    AnalysisKind,
    Dimension,
    TimeField,
    TimeGranularity,
)

MIN_COHORTS = 2
MAX_COHORTS = 6
DEFAULT_CITATIONS_PER_DATUM = 3


def validate_node_types(pair: tuple[Dimension, Dimension]) -> tuple[Dimension, Dimension]:
    first, second = pair
    if first == second and not first.multi_valued:
        raise ValueError(
            f"a {first.value}-{first.value} network is empty: each trial has only one "
            f"{first.value}, so no two values ever share a trial"
        )
    return pair


def validate_cohorts(cohorts: list[Cohort]) -> list[Cohort]:
    labels = [cohort.label.casefold() for cohort in cohorts]
    if len(set(labels)) != len(labels):
        raise ValueError("cohort labels must be unique")
    for index, cohort in enumerate(cohorts):
        if any(same_meaning(cohort.filters, other.filters) for other in cohorts[index + 1 :]):
            raise ValueError("two cohorts have identical filters, so the comparison is degenerate")
    return cohorts


Cohorts = Annotated[
    list[Cohort],
    Field(min_length=MIN_COHORTS, max_length=MAX_COHORTS),
    AfterValidator(validate_cohorts),
]
"""Two to six cohorts with distinct labels and distinct filters, wherever cohorts appear."""

NodeTypes = Annotated[tuple[DimensionField, DimensionField], AfterValidator(validate_node_types)]
"""The two kinds of entity a network links, which must be able to share a trial."""


class _AnalysisSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _pairs_are_consistent(self) -> Self:
        require_rules(self.model_dump(exclude_none=True), ANALYSIS_RULES)
        return self


class CountSpec(_AnalysisSpec):
    """A single number: how many trials match."""

    kind: Literal[AnalysisKind.COUNT] = Field(
        AnalysisKind.COUNT, description="Selects this analysis."
    )


class DistributionSpec(_AnalysisSpec):
    """Trial counts per value of one dimension, optionally split into series."""

    kind: Literal[AnalysisKind.DISTRIBUTION] = Field(
        AnalysisKind.DISTRIBUTION, description="Selects this analysis."
    )
    group_by: DimensionField = Field(description="The dimension that groups the trials.")
    series_by: DimensionField | None = Field(
        None, description="A second dimension that splits the result into series."
    )


class TimeSeriesSpec(_AnalysisSpec):
    """Trial counts per time bucket, optionally split into series."""

    kind: Literal[AnalysisKind.TIME_SERIES] = Field(
        AnalysisKind.TIME_SERIES, description="Selects this analysis."
    )
    time_field: TimeFieldField = Field(TimeField.START_DATE, description="The date counted.")
    granularity: GranularityField = Field(TimeGranularity.YEAR, description="The bucket size.")
    series_by: DimensionField | None = Field(
        None, description="A second dimension that splits the result into series."
    )


class ComparisonSpec(_AnalysisSpec):
    """Two or more cohorts side by side, either as totals or across one dimension."""

    kind: Literal[AnalysisKind.COMPARISON] = Field(
        AnalysisKind.COMPARISON, description="Selects this analysis."
    )
    cohorts: Cohorts = Field(description="The groups of trials compared side by side.")
    group_by: DimensionField | None = Field(
        None, description="An optional dimension to break the result down by."
    )


class HistogramSpec(_AnalysisSpec):
    """The spread of one numeric measure across trials, optionally split into groups
    (enrollment by phase), which is drawn as one box per group."""

    kind: Literal[AnalysisKind.HISTOGRAM] = Field(
        AnalysisKind.HISTOGRAM, description="Selects this analysis."
    )
    measure: MeasureField = Field(description="The numeric measure whose spread is shown.")
    group_by: DimensionField | None = Field(
        None, description="An optional dimension to break the result down by."
    )


class ScatterSpec(_AnalysisSpec):
    """One point per trial, positioned by two numeric measures. A third measure `z`
    becomes point size in 2D or depth in 3D."""

    kind: Literal[AnalysisKind.SCATTER] = Field(
        AnalysisKind.SCATTER, description="Selects this analysis."
    )
    x: MeasureField = Field(description="The measure on the horizontal axis.")
    y: MeasureField = Field(description="The measure on the vertical axis.")
    z: MeasureField | None = Field(None, description="A third measure, which adds a 3D view.")
    series_by: DimensionField | None = Field(
        None, description="A second dimension that splits the result into series."
    )


class NetworkSpec(_AnalysisSpec):
    """Entities as nodes, linked when they appear in the same trial."""

    kind: Literal[AnalysisKind.NETWORK] = Field(
        AnalysisKind.NETWORK, description="Selects this analysis."
    )
    node_types: NodeTypes = Field(
        description="The two kinds of entity linked when they share a trial."
    )


def validate_stages(stages: list[Dimension]) -> list[Dimension]:
    if len(set(stages)) != len(stages):
        raise ValueError("flow stages must be different dimensions")
    multi = [stage.value for stage in stages if stage.multi_valued]
    if multi:
        raise ValueError(
            f"{', '.join(multi)} can hold several values per trial, so a trial would follow "
            "several paths at once. Flow stages must have one value per trial"
        )
    return stages


Stages = Annotated[
    list[DimensionField], Field(min_length=2, max_length=3), AfterValidator(validate_stages)
]


class FlowSpec(_AnalysisSpec):
    """Trials flowing through two or three single-valued dimensions in order, such as
    sponsor class, then phase, then status."""

    kind: Literal[AnalysisKind.FLOW] = Field(
        AnalysisKind.FLOW, description="Selects this analysis."
    )
    stages: Stages = Field(description="Two or three single-valued dimensions, in order.")


class TrialListSpec(_AnalysisSpec):
    """The individual trials, with their dates, for a timeline or a table."""

    kind: Literal[AnalysisKind.TRIAL_LIST] = Field(
        AnalysisKind.TRIAL_LIST, description="Selects this analysis."
    )


AnalysisSpec = Annotated[
    CountSpec
    | DistributionSpec
    | TimeSeriesSpec
    | ComparisonSpec
    | HistogramSpec
    | ScatterSpec
    | NetworkSpec
    | FlowSpec
    | TrialListSpec,
    Field(discriminator="kind"),
]

ANALYSIS_SPECS: dict[AnalysisKind, type[_AnalysisSpec]] = {
    AnalysisKind.COUNT: CountSpec,
    AnalysisKind.DISTRIBUTION: DistributionSpec,
    AnalysisKind.TIME_SERIES: TimeSeriesSpec,
    AnalysisKind.COMPARISON: ComparisonSpec,
    AnalysisKind.HISTOGRAM: HistogramSpec,
    AnalysisKind.SCATTER: ScatterSpec,
    AnalysisKind.NETWORK: NetworkSpec,
    AnalysisKind.FLOW: FlowSpec,
    AnalysisKind.TRIAL_LIST: TrialListSpec,
}


def parameters_of(kind: AnalysisKind) -> dict[str, FieldInfo]:
    """The parameters a kind accepts (every spec field except the discriminator)."""
    fields = ANALYSIS_SPECS[kind].model_fields
    return {name: info for name, info in fields.items() if name != "kind"}


def required_parameters(kind: AnalysisKind) -> list[str]:
    return [name for name, info in parameters_of(kind).items() if info.is_required()]


def parameter_defaults(kind: AnalysisKind) -> dict[str, object]:
    """Parameters with a non-null default, which the resolver fills in when absent."""
    return {
        name: info.default
        for name, info in parameters_of(kind).items()
        if not info.is_required() and info.default is not None
    }


def kinds_accepting(parameters: set[str]) -> list[AnalysisKind]:
    """Every kind whose parameter list includes all of `parameters`."""
    return [kind for kind in AnalysisKind if parameters <= parameters_of(kind).keys()]


class Presentation(BaseModel):
    """Display preferences. Unset values are chosen by the visualization stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chart_type: ChartTypeField | None = Field(
        None, description="Preferred chart. Ignored with a note if it cannot show the result."
    )
    sort: SortOrderField | None = None
    top_n: int | None = Field(None, ge=1, le=100, description="Keep only the largest N groups.")
    citations_per_datum: int | None = Field(
        None, ge=0, le=25, description="Source citations attached to each data point."
    )


class NameMatching(StrEnum):
    """How strictly a drug or condition name must match a trial.

    `named` counts a trial only when the name is listed among its interventions or
    conditions. `mentioned` also counts trials that mention the name only in descriptions,
    such as an arm that permits "a PD-1 inhibitor, for example pembrolizumab". Under either
    setting, mention-only trials are retrieved and reported. The setting decides whether
    they are counted.
    """

    NAMED = "named"
    MENTIONED = "mentioned"


DEFAULT_NAME_MATCHING = NameMatching.NAMED


class RetrievalOptions(BaseModel):
    """How the plan's filters are applied to the registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name_matching: NameMatching | None = Field(
        None, description="named (default) or mentioned. See NameMatching."
    )


class Plan(BaseModel):
    """Everything the pipeline needs to answer a question, with no open choices left."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filters: Filters
    analysis: AnalysisSpec
    presentation: Presentation
    retrieval: RetrievalOptions = Field(default_factory=RetrievalOptions)
