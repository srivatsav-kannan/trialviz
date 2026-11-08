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
# Characters that would end or escape a quoted phrase.
_QUOTE_UNSAFE = re.compile(r'["\\]')


# A possessive or apostrophe. MeSH drops possessives ("Alzheimer Disease", "Crohn Disease"),
# and searching "alzheimer's disease" finds fewer trials than "alzheimer disease".
_APOSTROPHE = re.compile(r"[\"'\u2019]s\b|['\u2019]")
# A parenthetical, closed or not. In a name it is an abbreviation or an alias ("Crohn's
# disease (CD)"), and since names of several words are searched as phrases, keeping it
# would make the phrase match nothing.
_PARENTHETICAL = re.compile(r"\([^)]*(\)|$)")


def sanitize_term(text: str) -> str:
    """Reduce free text to lowercase search words, with no Essie syntax left in it."""
    without_syntax = _SYNTAX_CHARACTERS.sub(" ", _APOSTROPHE.sub("", text))
    return _WHITESPACE.sub(" ", without_syntax).strip().lower()


def name_words(name: str) -> str:
    """A drug or condition name as the words searched for in its naming fields: like
    `sanitize_term`, without any parenthetical, which would break the phrase."""
    return sanitize_term(_PARENTHETICAL.sub(" ", name))


def any_of_terms(
    terms: Iterable[str], areas: Iterable[str] = (), exact: Iterable[tuple[str, str]] = ()
) -> str | None:
    """Alternatives for a `query.*` parameter: `(breast cancer) OR (lung cancer)`.

    With `areas`, each term must appear in one of those fields, which the registry still
    expands with synonyms: `(AREA[Condition](melanoma) OR AREA[ConditionMeshTerm](melanoma))`.
    A term of several words is searched there as a phrase, since unquoted words may match
    anywhere across a trial's conditions: `type 1 diabetes` unquoted matches a trial whose
    condition is "Type 2 Diabetes Mellitus" and whose keyword is "phase 1".

    `exact` adds (area, value) alternatives matched as whole field values, such as the MeSH
    terms found to sit under a condition in the MeSH tree. Terms that sanitize to nothing
    are dropped. Returns None if nothing is left, so the caller can omit the parameter.
    """
    fields = list(areas)
    clean = name_words if fields else sanitize_term
    cleaned = list(dict.fromkeys(term for term in map(clean, terms) if term))

    def one(term: str) -> str:
        if not fields:
            return f"({term})"
        phrase = f'"{term}"' if " " in term else f"({term})"
        return "(" + " OR ".join(f"AREA[{area}]{phrase}" for area in fields) + ")"

    parts = [one(term) for term in cleaned]
    parts += [f'(AREA[{area}]"{_QUOTE_UNSAFE.sub(" ", value)}")' for area, value in exact]
    return " OR ".join(parts) or None


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
