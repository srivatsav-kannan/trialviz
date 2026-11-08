"""An interpreter backed by Claude, constrained to a JSON schema and a hard spend cap.

Claude reads the question and answers with slot assignments: a list of
`{"slot": "filters.phases", "values": ["PHASE3"]}` entries naming only what the question
settles, plus optional comparison cohorts and an issue flag. Structured output guarantees
that shape; it does not make the values right, so the resolver validates every value and
keeps the caller's fields in charge. Claude never produces numbers about trials. Its whole
job is translation.

Why a list of assignments rather than one object with a field per slot: a strict schema
with two dozen nullable fields (repeated inside every cohort) compiles into a grammar the
API rejects as too large. The assignment list is small, the slot names come straight from
the request models so they cannot drift, and the model only writes what it found.

Any failure (no key, the spend cap, a refusal, an outage, unreadable output) raises
InterpreterUnavailable, and the fallback chain moves on to the rule-based interpreter.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any, Literal, Protocol, cast

import anthropic

from trialviz.llm.budget import BudgetExceeded, SpendLedger, TokenPrices, prices_for
from trialviz.query.filters import Filters
from trialviz.query.interpreters.base import (
    Interpretation,
    InterpreterIssue,
    InterpreterUnavailable,
    IssueKind,
)
from trialviz.query.request import PLAN_SECTIONS, QueryRequest
from trialviz.vocabulary import (
    AnalysisKind,
    ChartType,
    Dimension,
    InterventionType,
    Measure,
    OverallStatus,
    Phase,
    SortOrder,
    SponsorClass,
    StudyType,
    TimeField,
    TimeGranularity,
)

# Server-side fallback reruns a request that a safety classifier declines on another model,
# so an over-cautious decline on a drug question does not cost the reading.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Characters per token used to bound the input size before the call. Real English text
# averages closer to four; three overestimates on purpose, because the bound feeds the cap.
_CHARS_PER_TOKEN_BOUND = 3

# Cohorts are not a slot assignment; they have their own field in the output.
_COHORTS_SLOT = "analysis.cohorts"

SLOT_PATHS: tuple[str, ...] = tuple(
    f"{section}.{name}"
    for section, model in PLAN_SECTIONS.items()
    for name in model.model_fields
    if f"{section}.{name}" != _COHORTS_SLOT
)
COHORT_SLOT_PATHS: tuple[str, ...] = tuple(f"filters.{name}" for name in Filters.model_fields)

SlotShape = Literal["list", "integer", "single"]


def _slot_shapes() -> dict[str, SlotShape]:
    """Whether each slot holds a list, an integer, or one string, read from the models'
    own JSON schemas so the conversion below follows any change to a field's type."""
    shapes: dict[str, SlotShape] = {}
    for section, model in PLAN_SECTIONS.items():
        properties = model.model_json_schema()["properties"]
        for name, spec in properties.items():
            types = {alternative.get("type") for alternative in spec.get("anyOf", [spec])}
            shape: SlotShape = (
                "list" if "array" in types else "integer" if "integer" in types else "single"
            )
            shapes[f"{section}.{name}"] = shape
    return shapes


SLOT_SHAPES = _slot_shapes()


def _assignments(paths: Iterable[str]) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["slot", "values"],
            "properties": {
                "slot": {"type": "string", "enum": list(paths)},
                "values": {"type": "array", "items": {"type": "string"}},
            },
        },
    }


OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["slots", "cohorts", "unexpressed", "issue"],
    "properties": {
        "slots": _assignments(SLOT_PATHS),
        "unexpressed": {"type": "array", "items": {"type": "string"}},
        "cohorts": {
            "anyOf": [
                {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["label", "slots"],
                        "properties": {
                            "label": {"type": "string"},
                            "slots": _assignments(COHORT_SLOT_PATHS),
                        },
                    },
                },
                {"type": "null"},
            ]
        },
        "issue": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "message"],
                    "properties": {
                        "kind": {"type": "string", "enum": [kind.value for kind in IssueKind]},
                        "message": {"type": "string"},
                    },
                },
                {"type": "null"},
            ]
        },
    },
}


