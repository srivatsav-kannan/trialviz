"""The public request: a question plus any part of the plan the caller wants to pin down.

`filters`, `analysis` and `presentation` mirror the plan's three parts, with every field
optional. Whatever the caller sets is binding; the interpreter fills the rest from the
question (see `query.resolver`). The schema is closed: an unknown or misspelled field is
rejected rather than silently ignored.

The flat fields in the assignment's example request (`drug_name`, `condition`,
`trial_phase`, `sponsor`, `country`, `start_year`, `end_year`) are accepted as shorthands
and folded into `filters`, so that request works verbatim.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from trialviz.query.consistency import ANALYSIS_RULES, require_rules
from trialviz.query.fields import (
    AnalysisKindField,
    DimensionField,
    EntityName,
    GranularityField,
    MeasureField,
    TimeFieldField,
    Year,
    set_values,
)
from trialviz.query.filters import Filters
from trialviz.query.plan import (
    Cohorts,
    NodeTypes,
    Presentation,
    RetrievalOptions,
    Stages,
    parameters_of,
)

MAX_QUERY_LENGTH = 2000


class AnalysisFields(BaseModel):
    """Any subset of an analysis. Parameters that apply to no chosen kind are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AnalysisKindField | None = None
    group_by: DimensionField | None = Field(None, description="distribution, comparison, histogram")
    series_by: DimensionField | None = Field(None, description="distribution, time_series, scatter")
    time_field: TimeFieldField | None = Field(None, description="time_series")
    granularity: GranularityField | None = Field(None, description="time_series")
    cohorts: Cohorts | None = Field(None, description="comparison")
    measure: MeasureField | None = Field(None, description="histogram")
    x: MeasureField | None = Field(None, description="scatter")
    y: MeasureField | None = Field(None, description="scatter")
    z: MeasureField | None = Field(None, description="scatter (size in 2D, depth in 3D)")
    stages: Stages | None = Field(None, description="flow")
    node_types: NodeTypes | None = Field(None, description="network")

    @model_validator(mode="after")
    def _parameters_fit_kind(self) -> Self:
        values = self.set_fields()
        require_rules(values, ANALYSIS_RULES)
        if self.kind is not None:
            stray = sorted(values.keys() - {"kind"} - parameters_of(self.kind).keys())
            if stray:
                raise ValueError(
                    f"{', '.join(stray)} does not apply to a {self.kind.value} analysis"
                )
        return self

    def set_fields(self) -> dict[str, object]:
        return set_values(self)


class InferenceMode(StrEnum):
    """`auto` reads the question to fill unset slots. `off` uses only the structured fields,
    which makes the request fully deterministic. The question is then just a label."""

    AUTO = "auto"
    OFF = "off"


QueryText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUERY_LENGTH)
]

# Shorthand field -> (filters field, whether the target is a list).
_SHORTHANDS: dict[str, tuple[str, bool]] = {
    "drug_name": ("interventions", True),
    "condition": ("conditions", True),
    "trial_phase": ("phases", True),
    "sponsor": ("sponsors", True),
    "country": ("countries", True),
    "start_year": ("start_year_from", False),
    "end_year": ("start_year_to", False),
}


class QueryRequest(BaseModel):
    """A natural-language question about clinical trials, with optional structured fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: QueryText = Field(description="The question, in plain language.")
    filters: Filters = Field(
        default_factory=Filters, description="Which trials to include. Every value is binding."
    )
    analysis: AnalysisFields = Field(
        default_factory=AnalysisFields, description="What to compute over those trials."
    )
    presentation: Presentation = Field(
        default_factory=Presentation, description="Chart preference, ordering and citations."
    )
    retrieval: RetrievalOptions = Field(
        default_factory=RetrievalOptions, description="How drug and condition names match."
    )
    inference: InferenceMode = Field(
        InferenceMode.AUTO,
        description="`auto` lets the question fill unset fields; `off` uses only the fields "
        "given, which makes the request fully deterministic.",
    )

    drug_name: EntityName | None = Field(None, description="Shorthand for filters.interventions.")
    condition: EntityName | None = Field(None, description="Shorthand for filters.conditions.")
    trial_phase: EntityName | None = Field(None, description="Shorthand for filters.phases.")
    sponsor: EntityName | None = Field(None, description="Shorthand for filters.sponsors.")
    country: EntityName | None = Field(None, description="Shorthand for filters.countries.")
    start_year: Year | None = Field(None, description="Shorthand for filters.start_year_from.")
    end_year: Year | None = Field(
        None,
        description="Shorthand for filters.start_year_to: the latest start year, not an end date.",
    )

    @model_validator(mode="before")
    @classmethod
    def _fold_shorthands_into_filters(cls, data: Any) -> Any:
        """Move shorthand fields into `filters` before validation, so they get exactly the
        same parsing and consistency checks as the nested form."""
        if not isinstance(data, dict) or not (_SHORTHANDS.keys() & data.keys()):
            return data
        data = dict(data)
        filters = data.get("filters") or {}
        if not isinstance(filters, dict):
            return data  # let normal validation report the type error
        filters = dict(filters)
        for shorthand, (target, is_list) in _SHORTHANDS.items():
            if data.get(shorthand) is None:
                continue
            if filters.get(target) is not None:
                raise ValueError(
                    f"{shorthand} and filters.{target} are both set. Send only one of them."
                )
            value = data.pop(shorthand)
            filters[target] = [value] if is_list and not isinstance(value, list) else value
        data["filters"] = filters
        return data


PLAN_SECTIONS: dict[str, type[BaseModel]] = {
    "filters": Filters,
    "analysis": AnalysisFields,
    "presentation": Presentation,
    "retrieval": RetrievalOptions,
}
"""The parts of a plan and the partial model that parses each. A slot path is
"<section>.<field>", such as "filters.phases"."""
