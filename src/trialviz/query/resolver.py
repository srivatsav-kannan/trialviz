"""Resolve a request into a plan by merging the caller's fields with the interpreter's proposal.

Every plan slot is filled from one of four sources, in strict priority order:

1. the caller's explicit field (`user`),
2. the interpreter's reading of the question (`inferred`),
3. a value implied by other slots (`derived`),
4. the documented default (`default`).

The interpreter sees the caller's fields as context, but it is not trusted to respect
them. The merge below enforces the priority itself, so an interpreter that contradicts a
field changes nothing except a note. Interpreter values pass through the same parsers and
consistency rules as the caller's; invalid ones are dropped individually rather than
failing the request, because one bad guess should not cost the rest of a good reading.
Caller values that fail validation never get here: they are rejected with a 422 at the
request boundary.

Resolution ends in one of two outcomes: a complete `Plan` with provenance for every slot,
or a `ClarificationNeeded` that names the slots that would complete it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from trialviz.query.consistency import ANALYSIS_RULES, FILTER_RULES, broken_rules, same_meaning
from trialviz.query.filters import Cohort
from trialviz.query.interpreters.base import Interpretation, Interpreter, InterpreterUnavailable
from trialviz.query.interpreters.rules import unnamed_references
from trialviz.query.plan import (
    DEFAULT_CITATIONS_PER_DATUM,
    DEFAULT_NAME_MATCHING,
    ComparisonSpec,
    Plan,
    kinds_accepting,
    parameter_defaults,
    parameters_of,
    required_parameters,
)
from trialviz.query.request import PLAN_SECTIONS, InferenceMode, QueryRequest
from trialviz.query.resolution import (
    ClarificationNeeded,
    ClarificationReason,
    Note,
    NoteCode,
    ResolvedQuery,
    Source,
)
from trialviz.vocabulary import AnalysisKind

KIND_SLOT = "analysis.kind"

_MISSING_PARAMETER_PROMPTS = {
    "group_by": "What should the trials be grouped by? Set analysis.group_by, for example "
    "phase, status, sponsor_class, intervention or country.",
    "cohorts": "Which groups should be compared? Set analysis.cohorts to two or more cohorts, "
    "each with a label and filters.",
    "measure": "Which measure should the histogram show? Set analysis.measure to enrollment, "
    "duration_months or site_count.",
    "x": "Which measure goes on the horizontal axis? Set analysis.x.",
    "y": "Which measure goes on the vertical axis? Set analysis.y.",
    "node_types": "Which entities should the network link? Set analysis.node_types, for "
    'example ["lead_sponsor", "intervention"].',
}


class QueryResolver:
    def __init__(
        self,
        interpreter: Interpreter | None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._interpreter = interpreter
        self._today = today

    async def resolve(self, request: QueryRequest) -> ResolvedQuery | ClarificationNeeded:
        notes: list[Note] = []
        interpretation = await self._interpret(request, notes)
        interpreter = interpretation.interpreter if interpretation else None

        if interpretation is not None and interpretation.issue is not None:
            issue = interpretation.issue
            reason = ClarificationReason(issue.kind.value)
            return ClarificationNeeded(
                reason=reason,
                message=issue.message,
                missing=unnamed_references(request)
                if reason is ClarificationReason.AMBIGUOUS_REFERENCE
                else [],
                notes=notes,
                interpreter=interpreter,
            )

        inferred = validate_proposal(interpretation.proposal, notes) if interpretation else {}
        draft = merge(explicit_slots(request), inferred, notes)
        enforce_pair_rules(draft, notes)

        if (clarification := settle_analysis(draft, notes)) is not None:
            return clarification.model_copy(update={"interpreter": interpreter})

        if "retrieval.name_matching" not in draft.values:
            draft.set("retrieval.name_matching", DEFAULT_NAME_MATCHING, Source.DEFAULT)
        if "presentation.citations_per_datum" not in draft.values:
            draft.set(
                "presentation.citations_per_datum", DEFAULT_CITATIONS_PER_DATUM, Source.DEFAULT
            )

        plan = build_plan(draft)
        notes.extend(cohort_overlap_notes(plan))
        return ResolvedQuery(
            plan=plan,
            provenance=dict(sorted(draft.provenance.items())),
            notes=notes,
            interpreter=interpreter,
        )

    async def _interpret(self, request: QueryRequest, notes: list[Note]) -> Interpretation | None:
        if request.inference is InferenceMode.OFF:
            notes.append(
                Note(
                    code=NoteCode.QUESTION_NOT_INTERPRETED,
                    message="Inference is off, so only the structured fields were used.",
                )
            )
            return None
        if self._interpreter is None:
            notes.append(
                Note(
                    code=NoteCode.INTERPRETER_UNAVAILABLE,
                    message="No interpreter is configured, so only the structured fields "
                    "were used.",
                )
            )
            return None
        try:
            interpretation = await self._interpreter.interpret(request, self._today())
        except InterpreterUnavailable as error:
            notes.append(
                Note(
                    code=NoteCode.INTERPRETER_UNAVAILABLE,
                    message=f"The question could not be interpreted ({error.reason}), so only "
                    "the structured fields were used.",
                )
            )
            return None
        notes.extend(
            Note(
                code=NoteCode.INTERPRETER_FALLBACK,
                message=f"{fallback.interpreter} was unavailable ({fallback.reason}), so "
                f"{interpretation.interpreter} read the question.",
            )
            for fallback in interpretation.fallbacks
        )
        notes.extend(
            Note(
                code=NoteCode.CONSTRAINT_NOT_APPLIED,
                message=f'The question asks for "{constraint}", which no filter here can '
                "express, so the results are not limited by it.",
            )
            for constraint in interpretation.unexpressed
        )
        return interpretation


@dataclass
class Draft:
    """Slot values keyed by path ("filters.phases"), each with the source that set it."""

    values: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Source] = field(default_factory=dict)

    def set(self, path: str, value: Any, source: Source) -> None:
        self.values[path] = value
        self.provenance[path] = source

    def drop(self, path: str) -> None:
        self.values.pop(path, None)
        self.provenance.pop(path, None)

    def source(self, path: str) -> Source | None:
        return self.provenance.get(path)

    def section(self, name: str) -> dict[str, Any]:
        prefix = f"{name}."
        return {
            path.removeprefix(prefix): value
            for path, value in self.values.items()
            if path.startswith(prefix)
        }


def explicit_slots(request: QueryRequest) -> dict[str, Any]:
    """Every slot the caller set, keyed by path."""
    return {
        f"{section}.{name}": value
        for section in PLAN_SECTIONS
        for name, value in getattr(request, section)
        if value is not None
    }


def validate_proposal(proposal: Mapping[str, object], notes: list[Note]) -> dict[str, Any]:
    """Validate an interpreter's proposal one slot at a time.

    Each value is parsed by the same model field that parses caller input, so it ends up
    canonical. A value that fails is dropped with a note; the rest of the proposal stands.
    """
    slots: dict[str, Any] = {}
    for section, raw_fields in proposal.items():
        model = PLAN_SECTIONS.get(section)
        if model is None or not isinstance(raw_fields, Mapping):
            if raw_fields is not None:
                notes.append(_rejected(section, f"{section!r} is not a plan section"))
            continue
        for name, raw in raw_fields.items():
            if raw is None:
                continue
            path = f"{section}.{name}"
            if name not in model.model_fields:
                notes.append(_rejected(path, f"{path} is not a known field"))
                continue
            try:
                slots[path] = getattr(model.model_validate({name: raw}), name)
            except ValidationError as error:
                notes.append(_rejected(path, _first_error(error)))
    return slots


def merge(explicit: Mapping[str, Any], inferred: Mapping[str, Any], notes: list[Note]) -> Draft:
    """Combine caller and interpreter slots. The caller's value wins every overlap."""
    draft = Draft()
    for path, value in explicit.items():
        draft.set(path, value, Source.USER)
    for path, value in inferred.items():
        if path not in explicit:
            draft.set(path, value, Source.INFERRED)
        elif not same_meaning(value, explicit[path]):
            notes.append(
                Note(
                    code=NoteCode.FIELD_OVERRIDES_QUESTION,
                    slot=path,
                    message=f"The question suggests {describe(value)} for {path}, but the "
                    f"request sets {describe(explicit[path])}, so the request's value was used.",
                )
            )
    return draft


