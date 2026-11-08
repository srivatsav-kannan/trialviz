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
- Drugs are identified by the trial's intervention MeSH term when one can be tied to the
  intervention: the term appears in its name ("pembrolizumab 200 mg") or in its other
  names ("Keytruda", listing "MK-3475, pembrolizumab"), or the trial has exactly one
  drug and one MeSH term left unpaired. Otherwise the intervention's own name is used.
  Placebo, sham, vehicle and saline control arms are not drugs and are left out.
- Partial dates ("2019-03") count as the first day of the month.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date

from trialviz.aggregation.evidence import Citation, Found, Study, each, one
from trialviz.retrieval.essie import sanitize_term
from trialviz.vocabulary import Dimension, InterventionType, Measure, OverallStatus, Phase
from trialviz.vocabulary.trials import SponsorClass, StudyType

NOT_REPORTED = "NOT_REPORTED"
NOT_REPORTED_LABEL = "Not reported"
NO_PHASE = "NO_PHASE"
NO_PHASE_LABEL = "No phase (observational)"
"""Observational and expanded-access studies have no phase by design, which is different
from an interventional trial that leaves its phase out."""


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
    "officials": "protocolSection.contactsLocationsModule.overallOfficials",
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
        kind = one(study, _PATHS["study_type"])
        if kind is not None and kind.value != StudyType.INTERVENTIONAL.value:
            return [Value(NO_PHASE, NO_PHASE_LABEL, (kind.citation(study),))]
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
    if key == NO_PHASE:
        return (98,)
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


Aliases = dict[str, str]
"""A brand or code name ("opdivo") and the MeSH term other trials tie it to ("Nivolumab")."""


@dataclass
class _Entry:
    found: Found
    names: list[str]
    mesh: Found | None = None


def _entries(study: Study, types: frozenset[str] | None) -> list[_Entry]:
    """The trial's drug-like interventions, each tied to its MeSH term where one can be tied
    (see the module docstring for the rules)."""
    meshes = list(each(study, _PATHS["intervention_meshes"], "term"))
    entries = []
    for index, intervention in enumerate(each(study, _PATHS["interventions"])):
        path = f"{_PATHS['interventions']}[{index}]"
        name = one(study, f"{path}.name")
        kind = intervention.value.get("type") if isinstance(intervention.value, dict) else None
        if name is None or _NOT_A_DRUG.search(str(name.value)) or (types and kind not in types):
            continue
        names = [str(name.value), *(str(o.value) for o in each(study, f"{path}.otherNames"))]
        tied = {str(m.value).casefold(): m for m in meshes if any(_ties(m.value, n) for n in names)}
        # Names that tie to several MeSH terms list alternatives ("Sintilimab", other names
        # "Pembrolizumab, Nivolumab"), so the intervention is not tied to any of them.
        entries.append(_Entry(name, names, next(iter(tied.values())) if len(tied) == 1 else None))
    unpaired_meshes = [m for m in meshes if all(m is not e.mesh for e in entries)]
    unpaired = [e for e in entries if e.mesh is None]
    if len(unpaired) == 1 and len(unpaired_meshes) == 1:
        unpaired[0].mesh = unpaired_meshes[0]
    return entries


def learn_aliases(studies: Iterable[Study]) -> Aliases:
    """Brand and code names that some trial ties to a MeSH term, so trials that use the
    name without a MeSH term ("Opdivo" with no other names) can be merged as well."""
    aliases: Aliases = {}
    for study in studies:
        for entry in _entries(study, None):
            if entry.mesh is None:
                continue
            for name in entry.names:
                if name.casefold() != str(entry.mesh.value).casefold():
                    aliases.setdefault(name.casefold(), str(entry.mesh.value))
    return aliases


def drugs(
    study: Study, aliases: Aliases | None = None, types: frozenset[str] | None = None
) -> list[Drug]:
    """The trial's interventions as canonical drugs, keyed by their MeSH term when one is
    tied or learned from other trials, else by the intervention's own name. With `types`,
    only interventions of those types count."""
    result: dict[str, Drug] = {}
    for entry in _entries(study, types):
        name = str(entry.found.value)
        learned = None if entry.mesh else (aliases or {}).get(name.casefold())
        label = str(entry.mesh.value) if entry.mesh else learned or name
        citations: tuple[Citation, ...] = (entry.found.citation(study),)
        if entry.mesh:
            citations += (entry.mesh.citation(study),)
        result.setdefault(label.casefold(), Drug(label.casefold(), label, name, citations))
    return list(result.values())