def _values(enum: Iterable[object]) -> str:
    return ", ".join(str(getattr(member, "value", member)) for member in enum)


# One line per slot. A test checks that this covers exactly SLOT_PATHS.
SLOT_GUIDE: dict[str, str] = {
    "filters.conditions": "diseases or conditions named in the question",
    "filters.interventions": "drugs, biologics, devices or procedures named, spelled as "
    "written (brand or generic, no synonyms added)",
    "filters.sponsors": "sponsor organizations named",
    "filters.keywords": "terms the trial record should mention, including names whose type "
    "(drug, condition, sponsor) you cannot tell",
    "filters.phases": _values(Phase),
    "filters.statuses": _values(OverallStatus),
    "filters.sponsor_classes": _values(SponsorClass),
    "filters.intervention_types": _values(InterventionType),
    "filters.study_types": _values(StudyType),
    "filters.countries": "countries named, in English",
    "filters.start_year_from": "one year, the earliest trial start year (inclusive)",
    "filters.start_year_to": "one year, the latest trial start year (inclusive)",
    "analysis.kind": _values(AnalysisKind),
    "analysis.group_by": _values(Dimension),
    "analysis.series_by": _values(Dimension),
    "analysis.time_field": _values(TimeField),
    "analysis.granularity": _values(TimeGranularity),
    "analysis.measure": _values(Measure),
    "analysis.x": _values(Measure),
    "analysis.y": _values(Measure),
    "analysis.z": f"optional third measure for a scatter: {_values(Measure)}",
    "analysis.stages": "two or three of: phase, status, sponsor_class, lead_sponsor, "
    "study_type, start_year, in flow order",
    "analysis.node_types": f"exactly two of: {_values(Dimension)}",
    "presentation.chart_type": _values(ChartType),
    "presentation.sort": _values(SortOrder),
    "presentation.top_n": "one whole number",
    "presentation.citations_per_datum": "one whole number, only if the question asks",
    "retrieval.name_matching": "named or mentioned. Set mentioned only when the question asks "
    "for trials that mention a name anywhere, not just trials that list it as an "
    "intervention or condition",
}

_unexplained = set(SLOT_PATHS) ^ set(SLOT_GUIDE)
if _unexplained:  # a new plan field must be explained to the model before it ships
    raise RuntimeError(f"SLOT_GUIDE and SLOT_PATHS disagree on: {sorted(_unexplained)}")

