"""Building a dataset from a plan and its fetched records. Pure and deterministic.

Each analysis kind becomes one dataset shape. Notes explain anything about the numbers a
reader could otherwise misjudge: trials counted in several groups, a subset of a large
result set, trials with no value for a measure, a pruned network, mentions.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations, pairwise

from trialviz.aggregation.datasets import (
    Bin,
    Categorical,
    Dataset,
    Distribution,
    Edge,
    Flow,
    FlowLink,
    FlowNode,
    Graph,
    Group,
    Matrix,
    Node,
    Point,
    Points,
    Scalar,
    Spread,
    TimeSeries,
    TrialRow,
    Trials,
    Value,
)
from trialviz.aggregation.evidence import Citation, Study, cite, each, nct_id, one, title
from trialviz.aggregation.extract import (
    DIMENSIONS,
    MEASURES,
    NOT_REPORTED,
    ORDERED_DIMENSIONS,
    arm_drug_sets,
    sort_key,
    time_period,
)
from trialviz.aggregation.extract import Value as DimensionValue
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
from trialviz.query.resolution import Note, NoteCode
from trialviz.retrieval.retriever import CohortRecords, Retrieval
from trialviz.vocabulary import Dimension, Measure, TimeField, TimeGranularity

DEFAULT_NETWORK_NODES = 40
MAX_NETWORK_EDGES = 300


@dataclass
class Built:
    dataset: Dataset
    notes: list[Note] = field(default_factory=list)


def build(plan: Plan, retrieval: Retrieval) -> Built:
    built = _dataset(plan, retrieval)
    # A count fetches only the records it cites, so its "subset" is by design, not a caveat.
    subset = [] if isinstance(plan.analysis, CountSpec) else _subset_notes(retrieval, plan)
    built.notes[:0] = subset + _mention_notes(retrieval)
    return built


def _dataset(plan: Plan, retrieval: Retrieval) -> Built:
    analysis = plan.analysis
    studies = list(retrieval.cohorts[0].studies)
    match analysis:
        case CountSpec():
            return _scalar(plan.filters, retrieval.cohorts[0])
        case DistributionSpec() if analysis.series_by:
            return _matrix_by_dimensions(studies, analysis.group_by, analysis.series_by)
        case DistributionSpec():
            return _categorical(studies, analysis.group_by)
        case TimeSeriesSpec():
            return _time_series(
                studies, analysis.time_field, analysis.granularity, analysis.series_by
            )
        case ComparisonSpec() if analysis.group_by:
            return _cohort_matrix(retrieval.cohorts, analysis.group_by)
        case ComparisonSpec():
            return _cohort_totals(retrieval.cohorts)
        case HistogramSpec():
            return _distribution(studies, analysis.measure, analysis.group_by)
        case ScatterSpec():
            return _points(studies, analysis.x, analysis.y, analysis.z, analysis.series_by)
        case NetworkSpec():
            limit = plan.presentation.top_n or DEFAULT_NETWORK_NODES
            return _graph(studies, analysis.node_types, limit)
        case FlowSpec():
            return _flow(studies, analysis.stages)
        case TrialListSpec():
            return _trials(studies)
    raise AssertionError(f"no dataset builder for {analysis.kind}")


def _field(dimension: Dimension) -> str:
    return dimension.value


def _several_note(dimension: Dimension, trials: int) -> list[Note]:
    if not dimension.multi_valued:
        return []
    return [
        Note(
            code=NoteCode.COUNTED_IN_SEVERAL_GROUPS,
            message=f"A trial with several values for {dimension.label.lower()} is counted once "
            f"under each, so the groups can add up to more than the {trials} trials.",
        )
    ]


def _groups(studies: Iterable[Study], dimension: Dimension) -> dict[str, Group]:
    groups: dict[str, Group] = {}
    for study in studies:
        identifier = nct_id(study)
        for value in DIMENSIONS[dimension](study):
            group = groups.setdefault(value.key, Group(value.key, value.label))
            group.add(identifier, value.citations)
    return groups


def _ordered(groups: dict[str, Group], dimension: Dimension) -> list[Group]:
    return sorted(
        groups.values(),
        key=lambda g: sort_key(dimension, DimensionValue(g.key, g.label)),
    )


# --- Counts ------------------------------------------------------------------------------


def _scalar(filters: Filters, cohort: CohortRecords) -> Built:
    citations = [c for study in cohort.studies for c in filter_citations(study, filters)[:1]]
    return Built(Scalar("Trials", cohort.total, citations))


def filter_citations(study: Study, filters: Filters) -> list[Citation]:
    """Why a trial matched: the field behind each active filter, in filter order."""
    found = []
    checks: list[tuple[object, str, str | None]] = [
        (filters.interventions, "derivedSection.interventionBrowseModule.meshes", "term"),
        (filters.conditions, "derivedSection.conditionBrowseModule.meshes", "term"),
        (filters.phases, "protocolSection.designModule.phases", None),
        (filters.statuses, "protocolSection.statusModule.overallStatus", None),
        (filters.countries, "protocolSection.contactsLocationsModule.locations", "country"),
        (filters.sponsors, "protocolSection.sponsorCollaboratorsModule.leadSponsor.name", None),
        (
            filters.sponsor_classes,
            "protocolSection.sponsorCollaboratorsModule.leadSponsor.class",
            None,
        ),
        (
            filters.start_year_from or filters.start_year_to,
            "protocolSection.statusModule.startDateStruct.date",
            None,
        ),
    ]
    for active, path, item in checks:
        if not active:
            continue
        if item:
            wanted = {str(v).casefold() for v in active} if isinstance(active, list) else set()
            items = list(each(study, path, item))
            match = next(
                (i for i in items if str(i.value).casefold() in wanted), items[0] if items else None
            )
            if match:
                found.append(match.citation(study))
        elif (value := one(study, path)) is not None:
            found.append(value.citation(study))
    return found or [cite(study, "protocolSection.identificationModule.briefTitle", title(study))]


def _categorical(studies: list[Study], dimension: Dimension) -> Built:
    groups = _ordered(_groups(studies, dimension), dimension)
    dataset = Categorical(
        dimension=dimension.label,
        field=_field(dimension),
        groups=groups,
        ordered=dimension in ORDERED_DIMENSIONS,
        geographic=dimension is Dimension.COUNTRY,
    )
    return Built(dataset, _several_note(dimension, len(studies)))


def _cohort_totals(cohorts: Sequence[CohortRecords]) -> Built:
    groups = []
    for cohort in cohorts:
        group = Group(cohort.label or "", cohort.label or "")
        for study in cohort.studies:
            group.add(
                nct_id(study),
                (cite(study, "protocolSection.identificationModule.briefTitle", title(study)),),
            )
        group.exact_count = cohort.total
        groups.append(group)
    return Built(Categorical("Cohort", "cohort", groups, ordered=False))


def _matrix(
    rows: list[tuple[str, str]],
    columns: list[tuple[str, str]],
    cells: dict[tuple[str, str], Group],
    labels: tuple[str, str, str, str],
    ordered: tuple[bool, bool],
) -> Matrix:
    return Matrix(labels[0], labels[1], labels[2], labels[3], rows, columns, cells, *ordered)


def _matrix_by_dimensions(studies: list[Study], rows_by: Dimension, columns_by: Dimension) -> Built:
    cells: dict[tuple[str, str], Group] = {}
    row_values: dict[str, DimensionValue] = {}
    column_values: dict[str, DimensionValue] = {}
    for study in studies:
        identifier = nct_id(study)
        for row in DIMENSIONS[rows_by](study):
            row_values.setdefault(row.key, row)
            for column in DIMENSIONS[columns_by](study):
                column_values.setdefault(column.key, column)
                cell = cells.setdefault(
                    (row.key, column.key),
                    Group(f"{row.key}|{column.key}", f"{row.label}, {column.label}"),
                )
                cell.add(identifier, row.citations + column.citations)
    rows = [
        (v.key, v.label) for v in sorted(row_values.values(), key=lambda v: sort_key(rows_by, v))
    ]
    columns = [
        (v.key, v.label)
        for v in sorted(column_values.values(), key=lambda v: sort_key(columns_by, v))
    ]
    matrix = _matrix(
        rows,
        columns,
        cells,
        (rows_by.label, _field(rows_by), columns_by.label, _field(columns_by)),
        (rows_by in ORDERED_DIMENSIONS, columns_by in ORDERED_DIMENSIONS),
    )
    notes = _several_note(rows_by, len(studies)) + _several_note(columns_by, len(studies))
    return Built(matrix, notes)


def _cohort_matrix(cohorts: Sequence[CohortRecords], columns_by: Dimension) -> Built:
    cells: dict[tuple[str, str], Group] = {}
    column_values: dict[str, DimensionValue] = {}
    rows = []
    for cohort in cohorts:
        row = cohort.label or ""
        rows.append((row, row))
        for study in cohort.studies:
            for column in DIMENSIONS[columns_by](study):
                column_values.setdefault(column.key, column)
                cell = cells.setdefault(
                    (row, column.key), Group(f"{row}|{column.key}", f"{row}, {column.label}")
                )
                cell.add(nct_id(study), column.citations)
    columns = [
        (v.key, v.label)
        for v in sorted(column_values.values(), key=lambda v: sort_key(columns_by, v))
    ]
    matrix = _matrix(
        rows,
        columns,
        cells,
        ("Cohort", "cohort", columns_by.label, _field(columns_by)),
        (False, columns_by in ORDERED_DIMENSIONS),
    )
    trials = sum(len(c.studies) for c in cohorts)
    return Built(matrix, _several_note(columns_by, trials))


# --- Time --------------------------------------------------------------------------------


def _time_series(
    studies: list[Study],
    time_field: TimeField,
    granularity: TimeGranularity,
    series_by: Dimension | None,
) -> Built:
    monthly = granularity is TimeGranularity.MONTH
    path_key = "start" if time_field is TimeField.START_DATE else "completion"
    cells: dict[tuple[str, str], Group] = {}
    series_values: dict[str, DimensionValue] = {}
    undated = 0
    for study in studies:
        period = time_period(study, path_key, monthly)
        if period is None:
            undated += 1
            continue
        date_citation = cite(study, period.path, one(study, period.path).value)  # type: ignore[union-attr]
        values = DIMENSIONS[series_by](study) if series_by else [DimensionValue("", "")]
        for value in values:
            series_values.setdefault(value.key, value)
            cell = cells.setdefault(
                (str(period.value), value.key), Group(str(period.value), str(period.value))
            )
            cell.add(nct_id(study), (date_citation, *value.citations))
    periods = _continuous_periods({p for p, _ in cells}, monthly)
    series = (
        [
            (v.key, v.label)
            for v in sorted(series_values.values(), key=lambda v: sort_key(series_by, v))
        ]
        if series_by
        else []
    )
    dataset = TimeSeries(
        granularity=granularity,
        time_label="Start" if time_field is TimeField.START_DATE else "Completion",
        periods=periods,
        series_dimension=series_by.label if series_by else None,
        series_field=_field(series_by) if series_by else None,
        series=series,
        cells=cells,
    )
    notes = _missing_note(undated, f"no {dataset.time_label.lower()} date") + (
        _several_note(series_by, len(studies)) if series_by else []
    )
    return Built(dataset, notes)


def _continuous_periods(present: set[str], monthly: bool) -> list[str]:
    """Every period from the first to the last, so gaps show as zeros, not as missing."""
    if not present:
        return []
    if not monthly:
        years = [int(p) for p in present]
        return [str(y) for y in range(min(years), max(years) + 1)]
    first, last = min(present), max(present)
    year, month = int(first[:4]), int(first[5:7])
    periods = []
    while f"{year:04d}-{month:02d}" <= last:
        periods.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return periods


def _missing_note(count: int, reason: str) -> list[Note]:
    if not count:
        return []
    return [
        Note(
            code=NoteCode.VALUES_MISSING,
            message=f"{count} trials were left out because they have {reason}.",
        )
    ]


# --- Measures ----------------------------------------------------------------------------


def _distribution(studies: list[Study], measure: Measure, group_by: Dimension | None) -> Built:
    spreads: dict[str, Spread] = {}
    excluded = 0
    for study in studies:
        measured = MEASURES[measure](study)
        if measured is None:
            excluded += 1
            continue
        groups = DIMENSIONS[group_by](study) if group_by else [DimensionValue("", "All trials")]
        for group in groups:
            spread = spreads.setdefault(group.key, Spread(group.key, group.label, []))
            spread.values.append(Value(nct_id(study), measured.value, measured.citations))
    ordered = (
        sorted(spreads.values(), key=lambda s: sort_key(group_by, DimensionValue(s.key, s.label)))
        if group_by
        else list(spreads.values())
    )
    values = [v for spread in ordered for v in spread.values]
    numbers = sorted(v.value for v in values)
    skewed = (
        bool(numbers)
        and statistics.median(numbers) > 0
        and numbers[-1] / statistics.median(numbers) > 20
    )
    dataset = Distribution(
        measure=measure,
        group_dimension=group_by.label if group_by else None,
        group_field=_field(group_by) if group_by else None,
        spreads=ordered,
        bins=_log_bins(values) if skewed else _bins(values),
        excluded=excluded,
        skewed=skewed,
    )
    return Built(dataset, _missing_note(excluded, f"no usable {measure.label.lower()}"))


def _bins(values: list[Value]) -> list[Bin]:
    """Equal-width bins with a "nice" width (1, 2 or 5 times a power of ten), chosen by the
    Freedman-Diaconis rule on the 1st to 99th percentile so a few huge trials do not flatten
    everything else. Values beyond that range go to the last bin, which is marked open-ended
    and keeps the normal width, so one huge trial does not stretch the axis."""
    if not values:
        return []
    numbers = sorted(v.value for v in values)
    if len(numbers) < 4 or numbers[0] == numbers[-1]:
        low, high = numbers[0], numbers[-1] or 1.0
        edges = [low, high if high > low else low + 1]
    else:
        q1, _, q3 = statistics.quantiles(numbers, n=4)
        p99 = numbers[min(len(numbers) - 1, math.ceil(0.99 * len(numbers)) - 1)]
        raw_width = 2 * (q3 - q1) / len(numbers) ** (1 / 3) or (p99 - numbers[0]) / 10 or 1.0
        width = _nice(max(raw_width, (p99 - numbers[0]) / 40))
        start = math.floor(numbers[0] / width) * width
        count = max(1, math.ceil((p99 - start) / width))
        edges = [start + i * width for i in range(count + 1)]
    bins = [
        Bin(
            edges[i],
            edges[i + 1],
            Group(f"{edges[i]:g}-{edges[i + 1]:g}", f"{edges[i]:g} to {edges[i + 1]:g}"),
        )
        for i in range(len(edges) - 1)
    ]
    if numbers[-1] >= bins[-1].end and len(bins) > 1:
        bins[-1].open_ended = True
        bins[-1].group.label = f"{bins[-1].start:g} or more"
    for value in values:
        index = next((i for i, b in enumerate(bins) if value.value < b.end), len(bins) - 1)
        bins[index].group.add(value.nct_id, value.citations)
    return bins


def _log_bins(values: list[Value]) -> list[Bin]:
    """Bins whose edges step 1, 2, 5 times each power of ten (0, 10, 20, 50, 100, ...), for
    heavily skewed measures such as enrollment, where equal widths put almost every trial
    in the first bin. Drawn on a log-like axis, each bin gets similar room."""
    numbers = sorted(v.value for v in values)
    p99 = numbers[min(len(numbers) - 1, math.ceil(0.99 * len(numbers)) - 1)]
    edges = [0.0]
    step = 1
    while edges[-1] < p99:
        for multiple in (1, 2, 5):
            edge = float(multiple * 10**step)
            edges.append(edge)
            if edge >= p99:
                break
        step += 1
    bins = [
        Bin(
            edges[i],
            edges[i + 1],
            Group(f"{edges[i]:g}-{edges[i + 1]:g}", f"{edges[i]:g} to {edges[i + 1]:g}"),
        )
        for i in range(len(edges) - 1)
    ]
    if numbers[-1] >= bins[-1].end and len(bins) > 1:
        bins[-1].open_ended = True
        bins[-1].group.label = f"{bins[-1].start:g} or more"
    for value in values:
        index = next((i for i, b in enumerate(bins) if value.value < b.end), len(bins) - 1)
        bins[index].group.add(value.nct_id, value.citations)
    return bins


def _nice(width: float) -> float:
    exponent = math.floor(math.log10(width))
    fraction = width / 10**exponent
    step = 1 if fraction <= 1 else 2 if fraction <= 2 else 5 if fraction <= 5 else 10
    return float(step * 10**exponent)


def _points(
    studies: list[Study], x: Measure, y: Measure, z: Measure | None, color_by: Dimension | None
) -> Built:
    points = []
    excluded = 0
    for study in studies:
        mx, my = MEASURES[x](study), MEASURES[y](study)
        mz = MEASURES[z](study) if z else None
        if mx is None or my is None or (z and mz is None):
            excluded += 1
            continue
        color = DIMENSIONS[color_by](study)[0].label if color_by else None
        citations = mx.citations + my.citations + (mz.citations if mz else ())
        points.append(
            Point(
                nct_id(study),
                title(study),
                mx.value,
                my.value,
                mz.value if mz else None,
                color,
                citations,
            )
        )
    dataset = Points(
        x,
        y,
        z,
        color_by.label if color_by else None,
        _field(color_by) if color_by else None,
        points,
        excluded,
    )
    measures = " and ".join(m.label.lower() for m in (x, y, z) if m)
    return Built(dataset, _missing_note(excluded, f"no usable {measures}"))


# --- Networks, flows, trial lists --------------------------------------------------------


def _graph(studies: list[Study], node_types: tuple[Dimension, Dimension], limit: int) -> Built:
    first, second = node_types
    nodes: dict[str, Node] = {}
    edges: dict[tuple[str, str], Group] = {}

    def node(dimension: Dimension, value: DimensionValue) -> str:
        identifier = f"{dimension.value}:{value.key}"
        if identifier not in nodes:
            nodes[identifier] = Node(
                identifier, value.label, dimension.value, Group(identifier, value.label)
            )
        return identifier

    def link(a: str, b: str, study: Study, citations: tuple[Citation, ...]) -> None:
        source, target = sorted((a, b))
        edges.setdefault((source, target), Group(f"{source}|{target}", "")).add(
            nct_id(study), citations
        )

    co_occurrence = first == second
    for study in studies:
        identifier = nct_id(study)
        if co_occurrence and first is Dimension.INTERVENTION:
            for given, arm_citation in arm_drug_sets(study):
                ids = []
                for drug in given:
                    value = DimensionValue(drug.key, drug.label, drug.citations)
                    nodes_id = node(first, value)
                    nodes[nodes_id].group.add(identifier, drug.citations)
                    ids.append(nodes_id)
                for a, b in combinations(sorted(set(ids)), 2):
                    link(a, b, study, (arm_citation,))
            continue
        left = [v for v in DIMENSIONS[first](study) if v.key != NOT_REPORTED]
        right = (
            left
            if co_occurrence
            else [v for v in DIMENSIONS[second](study) if v.key != NOT_REPORTED]
        )
        evidence: dict[str, tuple[Citation, ...]] = {}
        for dimension, values in ((first, left), (second, right)):
            for value in values:
                nid = node(dimension, value)
                nodes[nid].group.add(identifier, value.citations[:1])
                evidence[nid] = value.citations[:1]
        left_ids = [f"{first.value}:{v.key}" for v in left]
        right_ids = [f"{second.value}:{v.key}" for v in right]
        pairs = (
            combinations(sorted(set(left_ids)), 2)
            if co_occurrence
            else ((a, b) for a in left_ids for b in right_ids)
        )
        for a, b in pairs:
            link(a, b, study, evidence[a] + evidence[b])

    kept = sorted(nodes.values(), key=lambda n: (-n.group.count, n.label.casefold()))[:limit]
    kept_ids = {n.id for n in kept}
    kept_edges = sorted(
        (Edge(s, t, g) for (s, t), g in edges.items() if s in kept_ids and t in kept_ids),
        key=lambda e: (-e.group.count, e.source, e.target),
    )[:MAX_NETWORK_EDGES]
    for edge in kept_edges:
        for end in (edge.source, edge.target):
            nodes[end].degree += 1
    connected = [n for n in kept if n.degree > 0]
    _communities(connected, kept_edges)
    relation = (
        "drugs given together in the same treatment arm"
        if co_occurrence and first is Dimension.INTERVENTION
        else f"{first.label.lower()} and {second.label.lower()} in the same trial"
    )
    graph = Graph(
        node_types=(first.value, second.value),
        relation=relation,
        nodes=connected,
        edges=kept_edges,
        dropped_nodes=len(nodes) - len(connected),
        dropped_edges=len(edges) - len(kept_edges),
    )
    notes = []
    if graph.dropped_nodes or graph.dropped_edges:
        notes.append(
            Note(
                code=NoteCode.NETWORK_PRUNED,
                message=f"The network keeps the {len(connected)} most frequent entities that have "
                f"at least one link, and the {len(kept_edges)} strongest links. "
                f"{graph.dropped_nodes} entities and {graph.dropped_edges} links were left out.",
            )
        )
    return Built(graph, notes)


def _communities(nodes: list[Node], edges: list[Edge]) -> None:
    """Label propagation with a fixed visiting order, so clusters are reproducible."""
    labels = {n.id: i for i, n in enumerate(nodes)}
    neighbours: dict[str, Counter[str]] = {n.id: Counter() for n in nodes}
    for edge in edges:
        neighbours[edge.source][edge.target] += edge.group.count
        neighbours[edge.target][edge.source] += edge.group.count
    for _ in range(20):
        changed = False
        for n in nodes:
            weights: Counter[int] = Counter()
            for other, weight in neighbours[n.id].items():
                weights[labels[other]] += weight
            if weights:
                best = min(weights, key=lambda label: (-weights[label], label))
                if best != labels[n.id]:
                    labels[n.id], changed = best, True
        if not changed:
            break
    renumbered = {label: i for i, label in enumerate(dict.fromkeys(labels[n.id] for n in nodes))}
    for n in nodes:
        n.community = renumbered[labels[n.id]]


def _flow(studies: list[Study], stages: list[Dimension]) -> Built:
    nodes: dict[str, FlowNode] = {}
    links: dict[tuple[str, str], Group] = {}
    for study in studies:
        identifier = nct_id(study)
        path = []
        for index, stage in enumerate(stages):
            value = DIMENSIONS[stage](study)[0]
            node_id = f"{index}:{value.key}"
            nodes.setdefault(
                node_id, FlowNode(node_id, index, value.label, Group(node_id, value.label))
            )
            nodes[node_id].group.add(identifier, value.citations)
            path.append((node_id, value))
        for (a, va), (b, vb) in pairwise(path):
            links.setdefault((a, b), Group(f"{a}>{b}", "")).add(
                identifier, va.citations[:1] + vb.citations[:1]
            )

    def order(n: FlowNode) -> tuple[object, ...]:
        key = n.id.split(":", 1)[1]
        return (n.stage, sort_key(stages[n.stage], DimensionValue(key, n.label)))

    dataset = Flow(
        stages=[s.label for s in stages],
        nodes=sorted(nodes.values(), key=order),
        links=[FlowLink(a, b, g) for (a, b), g in sorted(links.items())],
    )
    return Built(dataset)


def _trials(studies: list[Study]) -> Built:
    rows = []
    undated = 0
    for study in studies:
        start = one(study, "protocolSection.statusModule.startDateStruct.date")
        end = one(study, "protocolSection.statusModule.completionDateStruct.date") or one(
            study, "protocolSection.statusModule.primaryCompletionDateStruct.date"
        )
        undated += start is None
        enrollment = MEASURES[Measure.ENROLLMENT](study)
        citations = tuple(f.citation(study) for f in (start, end) if f)
        rows.append(
            TrialRow(
                nct_id=nct_id(study),
                title=title(study),
                phase=DIMENSIONS[Dimension.PHASE](study)[0].label,
                status=DIMENSIONS[Dimension.STATUS](study)[0].label,
                sponsor=DIMENSIONS[Dimension.LEAD_SPONSOR](study)[0].label,
                start=str(start.value) if start else None,
                end=str(end.value) if end else None,
                enrollment=enrollment.value if enrollment else None,
                citations=citations,
            )
        )
    rows.sort(key=lambda r: (r.start is None, r.start or "", r.nct_id))
    return Built(Trials(rows, undated))


# --- Notes about the record sets ---------------------------------------------------------


def _subset_notes(retrieval: Retrieval, plan: Plan) -> list[Note]:
    notes = []
    # The subset is the most recently started trials, so a trend over start dates is
    # complete for recent years and short for early ones. That deserves saying outright.
    by_start = (
        isinstance(plan.analysis, TimeSeriesSpec)
        and plan.analysis.time_field is TimeField.START_DATE
    )
    for cohort in retrieval.cohorts:
        if not cohort.truncated:
            continue
        if by_start:
            notes.append(
                Note(
                    code=NoteCode.COMPUTED_FROM_SUBSET,
                    message=f"Only the {len(cohort.studies):,} most recently started of "
                    f"{cohort.total:,} matching trials were fetched, so recent years are complete "
                    "and earlier years are undercounted.",
                )
            )
            continue
        name = f"For {cohort.label}, results" if cohort.label else "Results"
        notes.append(
            Note(
                code=NoteCode.COMPUTED_FROM_SUBSET,
                message=f"{name} are computed from the {len(cohort.studies):,} most recently "
                f"started of {cohort.total:,} matching trials.",
            )
        )
    return notes


def _mention_notes(retrieval: Retrieval) -> list[Note]:
    notes = []
    for cohort in retrieval.cohorts:
        mentions = cohort.mentions
        if not mentions or not mentions.total:
            continue
        where = f" for {cohort.label}" if cohort.label else ""
        if cohort.counts_mentions:
            message = (
                f"{mentions.total:,} trials{where} mention the drug or condition only in "
                "descriptions. They are counted, and listed under mentions."
            )
            code = NoteCode.MENTIONS_COUNTED
        else:
            message = (
                f"{mentions.total:,} more trials{where} mention the drug or condition only in "
                "descriptions, often as one of several permitted options. They are listed under "
                "mentions but not counted. Set retrieval.name_matching to mentioned to count them."
            )
            code = NoteCode.MENTIONS_REPORTED
        notes.append(Note(code=code, message=message))
    return notes