# Words a drug's name can carry beyond the substance itself: doses, units, routes, forms.
_DOSE_WORDS = re.compile(
    r"^(\d+([.,]\d+)?|mg|g|mcg|ug|µg|kg|m2|ml|iu|units?|per|q\d*w|iv|sc|im|po|oral|"
    r"intravenous|subcutaneous|intramuscular|injection|infusion|tablets?|capsules?|"
    r"solution|dose|doses|dosing|high|low|standard|fixed|weekly|daily|regimen|arm|and|or|"
    r"plus|with|in|of|the|a|at|/|-|\+)$"
)


def _ties(term: object, name: object) -> bool:
    """Whether a MeSH term is the substance an intervention name refers to: the name is the
    term, or the term plus only dose, unit, route and form words ("pembrolizumab 200 mg IV").
    "Bone Marrow Aspiration and Biopsy" does not tie to "Biopsy", and
    "Bortezomib-bendamustine-melphalan" does not tie to "Melphalan"."""
    t, n = sanitize_term(str(term)), sanitize_term(str(name))
    if not t or f" {t} " not in f" {n} ":
        return False
    rest = f" {n} ".replace(f" {t} ", " ", 1).split()
    return all(_DOSE_WORDS.match(word) for word in rest)


def _names_match(term: object, name: object) -> bool:
    """Whether a MeSH term or asked-for name and a name in the record refer to the same
    thing: one contains the other as whole words, ignoring case and punctuation."""
    a, b = f" {sanitize_term(str(term))} ", f" {sanitize_term(str(name))} "
    return a.strip() != "" and b.strip() != "" and (a in b or b in a)


# --- Naming fields ---------------------------------------------------------------------------

_NAMING_FIELDS: dict[str, list[tuple[str, str | None]]] = {
    "interventions": [
        (_PATHS["interventions"], "name"),
        (_PATHS["intervention_meshes"], "term"),
    ],
    "conditions": [
        (_PATHS["conditions"], None),
        ("protocolSection.conditionsModule.keywords", None),
        (_PATHS["condition_meshes"], "term"),
    ],
}


def naming_values(study: Study, kind: str) -> Iterator[Found]:
    """The values in the fields that name a trial's interventions or conditions, the same
    fields retrieval requires a drug or condition to appear in."""
    for path, item in _NAMING_FIELDS[kind]:
        yield from each(study, path, item)
    if kind == "interventions":
        for index, _ in enumerate(each(study, _PATHS["interventions"])):
            yield from each(study, f"{_PATHS['interventions']}[{index}].otherNames")


def find_name(study: Study, kind: str, names: Iterable[str]) -> Found | None:
    """The first naming value in the trial that matches one of the names."""
    wanted = [n for n in names if sanitize_term(n)]
    return next(
        (
            found
            for found in naming_values(study, kind)
            if any(_names_match(n, found.value) for n in wanted)
        ),
        None,
    )


def shared_meshes(studies: list[Study]) -> list[str]:
    """MeSH terms at least half the trials share. The registry expands a searched name with
    synonyms, so these are what a name like "Keytruda" was expanded to ("pembrolizumab")."""
    counts: Counter[str] = Counter()
    for study in studies:
        counts.update(
            {
                str(found.value).casefold()
                for path in (_PATHS["intervention_meshes"], _PATHS["condition_meshes"])
                for found in each(study, path, "term")
            }
        )
    return [term for term, n in counts.items() if n * 2 >= max(1, len(studies))]


def _intervention(study: Study) -> list[Value]:
    found = [Value(d.key, d.label, d.citations) for d in drugs(study)]
    return found or _not_reported()


Extractors = dict[Dimension, Extractor]


