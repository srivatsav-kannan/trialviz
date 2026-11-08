"""Chart titles written from the plan, deterministically.

The title names what was counted and how it was grouped, in the plan's own terms
("Recruiting phase 3 melanoma trials by country"). Filters that do not fit naturally in
the title (countries, years, sponsor and study types) go in the subtitle, so no applied
constraint goes unstated.
"""

from __future__ import annotations

from trialviz.query.filters import Filters
from trialviz.query.plan import (
    ComparisonSpec,
    CountSpec,
    DistributionSpec,
    FlowSpec,
    HistogramSpec,
    NetworkSpec,
    Plan,
    ScatterSpec,
    TimeSeriesSpec,
    TrialListSpec,
)
from trialviz.vocabulary import OverallStatus, TimeField


def _join(items: list[str], word: str = "or") -> str:
    if len(items) <= 2:
        return f" {word} ".join(items)
    return ", ".join(items[:-1]) + f" {word} {items[-1]}"


def subject(filters: Filters) -> str:
    """Names what was counted, such as "Recruiting phase 3 melanoma trials of pembrolizumab".

    Conditions and keywords describe the trials, and drugs follow "of". Filters on
    different fields must all hold, so they are never joined with "or".
    """
    words = []
    if filters.statuses:
        statuses = [s.label.lower() for s in filters.statuses]
        if set(filters.statuses) >= {
            OverallStatus.RECRUITING,
            OverallStatus.NOT_YET_RECRUITING,
            OverallStatus.ENROLLING_BY_INVITATION,
            OverallStatus.ACTIVE_NOT_RECRUITING,
        }:
            statuses = ["ongoing"]
        words.append(_join(statuses))
    if filters.phases:
        words.append(_join([p.label for p in filters.phases]))
    if filters.conditions:
        words.append(_join(filters.conditions))
    if filters.keywords:
        words.append(_join(filters.keywords))
    words.append("trials")
    if filters.interventions:
        words.append(f"of {_join(filters.interventions)}")
    text = " ".join(words)
    return text[0].upper() + text[1:]


def subtitle(filters: Filters) -> str | None:
    parts = []
    if filters.sponsors:
        parts.append(f"Led by {_join(filters.sponsors)}")
    if filters.sponsor_classes:
        parts.append(f"{_join([c.label for c in filters.sponsor_classes])} sponsors")
    if filters.countries:
        parts.append(f"With sites in {_join([c.strip() for c in filters.countries])}")
    if filters.intervention_types:
        types = _join([t.label.lower() for t in filters.intervention_types])
        parts.append(f"{types[0].upper()}{types[1:]} interventions")
    if filters.study_types:
        parts.append(_join([t.label for t in filters.study_types]))
    low, high = filters.start_year_from, filters.start_year_to
    if low and high:
        parts.append(f"Started {low}" if low == high else f"Started {low} to {high}")
    elif low:
        parts.append(f"Started {low} or later")
    elif high:
        parts.append(f"Started {high} or earlier")
    return ". ".join(parts) + "." if parts else None


def title(plan: Plan) -> str:
    what = subject(plan.filters)
    analysis = plan.analysis
    match analysis:
        case CountSpec():
            return f"Number of {what[0].lower()}{what[1:]}"
        case DistributionSpec():
            by = analysis.group_by.label.lower()
            series = f" and {analysis.series_by.label.lower()}" if analysis.series_by else ""
            return f"{what} by {by}{series}"
        case TimeSeriesSpec():
            per = "month" if analysis.granularity.value == "month" else "year"
            event = "started" if analysis.time_field is TimeField.START_DATE else "completed"
            series = f", by {analysis.series_by.label.lower()}" if analysis.series_by else ""
            return f"{what} {event} per {per}{series}"
        case ComparisonSpec():
            cohorts = " vs ".join(c.label for c in analysis.cohorts)
            by = f" by {analysis.group_by.label.lower()}" if analysis.group_by else ""
            return f"{cohorts}: {what[0].lower()}{what[1:]}{by}"
        case HistogramSpec():
            by = f" by {analysis.group_by.label.lower()}" if analysis.group_by else ""
            return f"{analysis.measure.label} of {what[0].lower()}{what[1:]}{by}"
        case ScatterSpec():
            measures = [m.label.lower() for m in (analysis.x, analysis.y, analysis.z) if m]
            listed = _join(measures, "and")
            return f"{listed[0].upper()}{listed[1:]} of {what[0].lower()}{what[1:]}"
        case NetworkSpec():
            first, second = analysis.node_types
            pair = (
                f"{first.label.lower()}s used together"
                if first == second
                else f"{first.label.lower()}s and {second.label.lower()}s"
            )
            return f"Network of {pair} in {what[0].lower()}{what[1:]}"
        case FlowSpec():
            return f"{what}: {' to '.join(s.label.lower() for s in analysis.stages)}"
        case TrialListSpec():
            return what
    return what
