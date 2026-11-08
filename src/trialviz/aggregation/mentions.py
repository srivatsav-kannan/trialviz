"""Quoting the sentence where a mention-only trial mentions its drug or condition.

The registry matched these trials through text, not through a naming field, so the
citation is a quote: the sentence that contains the term, and the field it sits in.

The term a trial mentions is not always the one asked about. A "Keytruda" search matches
descriptions that say "pembrolizumab". Synonyms are learned from the named trials of the
same cohort: a MeSH term that most of them share ("pembrolizumab" for Keytruda) is what
the registry expanded the name to.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from trialviz.aggregation.evidence import Citation, CitationKind, Study, cite, each, nct_id, one
from trialviz.aggregation.extract import shared_meshes
from trialviz.query.filters import Filters
from trialviz.query.plan import ComparisonSpec, Plan
from trialviz.retrieval.essie import sanitize_term
from trialviz.retrieval.retriever import CohortRecords, Retrieval

MAX_QUOTES = 10
MAX_QUOTE_LENGTH = 300

_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+|\n+")
_TEXT_FIELDS = (
    ("protocolSection.identificationModule.briefTitle", None),
    ("protocolSection.identificationModule.officialTitle", None),
    ("protocolSection.armsInterventionsModule.interventions", "description"),
    ("protocolSection.armsInterventionsModule.armGroups", "description"),
    ("protocolSection.descriptionModule.briefSummary", None),
    ("protocolSection.conditionsModule.keywords", None),
)


class MentionReport(BaseModel):
    """Trials that mention a cohort's drug or condition only in their descriptions."""

    model_config = ConfigDict(frozen=True)

    cohort: str | None = Field(description="The cohort's label in a comparison, or null.")
    total: int = Field(description="How many trials only mention the names.")
    counted: bool = Field(
        description="Whether they are counted in the chart (`retrieval.name_matching` is "
        "`mentioned`) or only reported here."
    )
    nct_ids: list[str] = Field(description="The mention-only trials that were fetched.")
    citations: list[Citation] = Field(
        description="Quoted sentences that show where each trial mentions the name."
    )


def mention_reports(plan: Plan, retrieval: Retrieval) -> list[MentionReport]:
    reports = []
    for cohort in retrieval.cohorts:
        mentions = cohort.mentions
        if mentions is None or not mentions.total:
            continue
        terms = _terms(_layers(plan, cohort), cohort)
        quotes = [q for study in mentions.studies[: MAX_QUOTES * 3] if (q := quote(study, terms))]
        reports.append(
            MentionReport(
                cohort=cohort.label,
                total=mentions.total,
                counted=cohort.counts_mentions,
                nct_ids=[nct_id(s) for s in mentions.studies],
                citations=quotes[:MAX_QUOTES],
            )
        )
    return reports


def _layers(plan: Plan, cohort: CohortRecords) -> list[Filters]:
    if isinstance(plan.analysis, ComparisonSpec):
        own = next(c.filters for c in plan.analysis.cohorts if c.label == cohort.label)
        return [plan.filters, own]
    return [plan.filters]


def _terms(layers: list[Filters], cohort: CohortRecords) -> list[str]:
    """The names asked for, plus MeSH terms most of the named trials share."""
    asked = [
        sanitize_term(name)
        for layer in layers
        for name in [*(layer.interventions or []), *(layer.conditions or [])]
    ]
    return [t for t in dict.fromkeys([*asked, *shared_meshes(list(cohort.named.studies))]) if t]


def quote(study: Study, terms: list[str]) -> Citation | None:
    """The first sentence, in field order, that contains any of the terms."""
    for path, item in _TEXT_FIELDS:
        fields = (
            list(each(study, path, item))
            if item or path.endswith("keywords")
            else [f for f in [one(study, path)] if f]
        )
        for found in fields:
            text = str(found.value)
            lowered = text.casefold()
            term = next((t for t in terms if t in lowered), None)
            if term is None:
                continue
            start = lowered.index(term)
            sentence = _sentence_around(text, start)
            return cite(study, found.path, sentence, CitationKind.QUOTE)
    return None


def _sentence_around(text: str, position: int) -> str:
    """The sentence containing `position`, cut to a window around it if very long. The
    result is always an exact substring of `text`, so the quote can be checked."""
    begin = 0
    for match in _SENTENCE_END.finditer(text):
        if match.end() <= position:
            begin = match.end()
        else:
            end = match.start()
            break
    else:
        end = len(text)
    if end - begin > MAX_QUOTE_LENGTH:
        begin = max(begin, position - MAX_QUOTE_LENGTH // 2)
        end = min(end, begin + MAX_QUOTE_LENGTH)
    return text[begin:end].strip()
