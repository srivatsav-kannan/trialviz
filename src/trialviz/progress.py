"""Progress while an answer is computed, for `POST /v1/query/stream`.

Stages report through a context variable, so the code doing the work takes no extra
arguments: the pipeline names each stage, and the registry client reports each request and
each page of records. Tasks started inside a streamed request inherit the variable, so
cohorts fetched side by side report into the same tracker. With nobody listening, reporting
does nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum

from pydantic import BaseModel, Field


class Stage(StrEnum):
    READING = "reading"
    RETRIEVING = "retrieving"
    AGGREGATING = "aggregating"
    COUNTING = "counting"
    PRESENTING = "presenting"


class Progress(BaseModel):
    """One step of an answer in progress."""

    stage: Stage = Field(description="What the service is doing.")
    message: str = Field(description="The same, in words a person can be shown.")
    registry_requests: int = Field(description="ClinicalTrials.gov requests made so far.")
    records_fetched: int = Field(description="Trial records downloaded so far.")
    records_expected: int = Field(
        description="Records the fetches started so far will download in all, so "
        "`records_fetched / records_expected` is a fair progress bar during retrieval."
    )


class _Tracker:
    def __init__(self, send: Callable[[Progress], None]) -> None:
        self.send = send
        self.stage, self.message = Stage.READING, "Reading the question"
        self.requests = self.fetched = self.expected = 0

    def emit(self) -> None:
        self.send(
            Progress(
                stage=self.stage,
                message=self.message,
                registry_requests=self.requests,
                records_fetched=self.fetched,
                records_expected=self.expected,
            )
        )


_tracker: ContextVar[_Tracker | None] = ContextVar("progress", default=None)


@contextmanager
def listening(send: Callable[[Progress], None]) -> Iterator[None]:
    """Report every step taken inside the block to `send`."""
    token = _tracker.set(_Tracker(send))
    try:
        yield
    finally:
        _tracker.reset(token)


def stage(name: Stage, message: str) -> None:
    if tracker := _tracker.get():
        tracker.stage, tracker.message = name, message
        tracker.emit()


def registry_request() -> None:
    if tracker := _tracker.get():
        tracker.requests += 1
        tracker.emit()


def records(fetched: int, expected: int = 0) -> None:
    """Records downloaded by one page, and on a fetch's first page, how many it will get."""
    if tracker := _tracker.get():
        tracker.fetched += fetched
        tracker.expected += expected
        tracker.emit()
