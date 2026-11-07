"""The 49 eval questions through the deterministic path: rule-based interpreter, resolver.

The rule-based interpreter and the resolver are fully deterministic, so their eval results
are a snapshot. This test reruns every question offline and fails if any case changes
outcome, in either direction. A regression shows up as a newly failing case. An
improvement shows up too, and the committed results must be regenerated to accept it, so
no change to this path goes unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.interpretation.run import run
from trialviz.query.interpreters.rules import RuleBasedInterpreter

EVAL = Path(__file__).parent.parent / "evals" / "interpretation"


async def test_rule_based_eval_outcomes_match_the_committed_snapshot() -> None:
    cases = json.loads((EVAL / "cases.json").read_text())
    committed = json.loads((EVAL / "results" / "rules.json").read_text())["results"]
    results = await run(RuleBasedInterpreter(), cases, concurrency=8)

    expected = {r["id"]: (r["passed"], r["outcome"]) for r in committed}
    changed = [r["id"] for r in results if expected.get(r["id"]) != (r["passed"], r["outcome"])]
    assert changed == [], (
        f"outcomes changed for {changed}. If intended, regenerate with "
        "`uv run python -m evals.interpretation.run --interpreter rules`."
    )
