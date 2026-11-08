"""References in a question to something it never names, such as "this drug".

Any interpreter may notice one and ask for clarification, and the resolver then names the
filter that would settle it, so both use the one pattern defined here.
"""

from __future__ import annotations

import re

from trialviz.query.request import QueryRequest
from trialviz.vocabulary._parsing import fold_preserving_length

REFERENCE = re.compile(
    r"\b(?:this|that|these|those|the\ same|said|the\ given|the\ specified)\s+"
    r"(?P<noun>drugs?|medications?|treatments?|therap(?:y|ies)|interventions?"
    r"|conditions?|diseases?|indications?|sponsors?|compan(?:y|ies)|countr(?:y|ies))\b",
    re.VERBOSE,
)
"""Matched against the question folded to lowercase ASCII."""

_TARGETS = {
    "drug": ("interventions", "drug_name"),
    "medication": ("interventions", "drug_name"),
    "treatment": ("interventions", "drug_name"),
    "therap": ("interventions", "drug_name"),
    "intervention": ("interventions", "drug_name"),
    "condition": ("conditions", "condition"),
    "disease": ("conditions", "condition"),
    "indication": ("conditions", "condition"),
    "sponsor": ("sponsors", "sponsor"),
    "compan": ("sponsors", "sponsor"),
    "countr": ("countries", "country"),
}


def referenced_filter(noun: str) -> tuple[str, str]:
    """The filter a referring noun points at, and its shorthand: "drugs" gives
    ("interventions", "drug_name")."""
    return next(target for stem, target in _TARGETS.items() if noun.startswith(stem))


def unnamed_references(request: QueryRequest) -> list[str]:
    """The filter slots a question points at that no field fills, such as
    `filters.interventions` for "this drug" when no drug is set."""
    slots = []
    for match in REFERENCE.finditer(fold_preserving_length(request.query)):
        target, _ = referenced_filter(match.group("noun"))
        if getattr(request.filters, target) is None and f"filters.{target}" not in slots:
            slots.append(f"filters.{target}")
    return slots
