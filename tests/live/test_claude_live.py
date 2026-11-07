"""A few real calls to Claude through the full resolver. Run with `pytest --run-live`.

They spend real money against the configured cap (about $0.03 each), so there are only a
handful, chosen for things the rule-based interpreter cannot do: typing entity names,
resolving a reference from the caller's fields, and recognizing an unanswerable question.
Broader accuracy is measured by `evals/interpretation`, not here.
"""

from __future__ import annotations

import pytest

from tests.support import TODAY, clarification, resolved
from trialviz.api import build_interpreter
from trialviz.query.plan import ComparisonSpec, DistributionSpec
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import ClarificationReason
from trialviz.query.resolver import QueryResolver
from trialviz.settings import Settings

pytestmark = pytest.mark.live


@pytest.fixture
def resolver() -> QueryResolver:
    settings = Settings()
    if settings.anthropic_api_key is None:
        pytest.skip("ANTHROPIC_API_KEY is not set")
    return QueryResolver(build_interpreter(settings), today=lambda: TODAY)


async def test_brand_names_are_typed_as_interventions_and_compared(resolver: QueryResolver) -> None:
    outcome = resolved(
        await resolver.resolve(
            QueryRequest(query="Compare phases of Keytruda vs Opdivo trials since 2018")
        )
    )
    assert (outcome.interpreter or "").startswith("claude:")
    analysis = outcome.plan.analysis
    assert isinstance(analysis, ComparisonSpec)
    assert {cohort.label.casefold() for cohort in analysis.cohorts} == {"keytruda", "opdivo"}
    assert all(cohort.filters.interventions for cohort in analysis.cohorts)
    assert outcome.plan.filters.start_year_from == 2018


async def test_a_reference_is_resolved_from_the_callers_fields(resolver: QueryResolver) -> None:
    outcome = resolved(
        await resolver.resolve(
            QueryRequest.model_validate(
                {
                    "query": "Which countries run the most recruiting trials of this drug?",
                    "drug_name": "Ozempic",
                }
            )
        )
    )
    assert outcome.plan.filters.interventions == ["Ozempic"]
    assert outcome.plan.filters.statuses == ["RECRUITING"]
    analysis = outcome.plan.analysis
    assert isinstance(analysis, DistributionSpec)
    assert analysis.group_by == "country"


async def test_an_unanswerable_question_is_flagged(resolver: QueryResolver) -> None:
    outcome = clarification(
        await resolver.resolve(QueryRequest(query="Which PD-1 inhibitor is the most effective?"))
    )
    assert outcome.reason is ClarificationReason.OUT_OF_SCOPE
