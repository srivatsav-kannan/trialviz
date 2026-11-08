"""Exact group counts for result sets too large to fetch whole.

When a cohort has more matches than the record cap, its records are a subset and group
counts computed from them are estimates. For dimensions whose groups the registry can
filter on directly, each displayed group is instead counted by the registry itself: the
cohort's query plus one clause for the group. The records still supply the citations.

Groups are matched exactly as aggregation defines them. "Phase 2" means phase 2 and no
other phase, so it excludes Phase 1/2 trials, which have their own group. A group of
trials with no value uses the registry's MISSING operator.

The number of extra count requests is capped. If a chart needs more than that, its
counts stay as computed from the subset, which the subset note already says.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from trialviz.aggregation.build import Built
from trialviz.aggregation.datasets import Categorical, Group, Matrix, TimeSeries
from trialviz.aggregation.extract import (
    NO_PHASE,
    NO_PHASE_LABEL,
    NOT_REPORTED,
    NOT_REPORTED_LABEL,
    Value,
    phase_label,
    sort_key,
)
from trialviz.query.plan import ComparisonSpec, DistributionSpec, Plan, TimeSeriesSpec
from trialviz.query.resolution import Note, NoteCode
from trialviz.retrieval.retriever import CohortRecords, RequestLog, Retrieval
from trialviz.retrieval.translate import RegistryQuery
from trialviz.vocabulary import (
    Dimension,
    InterventionType,
    OverallStatus,
    Phase,
    SponsorClass,
    StudyType,
    TimeField,
    TimeGranularity,
)

COUNT_BUDGET = 30
"""Most extra count requests one answer may make (about 36 seconds at the polite rate)."""

Count = Callable[[RegistryQuery, RequestLog], Awaitable[int]]
FirstYear = Callable[[RegistryQuery, str, RequestLog], Awaitable[str | None]]

_AREAS = {
    Dimension.STATUS: "OverallStatus",
    Dimension.SPONSOR_CLASS: "LeadSponsorClass",
    Dimension.STUDY_TYPE: "StudyType",
    Dimension.INTERVENTION_TYPE: "InterventionType",
}


def group_clause(dimension: Dimension, key: str) -> str | None:
    """A registry clause matching exactly one aggregation group, or None if impossible."""
    if dimension is Dimension.PHASE:
        # A missing phase means "no phase by design" for observational and expanded-access
        # studies and "left out" for interventional trials, as in aggregation.
        if key == NOT_REPORTED:
            return "AREA[Phase]MISSING AND AREA[StudyType]INTERVENTIONAL"
        if key == NO_PHASE:
            return "AREA[Phase]MISSING AND NOT AREA[StudyType]INTERVENTIONAL"
        included = key.split("/")
        excluded = [p.value for p in Phase if p.value not in included]
        required = " AND ".join(f"AREA[Phase]{p}" for p in included)
        return f"{required} AND NOT AREA[Phase]({' OR '.join(excluded)})"
    if dimension in _AREAS:
        area = _AREAS[dimension]
        return f"AREA[{area}]MISSING" if key == NOT_REPORTED else f"AREA[{area}]{key}"
    if dimension is Dimension.COUNTRY:
        return (
            "AREA[LocationCountry]MISSING"
            if key == NOT_REPORTED
            else f'AREA[LocationCountry]"{key}"'
        )
    if dimension is Dimension.START_YEAR and key != NOT_REPORTED:
        return f"AREA[StartDate]RANGE[{key}-01-01, {key}-12-31]"
    return None


async def apply_exact_counts(
    plan: Plan,
    retrieval: Retrieval,
    built: Built,
    count: Count,
    first_year: FirstYear,
    log: RequestLog,
) -> None:
    """Replace subset-based counts with registry counts where possible, in place. Every
    request made is added to `log`, so the answer lists the calls behind its numbers."""
    truncated = [c for c in retrieval.cohorts if c.truncated]
    if not truncated:
        return
    original_periods = None
    if isinstance(built.dataset, TimeSeries) and isinstance(plan.analysis, TimeSeriesSpec):
        # The fetched subset is the most recent trials, so its years start too late. The
        # series must start at the earliest trial in the registry, not in the subset.
        area = _time_area(plan.analysis.time_field)
        earliest = await first_year(retrieval.cohorts[0].named.query, area, log)
        original_periods = list(built.dataset.periods)
        _extend_periods(built.dataset, earliest)
    if isinstance(built.dataset, Categorical) and isinstance(plan.analysis, DistributionSpec):
        # Groups too rare to appear in the subset would otherwise never be counted. For a
        # dimension with a known set of values, every value is counted, and empty ones dropped.
        _add_unseen_groups(built.dataset, plan.analysis.group_by)
    grid = None
    if isinstance(built.dataset, Matrix):
        # Every cell is counted, including combinations and known values the subset never
        # showed, so a rare sponsor class is not left at zero for one cohort.
        grid = (built.dataset.rows, built.dataset.columns, dict(built.dataset.cells))
        _complete_grid(built.dataset, plan)
    requests = _requests(plan, retrieval.cohorts, built)
    per_group = sum(2 if c.counts_mentions and c.mentions else 1 for _, c, _ in requests or [])
    if requests is None or per_group > COUNT_BUDGET:
        # Nothing is counted, so nothing is padded with zeros we cannot vouch for.
        if original_periods is not None and isinstance(built.dataset, TimeSeries):
            built.dataset.periods = original_periods
        if grid is not None and isinstance(built.dataset, Matrix):
            built.dataset.rows, built.dataset.columns, built.dataset.cells = grid
        return
    for group, cohort, clause in requests:
        total = await count(cohort.named.query.and_clause(clause), log)
        if cohort.counts_mentions and cohort.mentions:
            total += await count(cohort.mentions.query.and_clause(clause), log)
        group.exact_count = total
    if isinstance(built.dataset, Categorical):
        built.dataset.groups = [g for g in built.dataset.groups if g.count > 0]
    if isinstance(built.dataset, Matrix):
        _drop_empty(built.dataset, rows=isinstance(plan.analysis, DistributionSpec))
    # The counts no longer come from the subset, so the subset caveat no longer applies.
    built.notes[:] = [n for n in built.notes if n.code is not NoteCode.COMPUTED_FROM_SUBSET]
    built.notes.append(
        Note(
            code=NoteCode.COUNTS_EXACT_FROM_REGISTRY,
            message="Because only part of the matching trials could be fetched, each group's "
            "count was taken directly from the registry. Citations come from the fetched trials.",
        )
    )


def _requests(
    plan: Plan, cohorts: tuple[CohortRecords, ...], built: Built
) -> list[tuple[Group, CohortRecords, str]] | None:
    """The (group, cohort, clause) count requests a dataset needs, or None if any group
    cannot be counted exactly, since mixing exact and estimated bars would mislead."""
    analysis, dataset = plan.analysis, built.dataset
    single = cohorts[0]
    if isinstance(analysis, DistributionSpec) and isinstance(dataset, Categorical):
        return _all_or_none(
            (g, single, group_clause(analysis.group_by, g.key)) for g in dataset.groups
        )
    if (
        isinstance(analysis, DistributionSpec)
        and isinstance(dataset, Matrix)
        and analysis.series_by
    ):
        series_by = analysis.series_by
        return _all_or_none(
            (cell, single, _both(group_clause(analysis.group_by, r), group_clause(series_by, c)))
            for (r, c), cell in dataset.cells.items()
        )
    if isinstance(analysis, ComparisonSpec) and isinstance(dataset, Matrix) and analysis.group_by:
        by_label = {c.label: c for c in cohorts}
        group_by = analysis.group_by
        return _all_or_none(
            (cell, by_label[r], group_clause(group_by, c)) for (r, c), cell in dataset.cells.items()
        )
    if (
        isinstance(analysis, TimeSeriesSpec)
        and isinstance(dataset, TimeSeries)
        and analysis.granularity is TimeGranularity.YEAR
        and not analysis.series_by
    ):
        area = _time_area(analysis.time_field)
        return [
            (
                dataset.cells.setdefault((period, ""), Group(period, period)),
                single,
                f"AREA[{area}]RANGE[{period}-01-01, {period}-12-31]",
            )
            for period in dataset.periods
        ]
    return None


_PHASE_GROUPS = [
    "EARLY_PHASE1",
    "PHASE1",
    "PHASE1/PHASE2",
    "PHASE2",
    "PHASE2/PHASE3",
    "PHASE3",
    "PHASE4",
    "NA",
]

_KNOWN_VALUES: dict[Dimension, list[tuple[str, str]]] = {
    Dimension.PHASE: [(key, phase_label(key)) for key in _PHASE_GROUPS]
    + [(NO_PHASE, NO_PHASE_LABEL)],
    Dimension.STATUS: [(s.value, s.label) for s in OverallStatus],
    Dimension.SPONSOR_CLASS: [(c.value, c.label) for c in SponsorClass],
    Dimension.STUDY_TYPE: [(t.value, t.label) for t in StudyType],
    Dimension.INTERVENTION_TYPE: [(t.value, t.label) for t in InterventionType],
}


def _add_unseen_groups(dataset: Categorical, dimension: Dimension) -> None:
    known = _KNOWN_VALUES.get(dimension)
    if known is None:
        return
    present = {g.key for g in dataset.groups}
    dataset.groups += [Group(key, label) for key, label in known if key not in present]
    if NOT_REPORTED not in present:
        dataset.groups.append(Group(NOT_REPORTED, NOT_REPORTED_LABEL))
    dataset.groups.sort(key=lambda g: sort_key(dimension, Value(g.key, g.label)))


def _complete_grid(dataset: Matrix, plan: Plan) -> None:
    """Adds unseen known values along the dimensions the registry can count, then a cell
    for every row and column."""
    analysis = plan.analysis
    if isinstance(analysis, ComparisonSpec) and analysis.group_by:
        dataset.columns = _with_unseen(dataset.columns, analysis.group_by)
    elif isinstance(analysis, DistributionSpec) and analysis.series_by:
        dataset.rows = _with_unseen(dataset.rows, analysis.group_by)
        dataset.columns = _with_unseen(dataset.columns, analysis.series_by)
    for row, row_label in dataset.rows:
        for column, column_label in dataset.columns:
            dataset.cells.setdefault(
                (row, column), Group(f"{row}|{column}", f"{row_label}, {column_label}")
            )


def _with_unseen(values: list[tuple[str, str]], dimension: Dimension) -> list[tuple[str, str]]:
    known = _KNOWN_VALUES.get(dimension)
    if known is None:
        return values
    present = {key for key, _ in values}
    merged = values + [(key, label) for key, label in known if key not in present]
    return sorted(merged, key=lambda kv: sort_key(dimension, Value(*kv)))


def _drop_empty(dataset: Matrix, *, rows: bool) -> None:
    """Removes columns, and for a distribution rows, that the registry counted as empty."""

    def filled(row: str | None, column: str | None) -> bool:
        return any(
            cell.count > 0
            for (r, c), cell in dataset.cells.items()
            if (row is None or r == row) and (column is None or c == column)
        )

    dataset.columns = [(k, label) for k, label in dataset.columns if filled(None, k)]
    if rows:
        dataset.rows = [(k, label) for k, label in dataset.rows if filled(k, None)]
    keep = {(r, c) for r, _ in dataset.rows for c, _ in dataset.columns}
    dataset.cells = {key: cell for key, cell in dataset.cells.items() if key in keep}


def _time_area(time_field: TimeField) -> str:
    return "StartDate" if time_field is TimeField.START_DATE else "CompletionDate"


def _extend_periods(dataset: TimeSeries, earliest: str | None) -> None:
    if earliest is None or dataset.granularity is not TimeGranularity.YEAR or not dataset.periods:
        return
    first = min(int(earliest), int(dataset.periods[0]))
    dataset.periods = [str(y) for y in range(first, int(dataset.periods[-1]) + 1)]


def _both(first: str | None, second: str | None) -> str | None:
    return f"({first}) AND ({second})" if first and second else None


def _all_or_none(
    requests: Iterable[tuple[Group, CohortRecords, str | None]],
) -> list[tuple[Group, CohortRecords, str]] | None:
    collected = []
    for group, cohort, clause in requests:
        if clause is None:
            return None
        collected.append((group, cohort, clause))
    return collected