SYSTEM_PROMPT = f"""\
You translate questions about clinical trials into slots of a query plan for the \
ClinicalTrials.gov registry. Another system validates your slots, merges them with the \
caller's structured fields, retrieves the trials, and computes every number. You never \
answer the question yourself; you only describe what to compute.

Output
- slots: one entry per slot the question settles, as {{"slot": name, "values": [...]}}. \
Leave out every slot the question does not settle. Values are always strings, even years.
- cohorts: for comparisons only, otherwise null. Each cohort has a short label and the \
filter slots that define only that group.
- unexpressed: every constraint in the question that no slot can express, in a few words \
each. These are constraints that need a number comparison ("more than 500 participants"), \
an exclusion ("not type 1"), or several values together in one trial ("in combination \
with chemotherapy"), since values in one slot are alternatives. A term that keywords can \
carry is not unexpressed. Never drop a constraint silently. Otherwise an empty list.
- issue: null unless one of the issues below applies.

Slots
{chr(10).join(f"- {path}: {guide}" for path, guide in SLOT_GUIDE.items())}

Filters
- Values inside one slot are alternatives; separate slots must all hold.
- Only include names the question actually mentions. Use keywords for a name whose type \
you cannot tell, and for any other term the matching trials should mention, such as \
"vaccine" or "pediatric". Keywords match text anywhere in a trial record. Separate \
keywords are alternatives, so terms that must all appear go in one keyword, separated by \
spaces ("pediatric vaccine").
- Years bound the trial start date and are inclusive. "since 2015" means from 2015; \
"after 2015" means from 2016; "before 2020" means to 2019; "in 2020" means from 2020 to \
2020; "the last N years" means the N calendar years ending with the current year.
- "ongoing" or "active" trials means NOT_YET_RECRUITING, RECRUITING, \
ENROLLING_BY_INVITATION and ACTIVE_NOT_RECRUITING. "recruiting" alone means RECRUITING.
- A phase written as "1/2" or "II/III" means both phases.

Analysis: set analysis.kind when the question implies one.
- count: a single number ("how many").
- distribution: counts per value of group_by, optionally split by series_by.
- time_series: counts over time. time_field is start_date unless the question is about \
when trials completed. granularity is year unless it asks for months. series_by is optional.
- comparison: two or more named groups side by side, described in cohorts. Constraints \
shared by every group go in the top-level filter slots. group_by is optional (use \
start_year for "per year" comparisons).
- histogram: the spread of one numeric measure.
- scatter: one point per trial with measures x and y.
- network: node_types is a pair of dimensions linked when they share a trial. Use the \
same dimension twice for co-occurrence, such as drugs used together.
- flow: trials moving through two or three dimensions in order, such as sponsor class \
to phase to status ("how do industry phase 3 trials end up"). Set stages.
- trial_list: the individual trials, for "which trials" or a timeline of trials.
- histogram also takes group_by to compare the spread across groups (enrollment by \
phase). scatter also takes z, a third measure.
Leave a parameter out when the question does not settle it. Never guess a grouping.

Presentation: include only what the question asks for ("top 10" sets top_n).

Caller's fields
The caller may already have set some slots. Treat them as binding context, and use them \
to resolve references such as "this drug". Do not repeat a field in your output unless \
the question itself states that value. If the question states a value that contradicts \
a field, output the question's value; the system keeps the field and tells the caller.

Issues
- ambiguous_reference: the question refers to something it never names ("this drug") \
and the caller's fields do not supply it.
- out_of_scope: registry records cannot answer it, for example questions about efficacy \
rankings, prices, approvals, or anything unrelated to clinical trials.
When you set an issue, still fill any slots you can.
"""


def proposal_from_output(data: Mapping[str, Any]) -> dict[str, dict[str, object]]:
    """Turn the model's assignment lists into the proposal shape the resolver reads.

    Repeated assignments to one slot are combined. A single-valued slot given several
    values is passed through as a list, so validation rejects it with a note instead of
    this code silently picking one.
    """
    proposal: dict[str, dict[str, object]] = {section: {} for section in PLAN_SECTIONS}
    for path, value in _combine(data.get("slots") or []).items():
        section, name = path.split(".", 1)
        proposal[section][name] = value
    cohorts = data.get("cohorts")
    if cohorts:
        proposal["analysis"]["cohorts"] = [
            {
                "label": cohort.get("label"),
                "filters": {
                    path.removeprefix("filters."): value
                    for path, value in _combine(cohort.get("slots") or []).items()
                },
            }
            for cohort in cohorts
        ]
    return proposal


def _combine(assignments: Iterable[Mapping[str, Any]]) -> dict[str, object]:
    collected: dict[str, list[str]] = {}
    for assignment in assignments:
        collected.setdefault(assignment["slot"], []).extend(assignment["values"])
    return {path: _shaped(path, values) for path, values in collected.items() if values}


def _shaped(path: str, values: list[str]) -> object:
    shape = SLOT_SHAPES[path]
    if shape == "list" or len(values) != 1:
        return values
    value = values[0].strip()
    if shape == "integer" and value.lstrip("-").isdigit():
        return int(value)
    return value


class _CallFailed(InterpreterUnavailable):
    def __init__(self, reason: str, *, possibly_billed: bool = False) -> None:
        super().__init__(reason)
        self.possibly_billed = possibly_billed


class MessagesClient(Protocol):
    """The slice of the Anthropic SDK this interpreter uses, so tests can substitute it."""

    async def create(self, **kwargs: Any) -> Any: ...


