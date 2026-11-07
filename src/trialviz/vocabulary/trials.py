"""Controlled values that ClinicalTrials.gov itself defines.

Enum values are exactly the strings the API returns and accepts (verified against
`/api/v2/stats/field/values` on 2026-11-07), so they can be used in API filters without
translation. Labels are the human-readable forms for charts and notes.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from trialviz.vocabulary._parsing import enum_parser


class Phase(StrEnum):
    EARLY_PHASE1 = "EARLY_PHASE1"
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"
    NA = "NA"

    @property
    def label(self) -> str:
        return _PHASE_LABELS[self]


_PHASE_LABELS = {
    Phase.EARLY_PHASE1: "Early Phase 1",
    Phase.PHASE1: "Phase 1",
    Phase.PHASE2: "Phase 2",
    Phase.PHASE3: "Phase 3",
    Phase.PHASE4: "Phase 4",
    Phase.NA: "Not applicable",
}

_NUMERALS = {"1": "1", "i": "1", "2": "2", "ii": "2", "3": "3", "iii": "3", "4": "4", "iv": "4"}

# "Phase 0" is the former name of Early Phase 1 on ClinicalTrials.gov.
_PHASE_ALIASES: dict[str, Phase] = {
    "phase 0": Phase.EARLY_PHASE1,
    "0": Phase.EARLY_PHASE1,
    "early phase 1": Phase.EARLY_PHASE1,
    "early phase i": Phase.EARLY_PHASE1,
    "not applicable": Phase.NA,
    "n/a": Phase.NA,
} | {
    spelling: Phase(f"PHASE{number}")
    for numeral, number in _NUMERALS.items()
    for spelling in (numeral, f"phase {numeral}", f"phase{numeral}")
}

parse_phase = enum_parser(Phase, _PHASE_ALIASES, noun="phase")


def parse_phases(raw: object) -> list[Phase]:
    """Parse one phase spec or a list of them into canonical phases, in phase order.

    Combined phases expand to both members, because that is how the registry stores them:
    a "Phase 1/2" trial carries `["PHASE1", "PHASE2"]`, so "1/2" filters on both.
    """
    items: Iterable[object] = [raw] if isinstance(raw, str | Phase) else _as_list(raw, "phases")
    found: set[Phase] = set()
    for item in items:
        try:
            found.add(parse_phase(item))
        except ValueError:
            # Only a string that is not itself a phase ("N/A" is) is read as a combination.
            if not (isinstance(item, str) and "/" in item):
                raise
            found.update(parse_phase(part) for part in _split_combined_phase(item))
    return [phase for phase in Phase if phase in found]


def _split_combined_phase(text: str) -> list[str]:
    """Split "Phase I/II" into ["Phase I", "Phase II"] so each half parses on its own."""
    first, *rest = (part.strip() for part in text.split("/"))
    prefix = "phase " if first.lower().startswith("phase") else ""
    return [first, *(part if part.lower().startswith("phase") else prefix + part for part in rest)]


class OverallStatus(StrEnum):
    NOT_YET_RECRUITING = "NOT_YET_RECRUITING"
    RECRUITING = "RECRUITING"
    ENROLLING_BY_INVITATION = "ENROLLING_BY_INVITATION"
    ACTIVE_NOT_RECRUITING = "ACTIVE_NOT_RECRUITING"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    COMPLETED = "COMPLETED"
    WITHDRAWN = "WITHDRAWN"
    UNKNOWN = "UNKNOWN"
    WITHHELD = "WITHHELD"
    AVAILABLE = "AVAILABLE"
    NO_LONGER_AVAILABLE = "NO_LONGER_AVAILABLE"
    TEMPORARILY_NOT_AVAILABLE = "TEMPORARILY_NOT_AVAILABLE"
    APPROVED_FOR_MARKETING = "APPROVED_FOR_MARKETING"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").capitalize().replace("Active not", "Active, not")


# Deliberately no alias for "active" or "ongoing": users mean several statuses by those
# words, so the choice belongs to the interpreter, which can see the whole question.
parse_status = enum_parser(OverallStatus, {"unknown status": OverallStatus.UNKNOWN}, noun="status")


class SponsorClass(StrEnum):
    INDUSTRY = "INDUSTRY"
    NIH = "NIH"
    FED = "FED"
    OTHER_GOV = "OTHER_GOV"
    INDIV = "INDIV"
    NETWORK = "NETWORK"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"
    AMBIG = "AMBIG"

    @property
    def label(self) -> str:
        return _SPONSOR_CLASS_LABELS[self]


_SPONSOR_CLASS_LABELS = {
    SponsorClass.INDUSTRY: "Industry",
    SponsorClass.NIH: "NIH",
    SponsorClass.FED: "U.S. federal (non-NIH)",
    SponsorClass.OTHER_GOV: "Other government",
    SponsorClass.INDIV: "Individual",
    SponsorClass.NETWORK: "Network",
    SponsorClass.OTHER: "Other (academic, hospital, nonprofit)",
    SponsorClass.UNKNOWN: "Unknown",
    SponsorClass.AMBIG: "Ambiguous",
}

parse_sponsor_class = enum_parser(
    SponsorClass,
    {
        "federal": SponsorClass.FED,
        "other government": SponsorClass.OTHER_GOV,
        "individual": SponsorClass.INDIV,
        "ambiguous": SponsorClass.AMBIG,
    },
    noun="sponsor class",
)


class InterventionType(StrEnum):
    DRUG = "DRUG"
    BIOLOGICAL = "BIOLOGICAL"
    DEVICE = "DEVICE"
    PROCEDURE = "PROCEDURE"
    RADIATION = "RADIATION"
    BEHAVIORAL = "BEHAVIORAL"
    GENETIC = "GENETIC"
    DIETARY_SUPPLEMENT = "DIETARY_SUPPLEMENT"
    COMBINATION_PRODUCT = "COMBINATION_PRODUCT"
    DIAGNOSTIC_TEST = "DIAGNOSTIC_TEST"
    OTHER = "OTHER"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").capitalize()


parse_intervention_type = enum_parser(
    InterventionType,
    {
        "biologic": InterventionType.BIOLOGICAL,
        "behavioural": InterventionType.BEHAVIORAL,
        "radiotherapy": InterventionType.RADIATION,
        "supplement": InterventionType.DIETARY_SUPPLEMENT,
    },
    noun="intervention type",
)


class StudyType(StrEnum):
    INTERVENTIONAL = "INTERVENTIONAL"
    OBSERVATIONAL = "OBSERVATIONAL"
    EXPANDED_ACCESS = "EXPANDED_ACCESS"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").capitalize()


parse_study_type = enum_parser(StudyType, noun="study type")


def _as_list(raw: object, noun: str) -> list[object]:
    if not isinstance(raw, list | tuple):
        raise ValueError(f"{noun} must be a string or a list of strings")
    return list(raw)