def enforce_pair_rules(draft: Draft, notes: list[Note]) -> None:
    """Drop inferred values that break a pair rule together with another slot.

    A pair made only of caller values cannot be broken here, because request validation
    applies the same rules. So every broken pair contains an inferred value to drop.
    """
    for section, rules in (("filters", FILTER_RULES), ("analysis", ANALYSIS_RULES)):
        values = draft.section(section)
        for rule in broken_rules(values, rules):
            paths = [f"{section}.{slot}" for slot in reversed(rule.slots)]
            victim = next(path for path in paths if draft.source(path) is not Source.USER)
            notes.append(_rejected(victim, rule.describe(values, prefix=f"{section}.")))
            draft.drop(victim)


def settle_analysis(draft: Draft, notes: list[Note]) -> ClarificationNeeded | None:
    """Fix the analysis kind, drop parameters that do not belong to it, and fill defaults.

    Returns a clarification when the kind cannot be determined, when the question's kind
    contradicts the caller's parameters, or when a required parameter is missing.
    """
    analysis = draft.section("analysis")
    parameters = analysis.keys() - {"kind"}
    caller_parameters = {
        name for name in parameters if draft.source(f"analysis.{name}") is Source.USER
    }
    kind: AnalysisKind | None = analysis.get("kind")

    if (
        kind is not None
        and draft.source(KIND_SLOT) is Source.INFERRED
        and not caller_parameters <= parameters_of(kind).keys()
    ):
        # The question reads as one kind of analysis; the caller's parameters say another.
        return _conflicting_kind(kind, caller_parameters, notes)

    if kind is None:
        kind = _derive_kind(parameters, caller_parameters)
        if kind is None:
            return ClarificationNeeded(
                reason=ClarificationReason.MISSING_PARAMETER,
                message="What should be computed? Set analysis.kind to one of: "
                + ", ".join(k.value for k in AnalysisKind)
                + ".",
                missing=[KIND_SLOT],
                notes=notes,
            )
        draft.set(KIND_SLOT, kind, Source.DERIVED)

    for name in sorted(parameters - parameters_of(kind).keys()):
        path = f"analysis.{name}"
        notes.append(
            Note(
                code=NoteCode.INFERRED_VALUE_NOT_APPLICABLE,
                slot=path,
                message=f"Ignored the interpreted {path}: it does not apply to a "
                f"{kind.value} analysis.",
            )
        )
        draft.drop(path)

    for name, default in parameter_defaults(kind).items():
        if f"analysis.{name}" not in draft.values:
            draft.set(f"analysis.{name}", default, Source.DEFAULT)

    missing = [name for name in required_parameters(kind) if f"analysis.{name}" not in draft.values]
    if missing:
        return ClarificationNeeded(
            reason=ClarificationReason.MISSING_PARAMETER,
            message=" ".join(_MISSING_PARAMETER_PROMPTS[name] for name in missing),
            missing=[f"analysis.{name}" for name in missing],
            notes=notes,
        )
    return None