class ClaudeInterpreter:
    def __init__(
        self,
        messages: MessagesClient,
        ledger: SpendLedger,
        *,
        model: str,
        effort: str,
        max_tokens: int,
    ) -> None:
        self._messages = messages
        self._ledger = ledger
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens

    @classmethod
    def from_api_key(
        cls, api_key: str, ledger: SpendLedger, *, model: str, effort: str, max_tokens: int
    ) -> ClaudeInterpreter:
        client = anthropic.AsyncAnthropic(api_key=api_key, timeout=60.0, max_retries=2)
        # The SDK's heavily overloaded `create` is structurally compatible with the narrow
        # protocol this class needs, but too detailed for the type checker to match.
        messages = cast(MessagesClient, client.beta.messages)
        return cls(messages, ledger, model=model, effort=effort, max_tokens=max_tokens)

    @property
    def name(self) -> str:
        return f"claude:{self._model}"

    async def interpret(self, request: QueryRequest, today: date) -> Interpretation:
        prices = prices_for(self._model)
        if prices is None:
            raise InterpreterUnavailable(
                f"no price is known for {self._model}, so its spend cannot be bounded"
            )
        user_message = build_user_message(request, today)
        worst_case = self._worst_case_cost(prices, user_message)
        try:
            await self._ledger.reserve(worst_case)
        except BudgetExceeded as error:
            raise InterpreterUnavailable(str(error)) from error

        try:
            response = await self._call(user_message)
        except _CallFailed as error:
            # A dropped connection may still have been billed, so it is charged in full.
            await self._ledger.settle(worst_case, worst_case if error.possibly_billed else 0.0)
            raise
        await self._ledger.settle(worst_case, _cost_of(response, prices))
        return self._read(response)

    async def _call(self, user_message: str) -> Any:
        try:
            return await self._messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[
                    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": user_message}],
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                },
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as error:
            raise _CallFailed("the Anthropic API key was rejected") from error
        except anthropic.RateLimitError as error:
            raise _CallFailed("the Anthropic API is rate limiting requests") from error
        except anthropic.APIStatusError as error:
            raise _CallFailed(f"the Anthropic API returned {error.status_code}") from error
        except anthropic.APIConnectionError as error:
            raise _CallFailed(
                "the Anthropic API could not be reached", possibly_billed=True
            ) from error

    def _read(self, response: Any) -> Interpretation:
        if response.stop_reason == "refusal":
            raise InterpreterUnavailable("the model declined to read the question")
        if response.stop_reason == "max_tokens":
            raise InterpreterUnavailable("the model's output was cut off")
        text = next((block.text for block in response.content if block.type == "text"), None)
        try:
            data = json.loads(text) if text is not None else None
        except json.JSONDecodeError as error:
            raise InterpreterUnavailable("the model returned unreadable output") from error
        if not isinstance(data, dict):
            raise InterpreterUnavailable("the model returned no proposal")

        issue = data.get("issue")
        return Interpretation(
            interpreter=self.name,
            proposal=proposal_from_output(data),
            issue=InterpreterIssue(IssueKind(issue["kind"]), issue["message"]) if issue else None,
            unexpressed=tuple(str(item) for item in data.get("unexpressed") or ()),
        )

    def _worst_case_cost(self, prices: TokenPrices, user_message: str) -> float:
        prompt_chars = len(SYSTEM_PROMPT) + len(user_message) + len(json.dumps(OUTPUT_SCHEMA))
        return prices.cost(
            input_tokens=prompt_chars // _CHARS_PER_TOKEN_BOUND + 1,
            output_tokens=self._max_tokens,
        )


def build_user_message(request: QueryRequest, today: date) -> str:
    fields = request.model_dump(
        mode="json", include={"filters", "analysis", "presentation"}, exclude_none=True
    )
    fields = {section: values for section, values in fields.items() if values}
    return (
        f"Today's date: {today.isoformat()}\n\n"
        f"Caller's fields:\n{json.dumps(fields, indent=2) if fields else 'none'}\n\n"
        f"<question>\n{request.query}\n</question>"
    )


def _cost_of(response: Any, prices: TokenPrices) -> float:
    usage = response.usage
    return prices.cost(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
    )
