"""Reading values out of raw registry records, each with the citation that proves it.

A citation names a trial, the exact JSON path of a field in the API response, and the
exact text found there. Anyone can check it: fetch the trial, follow the path, compare.
`kind` says how to compare. A `value` citation's excerpt is the whole field value, and a
`quote` citation's excerpt is a passage inside a longer text field, used for the
sentence where a mention-only trial mentions its drug or condition.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

Study = Mapping[str, Any]

STUDY_URL = "https://clinicaltrials.gov/study/{nct_id}"


class CitationKind(StrEnum):
    """`value` when the excerpt is the whole value at the path, `quote` for a passage in it."""

    VALUE = "value"
    QUOTE = "quote"


class Citation(BaseModel):
    """Where one trial's registry record shows why it is counted where it is."""

    model_config = ConfigDict(frozen=True)

    nct_id: str = Field(description="The trial's NCT number.")
    field: str = Field(
        description='JSON path in the API response, such as "protocolSection.designModule.phases".'
    )
    excerpt: str = Field(description="The exact text at that path (value) or within it (quote).")
    kind: CitationKind = CitationKind.VALUE
    url: str = Field(description="The trial's page on ClinicalTrials.gov.")


def nct_id(study: Study) -> str:
    return str(study["protocolSection"]["identificationModule"]["nctId"])


def title(study: Study) -> str:
    return str(study["protocolSection"]["identificationModule"].get("briefTitle", ""))


@dataclass(frozen=True)
class Found:
    """A value read from a record, and where it was read from."""

    value: Any
    path: str

    def citation(self, study: Study) -> Citation:
        return cite(study, self.path, self.value)


def cite(study: Study, path: str, value: Any, kind: CitationKind = CitationKind.VALUE) -> Citation:
    excerpt = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    identifier = nct_id(study)
    return Citation(
        nct_id=identifier,
        field=path,
        excerpt=excerpt,
        kind=kind,
        url=STUDY_URL.format(nct_id=identifier),
    )


def at(study: Study, path: str) -> Any:
    """Follow a citation path ("a.b[2].c") through a record. Used to verify citations."""
    node: Any = study
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        node = node[int(part[1:-1])] if part.startswith("[") else node[part]
    return node


def one(study: Study, path: str) -> Found | None:
    """The value at a dotted path, or None if any step is missing."""
    try:
        value = at(study, path)
    except (KeyError, IndexError, TypeError):
        return None
    return None if value in (None, "", []) else Found(value, path)


def each(study: Study, list_path: str, item_field: str | None = None) -> Iterator[Found]:
    """Every item of a list field, or one field of every object in it, with indexed paths."""
    found = one(study, list_path)
    if found is None:
        return
    for index, item in enumerate(found.value):
        path = f"{list_path}[{index}]"
        value = item.get(item_field) if item_field and isinstance(item, Mapping) else item
        if value not in (None, ""):
            yield Found(value, f"{path}.{item_field}" if item_field else path)