def _derive_kind(parameters: set[str], caller_parameters: set[str]) -> AnalysisKind | None:
    """The one kind the set parameters point to, or None if they do not single one out.

    A kind is implied when it is the only kind that accepts all the parameters
    (`granularity` implies time_series), or the only accepting kind that requires one of
    them and has everything it requires (`group_by` implies distribution, since comparison
    would also need cohorts). Parameters that several kinds use optionally, such as
    `series_by` alone, imply nothing. If the full parameter set fits no kind, the caller's
    parameters alone decide, and the interpreter's misfits are dropped afterwards.
    """
    for candidate_parameters in (parameters, caller_parameters):
        accepting = kinds_accepting(candidate_parameters) if candidate_parameters else []
        if not accepting:
            continue
        if len(accepting) == 1:
            return accepting[0]
        implied = [
            kind
            for kind in accepting
            if candidate_parameters & set(required_parameters(kind))
            and set(required_parameters(kind)) <= candidate_parameters
        ]
        return implied[0] if len(implied) == 1 else None
    return None


def _conflicting_kind(
    kind: AnalysisKind, caller_parameters: set[str], notes: list[Note]
) -> ClarificationNeeded:
    fitting = kinds_accepting(caller_parameters)
    stray = ", ".join(f"analysis.{name}" for name in sorted(caller_parameters))
    options = " or ".join(k.value for k in fitting) if fitting else "no single analysis"
    return ClarificationNeeded(
        reason=ClarificationReason.CONFLICTING_ANALYSIS,
        message=f"The question reads as a {kind.value} analysis, but {stray} applies to "
        f"{options}. Set analysis.kind to say which you want.",
        missing=[KIND_SLOT],
        notes=notes,
    )


def build_plan(draft: Draft) -> Plan:
    """Assemble the final plan. A failure here is a resolver bug, not bad input."""
    try:
        return Plan.model_validate({section: draft.section(section) for section in PLAN_SECTIONS})
    except ValidationError as error:
        raise RuntimeError(f"resolver produced an invalid plan from {draft.values!r}") from error


def cohort_overlap_notes(plan: Plan) -> list[Note]:
    """Warn when a cohort constrains a field that the shared filters also constrain.

    Shared filters apply to every cohort, so a shared `interventions: [A]` plus a cohort
    `interventions: [B]` means "trials with both A and B", which is rarely what a
    comparison of A against B intends.
    """
    if not isinstance(plan.analysis, ComparisonSpec):
        return []
    shared = plan.filters.set_fields().keys()
    notes = []
    for cohort in plan.analysis.cohorts:
        overlap = sorted(shared & cohort.filters.set_fields().keys())
        if overlap:
            notes.append(
                Note(
                    code=NoteCode.COHORT_NARROWED_BY_SHARED_FILTER,
                    slot="analysis.cohorts",
                    message=f"Cohort {cohort.label!r} and the shared filters both set "
                    f"{', '.join(overlap)}. A trial must match both, so the cohort may be "
                    "narrower than intended.",
                )
            )
    return notes


def describe(value: Any) -> str:
    """A short human rendering of a slot value for notes."""
    if isinstance(value, Cohort):
        return value.label
    if isinstance(value, list | tuple):
        return ", ".join(describe(item) for item in value)
    if isinstance(value, StrEnum):
        return str(getattr(value, "label", value.value))
    return str(value)


def _rejected(path: str, reason: str) -> Note:
    return Note(
        code=NoteCode.INFERRED_VALUE_REJECTED,
        slot=path,
        message=f"Ignored the interpreted {path}: {reason}.",
    )


def _first_error(error: ValidationError) -> str:
    first = error.errors(include_url=False)[0]
    return str(first["msg"]).removeprefix("Value error, ").rstrip(".")
