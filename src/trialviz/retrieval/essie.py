"""Building Essie expressions, the query language of the ClinicalTrials.gov API.

Two kinds of values go into a query, and they are handled differently:

- Free-text names (drugs, conditions, sponsors, keywords) come from people and from a
  language model. Essie would execute any syntax inside them: the term `AREA[Phase]PHASE3`
  is read as a phase filter, and a stray quote fails the whole request with a 400 (both
  verified against the live API). Names are therefore reduced to words before use.
  Quoting each name would also neutralize syntax, but it turns a name into an exact
  phrase search and loses matches ("CAR-T" finds 2,963 trials unquoted, 2,629 quoted), so
  syntax characters are stripped instead and the registry's own word and synonym matching
  is kept. Essie's operators are uppercase keywords (AND, AREA, RANGE, MISSING, SEARCH and
  more), and a bare `AREA` still fails the request even without brackets. Matching ignores
  case (`mk-3475` and `MK-3475` find the same trials), so every term is lowercased, which
  disarms all of them, including any not listed here.
- Controlled values (phases, statuses, countries) are already canonical registry
  strings, so they go into `AREA[...]` clauses as they are.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Characters that carry meaning in Essie (grouping, phrases, field selectors, boosts).
_SYNTAX_CHARACTERS = re.compile(r"[\[\](){}\"^~:<>=\\*?!|]")
_WHITESPACE = re.compile(r"\s+")


def sanitize_term(text: str) -> str:
    """Reduce free text to lowercase search words, with no Essie syntax left in it."""
    without_syntax = _SYNTAX_CHARACTERS.sub(" ", text)
    return _WHITESPACE.sub(" ", without_syntax).strip().lower()


def any_of_terms(terms: Iterable[str]) -> str | None:
    """Alternatives for a `query.*` parameter: `(breast cancer) OR (lung cancer)`.

    Terms that sanitize to nothing are dropped. Returns None if none are left, so the
    caller can omit the parameter rather than send an empty search.
    """
    cleaned = list(dict.fromkeys(term for term in map(sanitize_term, terms) if term))
    if not cleaned:
        return None
    return " OR ".join(f"({term})" for term in cleaned)


def area_any_of(area: str, values: Iterable[str], *, quoted: bool = False) -> str:
    """`AREA[Phase](PHASE2 OR PHASE3)`. Quote values that contain spaces, such as countries."""
    rendered = [f'"{value}"' if quoted else value for value in values]
    return f"AREA[{area}]({' OR '.join(rendered)})"


def area_range(area: str, low: str | None, high: str | None) -> str:
    """`AREA[StartDate]RANGE[2018-01-01, MAX]`, with MIN or MAX for an open end."""
    return f"AREA[{area}]RANGE[{low or 'MIN'}, {high or 'MAX'}]"


def all_of(clauses: Iterable[str]) -> str | None:
    """Join clauses so every one must hold. Each is parenthesized so operators inside one
    clause cannot bind to its neighbors."""
    parts = [f"({clause})" for clause in clauses if clause]
    return " AND ".join(parts) if parts else None
