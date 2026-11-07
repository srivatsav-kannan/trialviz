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
    USER = "user"
    """Set explicitly in the request. Always wins."""
    INFERRED = "inferred"
    """Read from the question by the interpreter."""
    DERIVED = "derived"
    """Implied by other slots, such as an analysis kind implied by its parameters."""
    DEFAULT = "default"
    """Filled with the documented default because nothing else set it."""


class NoteCode(StrEnum):
    FIELD_OVERRIDES_QUESTION = "field_overrides_question"
    INFERRED_VALUE_REJECTED = "inferred_value_rejected"
    INFERRED_VALUE_NOT_APPLICABLE = "inferred_value_not_applicable"
    INTERPRETER_FALLBACK = "interpreter_fallback"
    INTERPRETER_UNAVAILABLE = "interpreter_unavailable"
    QUESTION_NOT_INTERPRETED = "question_not_interpreted"
    CONSTRAINT_NOT_APPLIED = "constraint_not_applied"
    COHORT_NARROWED_BY_SHARED_FILTER = "cohort_narrowed_by_shared_filter"


class Note(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: NoteCode
    message: str
    slot: str | None = Field(None, description="The slot path the note is about, if any.")


class ClarificationReason(StrEnum):
    MISSING_PARAMETER = "missing_parameter"
    """The analysis needs a value nobody supplied, such as what to group by."""
    AMBIGUOUS_REFERENCE = "ambiguous_reference"
    """The question points at something it never names ("this drug")."""
    CONFLICTING_ANALYSIS = "conflicting_analysis"
    """The structured fields and the question describe incompatible analyses."""
    OUT_OF_SCOPE = "out_of_scope"
    """The question is not something clinical-trial registry data can answer."""


class ResolvedQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["resolved"] = "resolved"
    plan: Plan
    provenance: dict[str, Source]
    notes: list[Note] = Field(default_factory=list)
    interpreter: str | None = Field(
        None, description="The interpreter that read the question, or null if none ran."
    )


class ClarificationNeeded(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["needs_clarification"] = "needs_clarification"
    reason: ClarificationReason
    message: str = Field(description="A question or instruction a person can act on.")
    missing: list[str] = Field(
        default_factory=list, description="Slot paths that would resolve the request if set."
    )
    notes: list[Note] = Field(default_factory=list)
    interpreter: str | None = None


Resolution = Annotated[ResolvedQuery | ClarificationNeeded, Field(discriminator="status")]
