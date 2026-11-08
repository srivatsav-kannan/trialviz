"""The request pipeline: resolve the question, fetch what the plan needs, aggregate it, and
present it as a recommended visualization with alternatives.

Each stage is independent and testable alone. This module only sequences them. It stops
after resolution when the question needs clarification, since there is nothing to fetch
for a question that is not yet understood.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from trialviz import progress
from trialviz.aggregation.build import build
from trialviz.aggregation.exact import apply_exact_counts
from trialviz.answer import Answer, present
from trialviz.progress import Stage
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import ClarificationNeeded, ResolvedQuery
from trialviz.query.resolver import QueryResolver
from trialviz.retrieval.retriever import RequestLog, Retrieval, Retriever


class AnswerTimeout(Exception):
    """The registry phase of an answer ran past its deadline."""


@dataclass(frozen=True)
class PipelineResult:
    resolution: ResolvedQuery | ClarificationNeeded
    retrieval: Retrieval | None
    """None when the question needs clarification."""
    answer: Answer | None
    """None when the question needs clarification."""

    @property
    def response(self) -> Answer | ClarificationNeeded:
        if self.answer is not None:
            return self.answer
        assert isinstance(self.resolution, ClarificationNeeded)
        return self.resolution


class Pipeline:
    def __init__(
        self,
        resolver: QueryResolver,
        retriever: Retriever,
        *,
        deadline_seconds: float | None = None,
    ) -> None:
        self._resolver = resolver
        self._retriever = retriever
        self._deadline = deadline_seconds

    async def run(self, request: QueryRequest) -> PipelineResult:
        progress.stage(Stage.READING, "Reading the question")
        resolution = await self._resolver.resolve(request)
        if isinstance(resolution, ClarificationNeeded):
            return PipelineResult(resolution, None, None)
        plan = resolution.plan
        log: RequestLog = []
        try:
            async with asyncio.timeout(self._deadline):
                progress.stage(Stage.RETRIEVING, "Finding and fetching the matching trials")
                retrieval = await self._retriever.retrieve(plan)
                progress.stage(Stage.AGGREGATING, "Grouping and counting the trials")
                built = await asyncio.to_thread(build, plan, retrieval)
                await apply_exact_counts(
                    plan, retrieval, built, self._retriever.count, self._retriever.first_year, log
                )
        except TimeoutError as error:
            raise AnswerTimeout(
                f"Answering took longer than {self._deadline:g} seconds of registry requests. "
                "Narrow the question, for example with a phase, a status or a start year."
            ) from error
        progress.stage(Stage.PRESENTING, "Choosing the chart")
        answer = await asyncio.to_thread(present, request.query, resolution, retrieval, built, log)
        return PipelineResult(resolution, retrieval, answer)
