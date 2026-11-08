"""The request pipeline: resolve the question, fetch what the plan needs, aggregate it, and
present it as a recommended visualization with alternatives.

Each stage is independent and testable alone. This module only sequences them. It stops
after resolution when the question needs clarification, since there is nothing to fetch
for a question that is not yet understood.
"""

from __future__ import annotations

from dataclasses import dataclass

from trialviz.aggregation.build import build
from trialviz.aggregation.exact import apply_exact_counts
from trialviz.answer import Answer, present
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import ClarificationNeeded, ResolvedQuery
from trialviz.query.resolver import QueryResolver
from trialviz.retrieval.retriever import Retrieval, Retriever


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
    def __init__(self, resolver: QueryResolver, retriever: Retriever) -> None:
        self._resolver = resolver
        self._retriever = retriever

    async def run(self, request: QueryRequest) -> PipelineResult:
        resolution = await self._resolver.resolve(request)
        if isinstance(resolution, ClarificationNeeded):
            return PipelineResult(resolution, None, None)
        plan = resolution.plan
        retrieval = await self._retriever.retrieve(plan)
        built = build(plan, retrieval)
        await apply_exact_counts(
            plan, retrieval, built, self._retriever.count, self._retriever.first_year
        )
        return PipelineResult(
            resolution, retrieval, present(request.query, resolution, retrieval, built)
        )
