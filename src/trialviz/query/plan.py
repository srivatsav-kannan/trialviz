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

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.fields import FieldInfo

from trialviz.query.consistency import ANALYSIS_RULES, broken_rules, same_meaning
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

Cohorts = Annotated[list[Cohort], Field(min_length=MIN_COHORTS, max_length=MAX_COHORTS)]


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


class _AnalysisSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _pairs_are_consistent(self) -> Self:
        values = self.model_dump(exclude_none=True)
        if broken := broken_rules(values, ANALYSIS_RULES):
            raise ValueError(". ".join(rule.describe(values) for rule in broken))
        return self


class CountSpec(_AnalysisSpec):
    """A single number: how many trials match."""

    kind: Literal[AnalysisKind.COUNT] = AnalysisKind.COUNT


class DistributionSpec(_AnalysisSpec):
    """Trial counts per value of one dimension, optionally split into series."""

    kind: Literal[AnalysisKind.DISTRIBUTION] = AnalysisKind.DISTRIBUTION
    group_by: DimensionField
    series_by: DimensionField | None = None


class TimeSeriesSpec(_AnalysisSpec):
    """Trial counts per time bucket, optionally split into series."""

    kind: Literal[AnalysisKind.TIME_SERIES] = AnalysisKind.TIME_SERIES
    time_field: TimeFieldField = TimeField.START_DATE
    granularity: GranularityField = TimeGranularity.YEAR
    series_by: DimensionField | None = None


class ComparisonSpec(_AnalysisSpec):
    """Two or more cohorts side by side, either as totals or across one dimension."""

    kind: Literal[AnalysisKind.COMPARISON] = AnalysisKind.COMPARISON
    cohorts: Cohorts
    group_by: DimensionField | None = None

    @model_validator(mode="after")
    def _cohorts_are_distinct(self) -> Self:
        validate_cohorts(self.cohorts)
        return self


class HistogramSpec(_AnalysisSpec):
    """The spread of one numeric measure across trials."""

    kind: Literal[AnalysisKind.HISTOGRAM] = AnalysisKind.HISTOGRAM
    measure: MeasureField


class ScatterSpec(_AnalysisSpec):
    """One point per trial, positioned by two numeric measures."""

    kind: Literal[AnalysisKind.SCATTER] = AnalysisKind.SCATTER
    x: MeasureField
    y: MeasureField
    series_by: DimensionField | None = None


class NetworkSpec(_AnalysisSpec):
    """Entities as nodes, linked when they appear in the same trial."""

    kind: Literal[AnalysisKind.NETWORK] = AnalysisKind.NETWORK
    node_types: tuple[DimensionField, DimensionField]

    @model_validator(mode="after")
    def _network_can_have_edges(self) -> Self:
        validate_node_types(self.node_types)
        return self


AnalysisSpec = Annotated[
    CountSpec
    | DistributionSpec
    | TimeSeriesSpec
    | ComparisonSpec
    | HistogramSpec
    | ScatterSpec
    | NetworkSpec,
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


class Plan(BaseModel):
    """Everything the pipeline needs to answer a question, with no open choices left."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filters: Filters
    analysis: AnalysisSpec
    presentation: Presentation
