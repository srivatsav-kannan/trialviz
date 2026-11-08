"""What resolution returns: a plan with its provenance, or a precise request for more input.

Provenance is keyed by slot path ("filters.phases", "analysis.group_by") and says where
each value in the plan came from. Notes explain every decision a caller might otherwise
have to guess at: a field that overrode the question, an inferred value that was thrown
out, a fallback interpreter that was used.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from trialviz.query.plan import Plan


class Source(StrEnum):
    """Where a plan value came from: the caller, the question, another value, or a default."""

    USER = "user"
    """Set explicitly in the request. Always wins."""
    INFERRED = "inferred"
    """Read from the question by the interpreter."""
    DERIVED = "derived"
    """Implied by other slots, such as an analysis kind implied by its parameters."""
    DEFAULT = "default"
    """Filled with the documented default because nothing else set it."""


class NoteCode(StrEnum):
    """What kind of decision a note reports. The message explains the specific case."""

    FIELD_OVERRIDES_QUESTION = "field_overrides_question"
    INFERRED_VALUE_REJECTED = "inferred_value_rejected"
    INFERRED_VALUE_NOT_APPLICABLE = "inferred_value_not_applicable"
    INTERPRETER_FALLBACK = "interpreter_fallback"
    INTERPRETER_UNAVAILABLE = "interpreter_unavailable"
    QUESTION_NOT_INTERPRETED = "question_not_interpreted"
    CONSTRAINT_NOT_APPLIED = "constraint_not_applied"
    COHORT_NARROWED_BY_SHARED_FILTER = "cohort_narrowed_by_shared_filter"
    # Raised after resolution, by aggregation and visualization.
    COUNTED_IN_SEVERAL_GROUPS = "counted_in_several_groups"
    COMPUTED_FROM_SUBSET = "computed_from_subset"
    COUNTS_EXACT_FROM_REGISTRY = "counts_exact_from_registry"
    VALUES_MISSING = "values_missing"
    NETWORK_PRUNED = "network_pruned"
    MENTIONS_REPORTED = "mentions_reported"
    MENTIONS_COUNTED = "mentions_counted"
    TOP_N_APPLIED = "top_n_applied"
    CHART_PREFERENCE_NOT_FITTING = "chart_preference_not_fitting"
    CONDITION_INCLUDES_MESH_TERMS = "condition_includes_mesh_terms"
    PHASE_FILTER_INCLUDES_COMBINED = "phase_filter_includes_combined"
    PERIODS_INCOMPLETE = "periods_incomplete"
    GRID_LIMITED = "grid_limited"


class Note(BaseModel):
    """A decision made while answering that a reader might otherwise misjudge."""

    model_config = ConfigDict(frozen=True)

    code: NoteCode
    message: str = Field(description="A sentence a person can read, naming the values involved.")
    slot: str | None = Field(None, description="The slot path the note is about, if any.")


class ClarificationReason(StrEnum):
    """Why a request needs clarification before it can be answered."""

    MISSING_PARAMETER = "missing_parameter"
    """The analysis needs a value nobody supplied, such as what to group by."""
    AMBIGUOUS_REFERENCE = "ambiguous_reference"
    """The question points at something it never names ("this drug")."""
    CONFLICTING_ANALYSIS = "conflicting_analysis"
    """The structured fields and the question describe incompatible analyses."""
    OUT_OF_SCOPE = "out_of_scope"
    """The question is not something clinical-trial registry data can answer."""


class ResolvedQuery(BaseModel):
    """A request resolved into a complete plan, with the source of every value."""

    model_config = ConfigDict(frozen=True)

    status: Literal["resolved"] = Field("resolved", description="Always `resolved` here.")
    plan: Plan
    provenance: dict[str, Source] = Field(
        description="Where each plan value came from, keyed by slot path such as `filters.phases`."
    )
    notes: list[Note] = Field(default_factory=list, description="Every decision worth knowing.")
    interpreter: str | None = Field(
        None, description="The interpreter that read the question, or null if none ran."
    )


class ClarificationNeeded(BaseModel):
    """The request cannot be answered as asked, and says what would settle it."""

    model_config = ConfigDict(frozen=True)

    status: Literal["needs_clarification"] = Field(
        "needs_clarification", description="Always `needs_clarification` here."
    )
    reason: ClarificationReason
    message: str = Field(description="A question or instruction a person can act on.")
    missing: list[str] = Field(
        default_factory=list, description="Slot paths that would resolve the request if set."
    )
    notes: list[Note] = Field(default_factory=list, description="Every decision worth knowing.")
    interpreter: str | None = Field(
        None, description="The interpreter that read the question, or null if none ran."
    )


Resolution = Annotated[ResolvedQuery | ClarificationNeeded, Field(discriminator="status")]
