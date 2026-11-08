"""What each dimension and measure means for one trial record.

A dimension maps a trial to zero or more groups, each with the citation that puts the
trial there. A measure maps a trial to a number, with citations for every field the
number was computed from. These definitions are the single place where the registry's
record shape meets the analysis vocabulary.

Decisions recorded here:

- A trial with phases ["PHASE1", "PHASE2"] belongs to one group, "Phase 1/2", rather than
  being counted once under each phase. That is how the registry labels such trials.
- A trial with no value for a dimension goes to an explicit "Not reported" group, so
  totals add up and missing data stays visible.
- Drugs are identified by MeSH term when the registry provides one, so "Keytruda",
  "MK-3475" and "pembrolizumab 200 mg" become one node. Intervention names are the
  fallback. Placebo, sham and vehicle arms are not drugs and are left out.
- Partial dates ("2019-03") count as the first day of the month.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from trialviz.aggregation.evidence import Citation, Found, Study, each, one
from trialviz.vocabulary import Dimension, InterventionType, Measure, OverallStatus, Phase
from trialviz.vocabulary.trials import SponsorClass, StudyType

NOT_REPORTED = "NOT_REPORTED"
NOT_REPORTED_LABEL = "Not reported"


@dataclass(frozen=True)
class Value:
    """One group a trial belongs to under some dimension."""

    key: str
    label: str
    citations: tuple[Citation, ...] = field(default=(), compare=False)


Extractor = Callable[[Study], list[Value]]

_PATHS = {
    "phases": "protocolSection.designModule.phases",
    "status": "protocolSection.statusModule.overallStatus",
    "sponsor": "protocolSection.sponsorCollaboratorsModule.leadSponsor",
    "interventions": "protocolSection.armsInterventionsModule.interventions",
    "arms": "protocolSection.armsInterventionsModule.armGroups",
    "intervention_meshes": "derivedSection.interventionBrowseModule.meshes",
    "conditions": "protocolSection.conditionsModule.conditions",
    "condition_meshes": "derivedSection.conditionBrowseModule.meshes",
    "locations": "protocolSection.contactsLocationsModule.locations",
    "study_type": "protocolSection.designModule.studyType",
    "start": "protocolSection.statusModule.startDateStruct.date",
    "primary_completion": "protocolSection.statusModule.primaryCompletionDateStruct.date",
    "completion": "protocolSection.statusModule.completionDateStruct.date",
    "enrollment": "protocolSection.designModule.enrollmentInfo.count",
}

_NOT_A_DRUG = re.compile(r"\b(placebo|sham|vehicle|saline control)\b", re.IGNORECASE)
_TYPE_PREFIX = re.compile(r"^[A-Za-z ]+:\s*")


def _not_reported() -> list[Value]:
    return [Value(NOT_REPORTED, NOT_REPORTED_LABEL)]


def _enum_value(study: Study, path: str, label: Callable[[str], str]) -> list[Value]:
    found = one(study, path)
    if found is None:
        return _not_reported()
    return [Value(str(found.value), label(str(found.value)), (found.citation(study),))]


def _phase(study: Study) -> list[Value]:
    found = one(study, _PATHS["phases"])
    if found is None:
        return _not_reported()
    phases = [Phase(p) for p in found.value if p in Phase.__members__.values()]
    ordered = [p for p in Phase if p in phases]
    key = "/".join(p.value for p in ordered)
    if len(ordered) == 1:
        label = ordered[0].label
    else:
        label = "Phase " + "/".join(p.label.removeprefix("Phase ") for p in ordered)
    return [Value(key, label, (found.citation(study),))]


def phase_order(key: str) -> tuple[int, ...]:
    """Natural order for phase keys: Early Phase 1, 1, 1/2, 2, 2/3, 3, 4, N/A, not reported."""
    if key == NOT_REPORTED:
        return (99,)
    members = list(Phase)
    indexes = [members.index(Phase(part)) for part in key.split("/")]
    return (indexes[0], len(indexes))


def _sponsor_class(study: Study) -> list[Value]:
    found = one(study, f"{_PATHS['sponsor']}.class")
    if found is None:
        return _not_reported()
    raw = str(found.value)
    label = SponsorClass(raw).label if raw in SponsorClass.__members__.values() else raw.title()
    return [Value(raw, label, (found.citation(study),))]


def _lead_sponsor(study: Study) -> list[Value]:
    found = one(study, f"{_PATHS['sponsor']}.name")
    if found is None:
        return _not_reported()
    return [Value(str(found.value).casefold(), str(found.value), (found.citation(study),))]


@dataclass(frozen=True)
class Drug:
    key: str
    label: str
    name: str
    """The intervention's own name in the record, used to match arm group references."""
    citations: tuple[Citation, ...]


def drugs(study: Study) -> list[Drug]:
    """The trial's interventions as canonical drugs: MeSH term when one matches the name."""
    meshes = list(each(study, _PATHS["intervention_meshes"], "term"))
    result: dict[str, Drug] = {}
    for found in each(study, _PATHS["interventions"], "name"):
        name = str(found.value)
        if _NOT_A_DRUG.search(name):
            continue
        mesh = next((m for m in meshes if str(m.value).casefold() in name.casefold()), None)
        chosen: Found = mesh or found
        key = str(chosen.value).casefold()
        citations = (found.citation(study),) + ((mesh.citation(study),) if mesh else ())
        result.setdefault(key, Drug(key, str(chosen.value), name, citations))
    return list(result.values())


def _intervention(study: Study) -> list[Value]:
    found = [Value(d.key, d.label, d.citations) for d in drugs(study)]
    return found or _not_reported()


