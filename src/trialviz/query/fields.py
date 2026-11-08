"""Reusable field types for the request and plan models.

Each type pairs a canonical JSON shape with the parser that accepts loose spellings, so
`["Phase III", "2"]` validates to `["PHASE2", "PHASE3"]` whether a person or the language
model wrote it. Lists are deduplicated and ordered deterministically: enum lists follow
the enum's declared order, and free-text lists keep first-seen order.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, Field, StringConstraints

from trialviz.retrieval.essie import name_words
from trialviz.vocabulary import Phase, parse_country, parse_phases
from trialviz.vocabulary.analysis import (
    AnalysisKind,
    ChartType,
    Dimension,
    Measure,
    SortOrder,
    TimeField,
    TimeGranularity,
    parse_analysis_kind,
    parse_chart_type,
    parse_dimension,
    parse_measure,
    parse_sort_order,
    parse_time_field,
    parse_time_granularity,
)
from trialviz.vocabulary.trials import (
    InterventionType,
    OverallStatus,
    SponsorClass,
    StudyType,
    parse_intervention_type,
    parse_sponsor_class,
    parse_status,
    parse_study_type,
)

MAX_LIST_ITEMS = 20
EARLIEST_YEAR = 1900
LATEST_YEAR = 2100

_WHITESPACE = re.compile(r"\s+")


def _dedupe_names(names: list[str]) -> list[str]:
    """Collapse inner whitespace and drop case-insensitive duplicates, keeping first spellings."""
    seen: dict[str, str] = {}
    for name in names:
        tidy = _WHITESPACE.sub(" ", name)
        seen.setdefault(tidy.casefold(), tidy)
    return list(seen.values())


def _require_list(raw: object) -> object:
    if isinstance(raw, str):
        raise ValueError("expected a list of strings, got a single string")
    return raw


def _in_declared_order[E: StrEnum](enum: type[E]) -> Callable[[list[E]], list[E]]:
    def order(values: list[E]) -> list[E]:
        present = set(values)
        return [member for member in enum if member in present]

    return order


def _searchable(name: str) -> str:
    """Retrieval reduces names to plain search words, so a name that reduces to nothing
    ("???", or only a parenthetical such as "(CD)") would become an empty search and
    silently drop its filter. It is refused here, by the same function retrieval uses."""
    if not re.search(r"[^\W_]", name_words(name)):
        raise ValueError(f"{name!r} has no words to search for")
    return name


EntityName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    AfterValidator(_searchable),
]
"""A drug, condition, sponsor or search term exactly as written. Resolution against the
registry happens at retrieval time, not here."""

EntityList = Annotated[
    list[EntityName],
    BeforeValidator(_require_list),
    AfterValidator(_dedupe_names),
    Field(min_length=1, max_length=MAX_LIST_ITEMS),
]

PhaseList = Annotated[
    list[Phase],
    BeforeValidator(lambda raw: parse_phases(_require_list(raw))),
    Field(min_length=1, max_length=len(Phase)),
]


StatusList = Annotated[
    list[Annotated[OverallStatus, BeforeValidator(parse_status)]],
    BeforeValidator(_require_list),
    AfterValidator(_in_declared_order(OverallStatus)),
    Field(min_length=1, max_length=len(OverallStatus)),
]
SponsorClassList = Annotated[
    list[Annotated[SponsorClass, BeforeValidator(parse_sponsor_class)]],
    BeforeValidator(_require_list),
    AfterValidator(_in_declared_order(SponsorClass)),
    Field(min_length=1, max_length=len(SponsorClass)),
]
InterventionTypeList = Annotated[
    list[Annotated[InterventionType, BeforeValidator(parse_intervention_type)]],
    BeforeValidator(_require_list),
    AfterValidator(_in_declared_order(InterventionType)),
    Field(min_length=1, max_length=len(InterventionType)),
]
StudyTypeList = Annotated[
    list[Annotated[StudyType, BeforeValidator(parse_study_type)]],
    BeforeValidator(_require_list),
    AfterValidator(_in_declared_order(StudyType)),
    Field(min_length=1, max_length=len(StudyType)),
]

CountryList = Annotated[
    list[Annotated[str, BeforeValidator(parse_country)]],
    BeforeValidator(_require_list),
    AfterValidator(lambda names: list(dict.fromkeys(names))),
    Field(min_length=1, max_length=MAX_LIST_ITEMS),
]

Year = Annotated[int, Field(ge=EARLIEST_YEAR, le=LATEST_YEAR, strict=True)]

# Analysis and presentation values accept documented aliases too ("drug" for intervention,
# "trend" for time_series), so every enum in a request is parsed the same forgiving way.
AnalysisKindField = Annotated[AnalysisKind, BeforeValidator(parse_analysis_kind)]
DimensionField = Annotated[Dimension, BeforeValidator(parse_dimension)]
MeasureField = Annotated[Measure, BeforeValidator(parse_measure)]
TimeFieldField = Annotated[TimeField, BeforeValidator(parse_time_field)]
GranularityField = Annotated[TimeGranularity, BeforeValidator(parse_time_granularity)]
ChartTypeField = Annotated[ChartType, BeforeValidator(parse_chart_type)]
SortOrderField = Annotated[SortOrder, BeforeValidator(parse_sort_order)]
