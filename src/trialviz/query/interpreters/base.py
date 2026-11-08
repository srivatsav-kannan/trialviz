"""The contract every interpreter meets.

An interpreter reads the question (with the caller's structured fields as context) and
proposes values for plan slots. Its output is a proposal, never a decision: the resolver
validates every proposed value and enforces the rule that explicit fields win. That is
why `proposal` is an untyped mapping. Typing it would imply a trust the resolver does not
extend.

Interpreters are interchangeable. The resolver cannot tell whether a language model or a
set of rules produced a proposal, and the tests depend on that.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from typing import Protocol

from trialviz.query.request import QueryRequest

log = logging.getLogger(__name__)


class IssueKind(StrEnum):
    AMBIGUOUS_REFERENCE = "ambiguous_reference"
    OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True)
class InterpreterIssue:
    """A problem with the question itself that no slot value can express."""

    kind: IssueKind
    message: str


@dataclass(frozen=True)
class Fallback:
    """An interpreter that was tried and could not answer, and why."""

    interpreter: str
    reason: str


@dataclass(frozen=True)
class Interpretation:
    interpreter: str
    proposal: Mapping[str, object]
    """Untrusted slot values, shaped like {"filters": {...}, "analysis": {...},
    "presentation": {...}}."""
    issue: InterpreterIssue | None = None
    unexpressed: tuple[str, ...] = ()
    """Constraints in the question that no slot can express, such as "more than 500
    participants". Reported to the caller so they are never dropped silently."""
    fallbacks: tuple[Fallback, ...] = field(default=())

    def after_fallbacks(self, fallbacks: tuple[Fallback, ...]) -> Interpretation:
        return replace(self, fallbacks=fallbacks)


class InterpreterUnavailable(Exception):
    """The interpreter cannot produce a proposal right now (no key, spend cap, outage).

    This is an expected condition, not a bug: the fallback chain moves to the next
    interpreter, and the reason is reported to the caller as a note.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Interpreter(Protocol):
    @property
    def name(self) -> str: ...

    async def interpret(self, request: QueryRequest, today: date) -> Interpretation:
        """Propose slot values for `request.query`.

        `today` anchors relative time ("in the last five years") so results are
        reproducible. Raises InterpreterUnavailable when no proposal can be made.
        """
        ...


class FallbackInterpreter:
    """Try interpreters in order and return the first proposal, recording each failure."""

    def __init__(self, *chain: Interpreter) -> None:
        if not chain:
            raise ValueError("FallbackInterpreter needs at least one interpreter")
        self._chain = chain

    @property
    def name(self) -> str:
        return " > ".join(interpreter.name for interpreter in self._chain)

    async def interpret(self, request: QueryRequest, today: date) -> Interpretation:
        failures: list[Fallback] = []
        for interpreter in self._chain:
            try:
                interpretation = await interpreter.interpret(request, today)
            except InterpreterUnavailable as error:
                failures.append(Fallback(interpreter.name, error.reason))
                continue
            except Exception as error:
                # A bug in one interpreter should cost the question its best reading, not
                # the whole request, so the next interpreter still gets a turn, and the
                # traceback goes to the log, where the note in the answer cannot carry it.
                log.exception("interpreter %s failed", interpreter.name)
                failures.append(Fallback(interpreter.name, f"failed with {type(error).__name__}"))
                continue
            return interpretation.after_fallbacks(tuple(failures))
        summary = "; ".join(f"{failure.interpreter}: {failure.reason}" for failure in failures)
        raise InterpreterUnavailable(f"every interpreter failed ({summary})")
