"""Which trials a question is about.

Matching semantics, used everywhere a `Filters` appears (the request, the plan, a
comparison cohort): values inside one field are alternatives (any of), and separate
fields must all hold (all of). `{"phases": ["PHASE2", "PHASE3"], "countries": ["Japan"]}`
means trials in Phase 2 or Phase 3 that have a site in Japan.
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from trialviz.query.consistency import FILTER_RULES, require_rules
from trialviz.query.fields import (
    CountryList,
    EntityList,
    InterventionTypeList,
    PhaseList,
    SponsorClassList,
    StatusList,
    StudyTypeList,
    Year,
    set_values,
)


class Filters(BaseModel):
    """Constraints on the trial set. Every field is optional. Omit a field rather than
    sending an empty list."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conditions: EntityList | None = Field(None, description="Conditions or diseases studied.")
    interventions: EntityList | None = Field(
        None, description="Drugs, biologics, devices or other interventions, as named."
    )
    sponsors: EntityList | None = Field(None, description="Lead sponsor organizations.")
    keywords: EntityList | None = Field(
        None,
        description=(
            "Free-text terms matched anywhere in the trial record. Use when it is unclear "
            "whether a term is a drug, a condition or something else."
        ),
    )
    phases: PhaseList | None = Field(
        None, description='Accepts "PHASE3", "Phase 3", "Phase III" or "3"; "1/2" means both.'
    )
    statuses: StatusList | None = Field(None, description="Overall recruitment status.")
    sponsor_classes: SponsorClassList | None = Field(None, description="Lead sponsor category.")
    intervention_types: InterventionTypeList | None = Field(
        None, description="Intervention types, such as DRUG or BIOLOGICAL. Any of them matches."
    )
    study_types: StudyTypeList | None = Field(
        None, description="Study types, such as INTERVENTIONAL. Any of them matches."
    )
    countries: CountryList | None = Field(
        None, description="Countries with at least one trial site, in the registry's spelling."
    )
    start_year_from: Year | None = Field(None, description="Earliest trial start year, inclusive.")
    start_year_to: Year | None = Field(None, description="Latest trial start year, inclusive.")

    @model_validator(mode="after")
    def _pairs_are_consistent(self) -> Self:
        require_rules(self.model_dump(exclude_none=True), FILTER_RULES)
        return self

    def set_fields(self) -> dict[str, object]:
        """The constrained fields and their values, in declaration order."""
        return set_values(self)

    def is_empty(self) -> bool:
        return not self.set_fields()


CohortLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class Cohort(BaseModel):
    """One side of a comparison. Its filters apply on top of the plan's shared filters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: CohortLabel = Field(description='Display name, such as "Pembrolizumab".')
    filters: Filters

    @model_validator(mode="after")
    def _constrains_something(self) -> Self:
        if self.filters.is_empty():
            raise ValueError(f"cohort {self.label!r} has no filters, so it would match everything")
        return self
