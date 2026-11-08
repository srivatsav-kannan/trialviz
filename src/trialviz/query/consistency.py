"""Rules that relate two slots, shared by request validation and plan resolution.

A single slot can be valid while a pair is not: a start year of 2020 and an end year of
2015 are each fine. These rules live in one place so that a user who sends the bad pair
gets a 422, and an interpreter that proposes it has its own half dropped, under exactly
the same definition of "bad".
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class PairRule:
    """Two slots that must satisfy `holds` whenever both are set.

    `slots` is ordered by importance: when an interpreter breaks the rule, the resolver
    drops inferred values starting from the second slot.
    """

    slots: tuple[str, str]
    holds: Callable[[Any, Any], bool]
    explanation: str

    def broken_by(self, values: Mapping[str, Any]) -> bool:
        first, second = (values.get(slot) for slot in self.slots)
        return first is not None and second is not None and not self.holds(first, second)

    def describe(self, values: Mapping[str, Any], prefix: str = "") -> str:
        first, second = (f"{prefix}{slot}={values[slot]!s}" for slot in self.slots)
        return f"{first} and {second}: {self.explanation}"


FILTER_RULES = (
    PairRule(
        ("start_year_from", "start_year_to"),
        operator.le,
        "the start-year range runs backwards",
    ),
)

ANALYSIS_RULES = (
    PairRule(("group_by", "series_by"), operator.ne, "grouping and series must differ"),
    PairRule(("x", "y"), operator.ne, "a scatter plot needs two different measures"),
    PairRule(("x", "z"), operator.ne, "the third measure must differ from the first"),
    PairRule(("y", "z"), operator.ne, "the third measure must differ from the second"),
)


def broken_rules(values: Mapping[str, Any], rules: tuple[PairRule, ...]) -> list[PairRule]:
    return [rule for rule in rules if rule.broken_by(values)]


def same_meaning(left: Any, right: Any) -> bool:
    """Whether two slot values mean the same thing, ignoring list order and letter case.

    `["Keytruda", "Opdivo"]` and `["opdivo", "keytruda"]` are the same filter, so two
    cohorts that differ only that way are duplicates, and an interpreter that proposes one
    while the caller sent the other is agreeing, not contradicting.
    """
    return bool(_canonical(left) == _canonical(right))


def _canonical(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        return frozenset((key, _canonical(item)) for key, item in value.items())
    if isinstance(value, list | tuple | set | frozenset):
        return frozenset(_canonical(item) for item in value)
    if isinstance(value, str):
        return value.casefold()
    return value