def _intervention_type(study: Study) -> list[Value]:
    values: dict[str, Value] = {}
    for found in each(study, _PATHS["interventions"], "type"):
        raw = str(found.value)
        label = InterventionType(raw).label if raw in InterventionType.__members__.values() else raw
        values.setdefault(raw, Value(raw, label, (found.citation(study),)))
    return list(values.values()) or _not_reported()


def _named_terms(study: Study, mesh_path: str, name_path: str) -> list[Value]:
    found = list(each(study, mesh_path, "term")) or list(each(study, name_path))
    values: dict[str, Value] = {}
    for item in found:
        text = str(item.value)
        values.setdefault(text.casefold(), Value(text.casefold(), text, (item.citation(study),)))
    return list(values.values()) or _not_reported()


def _condition(study: Study) -> list[Value]:
    return _named_terms(study, _PATHS["condition_meshes"], _PATHS["conditions"])


def _country(study: Study) -> list[Value]:
    values: dict[str, Value] = {}
    for found in each(study, _PATHS["locations"], "country"):
        country = str(found.value)
        values.setdefault(country, Value(country, country.strip(), (found.citation(study),)))
    return list(values.values()) or _not_reported()


def _start_year(study: Study) -> list[Value]:
    found = one(study, _PATHS["start"])
    if found is None:
        return _not_reported()
    year = str(found.value)[:4]
    return [Value(year, year, (found.citation(study),))]


DIMENSIONS: dict[Dimension, Extractor] = {
    Dimension.PHASE: _phase,
    Dimension.STATUS: lambda s: _enum_value(s, _PATHS["status"], lambda v: OverallStatus(v).label),
    Dimension.SPONSOR_CLASS: _sponsor_class,
    Dimension.LEAD_SPONSOR: _lead_sponsor,
    Dimension.INTERVENTION: _intervention,
    Dimension.INTERVENTION_TYPE: _intervention_type,
    Dimension.CONDITION: _condition,
    Dimension.COUNTRY: _country,
    Dimension.STUDY_TYPE: lambda s: _enum_value(
        s, _PATHS["study_type"], lambda v: StudyType(v).label
    ),
    Dimension.START_YEAR: _start_year,
}


def sort_key(dimension: Dimension, value: Value) -> tuple[object, ...]:
    """The dimension's inherent order, where it has one, with "Not reported" last."""
    if value.key == NOT_REPORTED:
        return (1, 0)
    match dimension:
        case Dimension.PHASE:
            return (0, phase_order(value.key))
        case Dimension.STATUS:
            return (0, list(OverallStatus).index(OverallStatus(value.key)))
        case Dimension.START_YEAR:
            return (0, value.key)
    return (0, value.label.casefold())


ORDERED_DIMENSIONS = frozenset({Dimension.PHASE, Dimension.STATUS, Dimension.START_YEAR})
"""Dimensions whose values have an inherent order, which lines and natural sort respect."""


# --- Measures ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Measured:
    value: float
    citations: tuple[Citation, ...]


def parse_date(text: str) -> date | None:
    """Registry dates are "YYYY-MM-DD" or "YYYY-MM". A partial date is the 1st of the month."""
    parts = text.split("-")
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 1)
    except (ValueError, IndexError):
        return None


def _enrollment(study: Study) -> Measured | None:
    found = one(study, _PATHS["enrollment"])
    if found is None or not isinstance(found.value, int | float):
        return None
    return Measured(float(found.value), (found.citation(study),))


def _duration_months(study: Study) -> Measured | None:
    start = one(study, _PATHS["start"])
    end = one(study, _PATHS["completion"]) or one(study, _PATHS["primary_completion"])
    if start is None or end is None:
        return None
    began, ended = parse_date(str(start.value)), parse_date(str(end.value))
    if began is None or ended is None or ended < began:
        return None
    months = (ended - began).days / 30.4375
    return Measured(round(months, 1), (start.citation(study), end.citation(study)))


def _site_count(study: Study) -> Measured | None:
    """The number of listed sites. The citation is the last site's country, whose index in
    the locations list shows the count (index 11 means 12 sites)."""
    sites = list(each(study, _PATHS["locations"], "country"))
    if not sites:
        return None
    last = one(study, _PATHS["locations"])
    count = len(last.value) if last else len(sites)
    return Measured(float(count), (sites[-1].citation(study),))


MEASURES: dict[Measure, Callable[[Study], Measured | None]] = {
    Measure.ENROLLMENT: _enrollment,
    Measure.DURATION_MONTHS: _duration_months,
    Measure.SITE_COUNT: _site_count,
}


def time_period(study: Study, time_path_key: str, monthly: bool) -> Found | None:
    """The trial's start or completion date as a period key ("2019" or "2019-03")."""
    found = one(study, _PATHS[time_path_key])
    if found is None:
        return None
    text = str(found.value)
    return Found(text[:7] if monthly else text[:4], found.path)


def arm_drug_sets(study: Study) -> list[tuple[list[Drug], Citation]]:
    """For each arm, the canonical drugs given in it, with the arm's own citation.

    Arms list interventions as "Type: Name", so each reference is matched back to the
    trial's interventions by name. Two drugs in one arm are given together, which is what
    a combination means. Two drugs in different arms of the same trial are not.
    """
    by_name = {drug.name.casefold(): drug for drug in drugs(study)}
    sets = []
    for arm in each(study, _PATHS["arms"], "interventionNames"):
        names = [_TYPE_PREFIX.sub("", str(n)).casefold() for n in arm.value]
        given = list({by_name[n].key: by_name[n] for n in names if n in by_name}.values())
        if given:
            sets.append((given, arm.citation(study)))
    return sets
