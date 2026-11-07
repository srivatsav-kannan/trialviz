"""The request pipeline: resolve the question, then fetch what the plan needs.

Each stage is independent and testable alone. This module only sequences them, and it
stops early when resolution needs clarification, since there is nothing to fetch for a
question that is not yet understood. The aggregation and visualization stages extend it.
"""

from __future__ import annotations

from dataclasses import dataclass

from trialviz.query.request import QueryRequest
from trialviz.query.resolution import ClarificationNeeded, ResolvedQuery
from trialviz.query.resolver import QueryResolver
from trialviz.retrieval.retriever import Retrieval, Retriever


@dataclass(frozen=True)
class PipelineResult:
    resolution: ResolvedQuery | ClarificationNeeded
    retrieval: Retrieval | None
    """None when the question needs clarification."""


class Pipeline:
    def __init__(self, resolver: QueryResolver, retriever: Retriever) -> None:
        self._resolver = resolver
        self._retriever = retriever

    async def run(self, request: QueryRequest) -> PipelineResult:
        resolution = await self._resolver.resolve(request)
        if isinstance(resolution, ClarificationNeeded):
            return PipelineResult(resolution, None)
        return PipelineResult(resolution, await self._retriever.retrieve(resolution.plan))
