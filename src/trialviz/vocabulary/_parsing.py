"""Shared machinery for turning loose user spellings into canonical vocabulary values.

Every controlled value in the system (phases, statuses, countries, dimensions) is parsed
the same way: fold the input to a comparison key, look the key up in an alias table, and
fail with a message that names the accepted values or the closest match. User input and
model output both pass through these parsers, so a value is canonical no matter where it
came from.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def comparison_key(text: str) -> str:
    """Fold text so that spelling variants compare equal.

    Accents are stripped, case is folded, and every run of punctuation or whitespace
    becomes one space: "Côte d’Ivoire", "cote d'ivoire" and "COTE-D-IVOIRE" all map to
    "cote d ivoire".
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALPHANUMERIC.sub(" ", without_accents.casefold()).strip()


def fold_preserving_length(text: str) -> str:
    """Lowercase and strip accents one character at a time, keeping every index aligned.

    Text scanners match against the folded copy and slice the original by the same
    offsets, so a phrase found in "ROCHE in Türkiye" keeps its original spelling.
    """
    folded = []
    for char in text:
        decomposed = unicodedata.normalize("NFKD", char.lower())
        base = "".join(part for part in decomposed if not unicodedata.combining(part))
        folded.append(base if len(base) == 1 else char)
    return "".join(folded)


def closest_matches(key: str, candidates: Iterable[str], limit: int = 3) -> list[str]:
    return difflib.get_close_matches(key, list(candidates), n=limit, cutoff=0.75)


def enum_parser[E: StrEnum](
    enum: type[E],
    aliases: Mapping[str, E] | None = None,
    *,
    noun: str,
) -> Callable[[object], E]:
    """Build a parser that accepts an enum member, its value, or a documented alias.

    Matching is on `comparison_key`, so "ACTIVE_NOT_RECRUITING", "Active, not recruiting"
    and "active not recruiting" are the same input. A trailing plural "s" is tolerated
    ("devices" parses as DEVICE).
    """
    table: dict[str, E] = {comparison_key(member.value): member for member in enum}
    for alias, member in (aliases or {}).items():
        table[comparison_key(alias)] = member

    accepted = ", ".join(member.value for member in enum)

    def parse(raw: object) -> E:
        if isinstance(raw, enum):
            return raw
        if not isinstance(raw, str):
            raise ValueError(f"{noun} must be a string, got {type(raw).__name__}")
        key = comparison_key(raw)
        member = table.get(key) or (table.get(key[:-1]) if key.endswith("s") else None)
        if member is not None:
            return member
        suggestions = [table[match].value for match in closest_matches(key, table)]
        hint = f" Did you mean {' or '.join(dict.fromkeys(suggestions))}?" if suggestions else ""
        raise ValueError(f"Unknown {noun} {raw!r}.{hint} Accepted values: {accepted}.")

    return parse
