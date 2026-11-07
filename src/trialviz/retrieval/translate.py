"""Translating a plan's filters into ClinicalTrials.gov request parameters.

This is a pure function of the plan, so the same plan always yields byte-identical
requests. Name filters use the API's search parameters, which match synonyms ("Keytruda"
and "MK-3475" both find pembrolizumab trials). Controlled values use `filter.advanced`.

Drug and condition names must appear in the fields that name a trial's interventions or
conditions. The registry's default search also matches descriptions, so about one in ten
"Keytruda" trials only lists pembrolizumab as one permitted option in an arm description,
and a "melanoma" search matches a "non-melanoma" title. Restricting to the naming fields
keeps synonym expansion, loses about 3 to 15 percent of matches, and raises precision from
about 90 percent to 99 percent or more (measured against the live API). It also guarantees
every included trial has a field that names the drug or condition, which citations need.

| Plan filter          | API parameter                          |
| -------------------- | -------------------------------------- |
| conditions           | query.cond, in Condition, ConditionMeshTerm or Keyword   |
| interventions        | query.intr, in InterventionName, OtherName or MeshTerm   |
| sponsors             | query.lead (lead sponsor only)         |
| keywords             | query.term                             |
| phases               | filter.advanced AREA[Phase]            |
| statuses             | filter.advanced AREA[OverallStatus]    |
| sponsor_classes      | filter.advanced AREA[LeadSponsorClass] |
| intervention_types   | filter.advanced AREA[InterventionType] |
| study_types          | filter.advanced AREA[StudyType]        |
| countries            | filter.advanced AREA[LocationCountry]  |
| start_year_from / to | filter.advanced AREA[StartDate]RANGE   |

`query.lead` is used rather than `query.spons` because the latter also matches
collaborators, and the plan's `sponsors` filter means lead sponsors.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from trialviz.query.filters import Filters
from trialviz.query.plan import (
    ComparisonSpec,
    DistributionSpec,
    HistogramSpec,
    NetworkSpec,
    Plan,
    ScatterSpec,
    TimeSeriesSpec,
)
from trialviz.retrieval.essie import all_of, any_of_terms, area_any_of, area_range
from trialviz.vocabulary import Dimension, Measure, TimeField

STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"

_NAME_PARAMETERS = {
    "conditions": "query.cond",
    "interventions": "query.intr",
    "sponsors": "query.lead",
    "keywords": "query.term",
}

_NAMING_FIELDS = {
    "conditions": ("Condition", "ConditionMeshTerm", "Keyword"),
    "interventions": ("InterventionName", "InterventionOtherName", "InterventionMeshTerm"),
}
"""Fields a drug or condition name must appear in. Sponsors are already field-specific
(query.lead) and keywords are free text by definition."""

_AREAS = {
    "phases": "Phase",
    "statuses": "OverallStatus",
    "sponsor_classes": "LeadSponsorClass",
    "intervention_types": "InterventionType",
    "study_types": "StudyType",
}


@dataclass(frozen=True)
class RegistryQuery:
    """The search part of a /studies request: everything except paging and fields.

    Parameters are stored sorted, so equal queries are equal objects with equal URLs, which
    is what makes them usable as cache keys and as citations of how data was fetched.
    """

    params: tuple[tuple[str, str], ...] = ()

    def url(self, **extra: str) -> str:
        """The request URL, for traceability. Extra parameters (paging, fields) go last."""
        query = urlencode([*self.params, *sorted(extra.items())])
        return f"{STUDIES_URL}?{query}" if query else STUDIES_URL


def translate(*layers: Filters) -> RegistryQuery:
    """Build one query that requires every layer to match.

    A comparison cohort is two layers: the plan's shared filters and the cohort's own.
    Inside a layer, values of one field are alternatives. Across layers, everything must
    hold, so a shared `interventions: [A]` and a cohort `interventions: [B]` yield trials
    that involve both A and B.
    """
    names: dict[str, list[str]] = {}
    clauses: list[str] = []
    for layer in layers:
        for field, parameter in _NAME_PARAMETERS.items():
            values = getattr(layer, field)
            if values and (expression := any_of_terms(values, _NAMING_FIELDS.get(field, ()))):
                names.setdefault(parameter, []).append(expression)
        for field, area in _AREAS.items():
            if values := getattr(layer, field):
                clauses.append(area_any_of(area, values))
        if layer.countries:
            clauses.append(area_any_of("LocationCountry", layer.countries, quoted=True))
        if layer.start_year_from is not None or layer.start_year_to is not None:
            clauses.append(
                area_range(
                    "StartDate",
                    f"{layer.start_year_from}-01-01" if layer.start_year_from else None,
                    f"{layer.start_year_to}-12-31" if layer.start_year_to else None,
                )
            )

    params = {
        parameter: expressions[0] if len(expressions) == 1 else all_of(expressions) or ""
        for parameter, expressions in names.items()
    }
    if advanced := all_of(clauses):
        params["filter.advanced"] = advanced
    return RegistryQuery(tuple(sorted(params.items())))


def cohort_queries(plan: Plan) -> list[tuple[str | None, RegistryQuery]]:
    """One query per cohort for a comparison, otherwise a single unlabeled query."""
    if isinstance(plan.analysis, ComparisonSpec):
        return [
            (cohort.label, translate(plan.filters, cohort.filters))
            for cohort in plan.analysis.cohorts
        ]
    return [(None, translate(plan.filters))]


# --- Which record fields to request ---------------------------------------------------
# Requesting only what the analysis reads keeps a 10,000-trial fetch to a few megabytes
# instead of hundreds. Names are the API's field "pieces".

BASE_FIELDS = ("NCTId", "BriefTitle", "OverallStatus", "Phase", "StartDate")
"""Always fetched: enough to identify and describe any trial cited in a result."""

DIMENSION_FIELDS: dict[Dimension, tuple[str, ...]] = {
    Dimension.PHASE: ("Phase",),
    Dimension.STATUS: ("OverallStatus",),
    Dimension.SPONSOR_CLASS: ("LeadSponsorClass", "LeadSponsorName"),
    Dimension.LEAD_SPONSOR: ("LeadSponsorName", "LeadSponsorClass"),
    Dimension.INTERVENTION: (
        "InterventionName",
        "InterventionType",
        "InterventionMeshTerm",
        "ArmGroupLabel",
        "ArmGroupInterventionName",
    ),
    Dimension.INTERVENTION_TYPE: ("InterventionType", "InterventionName"),
    Dimension.CONDITION: ("Condition", "ConditionMeshTerm"),
    Dimension.COUNTRY: ("LocationCountry",),
    Dimension.STUDY_TYPE: ("StudyType",),
    Dimension.START_YEAR: ("StartDate",),
}

MEASURE_FIELDS: dict[Measure, tuple[str, ...]] = {
    Measure.ENROLLMENT: ("EnrollmentCount", "EnrollmentType"),
    Measure.DURATION_MONTHS: ("StartDate", "PrimaryCompletionDate", "CompletionDate"),
    Measure.SITE_COUNT: ("LocationCountry",),
}

FILTER_EVIDENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "conditions": ("Condition", "ConditionMeshTerm", "Keyword"),
    "interventions": ("InterventionName", "InterventionOtherName", "InterventionMeshTerm"),
    "sponsors": ("LeadSponsorName",),
    "sponsor_classes": ("LeadSponsorClass",),
    "intervention_types": ("InterventionType",),
    "study_types": ("StudyType",),
    "countries": ("LocationCountry",),
}
"""Fields that show why a trial matched each filter, so an included trial carries its own
evidence (its country list for a country filter, its drug names for a drug filter).
Phases, statuses and start dates are base fields. Keywords match anywhere in a record, so
no single field can show them."""

TIME_FIELDS: dict[TimeField, tuple[str, ...]] = {
    TimeField.START_DATE: ("StartDate",),
    TimeField.COMPLETION_DATE: ("CompletionDate",),
}


def fields_for(plan: Plan) -> tuple[str, ...]:
    """The base fields, the evidence for every active filter, and what the analysis reads."""
    analysis = plan.analysis
    dimensions: list[Dimension | None] = []
    measures: list[Measure] = []
    layers = [plan.filters]
    if isinstance(analysis, ComparisonSpec):
        layers += [cohort.filters for cohort in analysis.cohorts]
    extra = [
        field
        for layer in layers
        for name in layer.set_fields()
        for field in FILTER_EVIDENCE_FIELDS.get(name, ())
    ]
    match analysis:
        case DistributionSpec():
            dimensions += [analysis.group_by, analysis.series_by]
        case TimeSeriesSpec():
            dimensions.append(analysis.series_by)
            extra += TIME_FIELDS[analysis.time_field]
        case ComparisonSpec():
            dimensions.append(analysis.group_by)
        case HistogramSpec():
            measures.append(analysis.measure)
        case ScatterSpec():
            measures += [analysis.x, analysis.y]
            dimensions.append(analysis.series_by)
        case NetworkSpec():
            dimensions += list(analysis.node_types)
    for dimension in dimensions:
        if dimension is not None:
            extra += DIMENSION_FIELDS[dimension]
    for measure in measures:
        extra += MEASURE_FIELDS[measure]
    return tuple(dict.fromkeys([*BASE_FIELDS, *extra]))
