"""The final answer: a recommended visualization, its alternatives, and everything needed to
trust it.

`meta` carries the resolved plan with the source of every value, every note raised along
the way, how the chart was chosen (with the reason each alternative fits and each other
chart was ruled out), the trials that only mention the drug or condition, and the exact
registry requests behind the numbers.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from trialviz.aggregation.build import Built
from trialviz.aggregation.datasets import Categorical, Matrix, TimeSeries
from trialviz.aggregation.mentions import MentionReport, mention_reports
from trialviz.query.plan import ComparisonSpec, DistributionSpec, Plan, TimeSeriesSpec
from trialviz.query.resolution import ClarificationNeeded, Note, NoteCode, ResolvedQuery, Source
from trialviz.retrieval.retriever import Retrieval
from trialviz.visualization.recommend import facts_of, recommend
from trialviz.visualization.spec import View, Visualization, encoding_and_options, rows
from trialviz.visualization.titles import subtitle, title
from trialviz.visualization.vega import to_vega_lite
from trialviz.vocabulary import ChartType


class ChartOption(BaseModel):
    type: ChartType
    reason: str


class ChartChoice(BaseModel):
    recommended: ChartOption
    alternatives: list[ChartOption]
    ruled_out: list[ChartOption] = Field(description="Charts that cannot draw this data, and why.")


class CohortSummary(BaseModel):
    label: str | None
    total: int = Field(description="Exact number of matching trials the results describe.")
    fetched: int
    truncated: bool
    mentions: int | None = Field(None, description="Trials that only mention the names, if any.")


class AnswerMeta(BaseModel):
    source: Literal["clinicaltrials.gov"] = "clinicaltrials.gov"
    data_version: str = Field(description="The registry's data timestamp when this was answered.")
    interpreter: str | None
    plan: Plan
    provenance: dict[str, Source]
    notes: list[Note]
    cohorts: list[CohortSummary]
    chart_choice: ChartChoice
    mentions: list[MentionReport]
    registry_requests: list[str]


class Answer(BaseModel):
    status: Literal["resolved"] = "resolved"
    query: str
    visualization: Visualization
    alternatives: list[View]
    meta: AnswerMeta


QueryResponse = Annotated[Answer | ClarificationNeeded, Field(discriminator="status")]


def _single_valued(plan: Plan, built: Built) -> bool:
    """Whether every trial sits in exactly one group of what is drawn, so parts make a whole."""
    analysis, dataset = plan.analysis, built.dataset
    if isinstance(dataset, Categorical):
        return isinstance(analysis, DistributionSpec) and not analysis.group_by.multi_valued
    if isinstance(dataset, Matrix):
        if isinstance(analysis, DistributionSpec) and analysis.series_by:
            return not analysis.series_by.multi_valued
        if isinstance(analysis, ComparisonSpec) and analysis.group_by:
            return not analysis.group_by.multi_valued
        return False
    if isinstance(dataset, TimeSeries) and isinstance(analysis, TimeSeriesSpec):
        return analysis.series_by is None or not analysis.series_by.multi_valued
    return False


def present(query: str, resolution: ResolvedQuery, retrieval: Retrieval, built: Built) -> Answer:
    plan = resolution.plan
    presentation = plan.presentation
    limit = presentation.citations_per_datum or 0
    notes = [*resolution.notes, *built.notes]

    facts = facts_of(built.dataset, presentation, _single_valued(plan, built))
    source = resolution.provenance.get("presentation.chart_type")
    asker = "the question" if source is Source.INFERRED else "the caller"
    choice = recommend(facts, presentation.chart_type, asker)
    if choice.preference_note:
        notes.append(
            Note(
                code=NoteCode.CHART_PREFERENCE_NOT_FITTING,
                message=choice.preference_note,
                slot="presentation.chart_type",
            )
        )

    data = rows(built.dataset, choice.recommended.chart, limit, presentation.sort)
    top_n = presentation.top_n
    unordered = isinstance(built.dataset, Categorical) and not built.dataset.ordered
    if top_n and unordered and isinstance(data, list) and len(data) > top_n:
        notes.append(
            Note(
                code=NoteCode.TOP_N_APPLIED,
                message=f"Showing the {top_n} largest of {len(data)} groups.",
                slot="presentation.top_n",
            )
        )
        data = sorted(data, key=lambda r: -r["trial_count"])[:top_n]

    heading, details = title(plan), subtitle(plan.filters)
    encoding, options = encoding_and_options(choice.recommended.chart, built.dataset, data)
    visualization = Visualization(
        type=choice.recommended.chart,
        reason=choice.recommended.reason,
        title=heading,
        subtitle=details,
        encoding=encoding,
        options=options,
        data=data,
        vega_lite=to_vega_lite(choice.recommended.chart, encoding, data, heading, options),
    )

    alternatives = []
    for alternative in choice.alternatives:
        own = rows(built.dataset, alternative.chart, limit, presentation.sort)
        if isinstance(own, list) and isinstance(data, list) and len(own) > len(data):
            own = [r for r in own if r in data] or own
        view_data = own if own != data else data
        alt_encoding, alt_options = encoding_and_options(
            alternative.chart, built.dataset, view_data
        )
        alternatives.append(
            View(
                type=alternative.chart,
                reason=alternative.reason,
                encoding=alt_encoding,
                options=alt_options,
                data=None if view_data is data or view_data == data else view_data,
                vega_lite=to_vega_lite(
                    alternative.chart, alt_encoding, view_data, heading, alt_options
                ),
            )
        )

    return Answer(
        query=query,
        visualization=visualization,
        alternatives=alternatives,
        meta=AnswerMeta(
            data_version=retrieval.data_version,
            interpreter=resolution.interpreter,
            plan=plan,
            provenance=resolution.provenance,
            notes=notes,
            cohorts=[
                CohortSummary(
                    label=c.label,
                    total=c.total,
                    fetched=len(c.studies),
                    truncated=c.truncated,
                    mentions=c.mentions.total if c.mentions else None,
                )
                for c in retrieval.cohorts
            ],
            chart_choice=ChartChoice(
                recommended=ChartOption(
                    type=choice.recommended.chart, reason=choice.recommended.reason
                ),
                alternatives=[
                    ChartOption(type=a.chart, reason=a.reason) for a in choice.alternatives
                ],
                ruled_out=[ChartOption(type=r.chart, reason=r.reason) for r in choice.ruled_out],
            ),
            mentions=mention_reports(plan, retrieval),
            registry_requests=[url for c in retrieval.cohorts for url in c.requests],
        ),
    )