def extractors(studies: Iterable[Study], types: Iterable[InterventionType] | None) -> Extractors:
    """The dimension extractors for one answer. Interventions differ from the defaults in
    two ways: only interventions of the plan's intervention types count, so a question
    about drug trials does not draw their biopsies and scans, and brand names are merged
    into the MeSH terms other trials in the answer tie them to."""
    aliases = learn_aliases(studies)
    allowed = frozenset(t.value for t in types) if types else None

    def intervention(study: Study) -> list[Value]:
        found = [Value(d.key, d.label, d.citations) for d in drugs(study, aliases, allowed)]
        return found or _not_reported()

    return {**DIMENSIONS, Dimension.INTERVENTION: intervention}


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


def _investigator(study: Study) -> list[Value]:
    """The trial's overall officials (principal investigators, study chairs, directors),
    by name. Names are written by each trial, so the same person can appear under two
    spellings, which only merge when they match ignoring case and punctuation."""
    values: dict[str, Value] = {}
    for found in each(study, _PATHS["officials"], "name"):
        name = str(found.value).strip()
        key = sanitize_term(re.sub(r",.*$", "", name)) or name.casefold()
        values.setdefault(key, Value(key, re.sub(r",.*$", "", name), (found.citation(study),)))
    return list(values.values()) or _not_reported()


def _site(study: Study) -> list[Value]:
    """The trial's sites, as facility, city and country, since facility names repeat
    across cities ("University Hospital")."""
    values: dict[str, Value] = {}
    for index, found in enumerate(each(study, _PATHS["locations"])):
        location = found.value if isinstance(found.value, dict) else {}
        facility = str(location.get("facility") or "").strip()
        if not facility:
            continue
        place = ", ".join(str(location[k]).strip() for k in ("city", "country") if location.get(k))
        label = f"{facility} ({place})" if place else facility
        cited = one(study, f"{_PATHS['locations']}[{index}].facility")
        citations = (cited.citation(study),) if cited else ()
        key = sanitize_term(label) or label.casefold()
        values.setdefault(key, Value(key, label, citations))
    return list(values.values()) or _not_reported()


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
    Dimension.INVESTIGATOR: _investigator,
    Dimension.SITE: _site,
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


# Words that say an arm's drugs are alternatives rather than given together. The first set
# is safe to look for in descriptions too; the second only in the short arm label, since
# descriptions of real combinations say "with or without" and "at 3-week intervals".
_ALTERNATIVES_ANYWHERE = re.compile(r"\b(choice|either|monotherapy|single[- ]agent)\b", re.I)
_ALTERNATIVES_IN_LABEL = re.compile(
    r"\b(\w+ or \w+|switch\w*|interval|alternat\w*|to iv|to sc)\b", re.I
)


def arm_drug_sets(
    study: Study, aliases: Aliases | None = None, types: frozenset[str] | None = None
) -> list[tuple[list[Drug], Citation]]:
    """For each arm, the canonical drugs given in it, with the arm's own citation.

    Arms list interventions as "Type: Name", so each reference is matched back to the
    trial's interventions by name. Two drugs in one arm are given together, which is what
    a combination means, unless the arm's label or description says they are alternatives
    ("investigator's choice", "anti-PD-1 monotherapy", "nivolumab or pembrolizumab"). Two
    drugs in different arms of the same trial are not a combination either.
    """
    by_name = {drug.name.casefold(): drug for drug in drugs(study, aliases, types)}
    sets = []
    for index, _ in enumerate(each(study, _PATHS["arms"])):
        path = f"{_PATHS['arms']}[{index}]"
        arm = one(study, f"{path}.interventionNames")
        if arm is None:
            continue
        label = one(study, f"{path}.label")
        description = one(study, f"{path}.description")
        texts = " ".join(str(f.value) for f in (label, description) if f)
        if _ALTERNATIVES_ANYWHERE.search(texts) or (
            label and _ALTERNATIVES_IN_LABEL.search(str(label.value))
        ):
            continue
        names = [_TYPE_PREFIX.sub("", str(n)).casefold() for n in arm.value]
        given = list({by_name[n].key: by_name[n] for n in names if n in by_name}.values())
        if given:
            sets.append((given, arm.citation(study)))
    return sets
