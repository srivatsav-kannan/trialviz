"""Score interpreters on the labeled questions in cases.json.

Each case runs through the full resolver, so a case passes only if the final plan (or
clarification) is right, not merely the interpreter's raw guess. Expectations are partial
on purpose: a case names the slots that matter and those that must stay empty, and says
nothing about the rest.

    uv run python -m evals.run --interpreter rules
    uv run python -m evals.run --interpreter claude   # spends against the cap

Results are written to evals/results/<interpreter>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from trialviz.query.consistency import same_meaning
from trialviz.query.interpreters.base import Interpreter
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.plan import ComparisonSpec
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import ClarificationNeeded, ResolvedQuery
from trialviz.query.resolver import QueryResolver
from trialviz.service import build_interpreter
from trialviz.settings import Settings

HERE = Path(__file__).parent
TODAY = date(2026, 11, 7)  # fixed so relative-year cases have one right answer


def failures_for(outcome: ResolvedQuery | ClarificationNeeded, expect: dict[str, Any]) -> list[str]:
    """Everything about the outcome that contradicts the expectation; empty means a pass.

    `any_of` lists alternative expectations for questions with more than one fair reading;
    the case passes if any of them does.
    """
    if alternatives := expect.get("any_of"):
        attempts = [failures_for(outcome, alternative) for alternative in alternatives]
        if any(not problems for problems in attempts):
            return []
        return [f"reading {i + 1}: {'; '.join(p)}" for i, p in enumerate(attempts)]
    if outcome.status != expect["status"]:
        detail = outcome.message if isinstance(outcome, ClarificationNeeded) else "a plan"
        return [f"expected {expect['status']}, got {outcome.status} ({detail})"]
    if isinstance(outcome, ClarificationNeeded):
        reason = expect.get("reason")
        return (
            [f"reason {outcome.reason.value} != {reason}"]
            if reason and outcome.reason.value != reason
            else []
        )

    plan = outcome.plan.model_dump(mode="json")
    slots = {
        f"{s}.{n}": v for s, values in plan.items() for n, v in values.items() if v is not None
    }
    problems = []
    for path, expected in expect.get("slots", {}).items():
        if not same_meaning(slots.get(path), expected):
            problems.append(f"{path}: expected {expected!r}, got {slots.get(path)!r}")
    for path in expect.get("present", []):
        if path not in slots:
            problems.append(f"{path}: expected a value, got none")
    for path in expect.get("absent", []):
        if path in slots:
            problems.append(f"{path}: expected empty, got {slots[path]!r}")
    for path, banned in expect.get("excluded_values", {}).items():
        if set(banned) & set(slots.get(path) or []):
            problems.append(f"{path}: must not contain {banned!r}, got {slots[path]!r}")
    if cohorts := expect.get("cohorts"):
        problems.extend(_cohort_failures(outcome, cohorts))
    if cohort_filters := expect.get("cohort_filters"):
        problems.extend(_cohort_filter_failures(outcome, cohort_filters))
    codes = {f"{n.code.value}:{n.slot}" for n in outcome.notes}
    problems.extend(f"missing note {note}" for note in expect.get("notes", []) if note not in codes)
    problems.extend(
        f"unexpected note {note}" for note in expect.get("forbidden_notes", []) if note in codes
    )
    return problems


def _cohort_failures(outcome: ResolvedQuery, expected: dict[str, Any]) -> list[str]:
    analysis = outcome.plan.analysis
    if not isinstance(analysis, ComparisonSpec):
        return ["expected cohorts, but the plan is not a comparison"]
    labels = {cohort.label.casefold() for cohort in analysis.cohorts}
    problems = []
    if labels != {label.casefold() for label in expected["labels"]}:
        problems.append(f"cohort labels {sorted(labels)} != {expected['labels']}")
    field = expected["field"]
    untyped = [c.label for c in analysis.cohorts if getattr(c.filters, field) is None]
    if untyped:
        problems.append(f"cohorts {untyped} do not set filters.{field}")
    return problems


def _cohort_filter_failures(outcome: ResolvedQuery, expected: list[dict[str, Any]]) -> list[str]:
    """Each expected filter set must be contained in a different cohort, labels aside."""
    analysis = outcome.plan.analysis
    if not isinstance(analysis, ComparisonSpec):
        return ["expected cohorts, but the plan is not a comparison"]
    remaining = [
        cohort.filters.model_dump(mode="json", exclude_none=True) for cohort in analysis.cohorts
    ]
    problems = []
    for wanted in expected:
        match = next(
            (c for c in remaining if all(same_meaning(c.get(k), v) for k, v in wanted.items())),
            None,
        )
        if match is None:
            problems.append(f"no cohort has filters {wanted!r}; cohorts: {remaining!r}")
        else:
            remaining.remove(match)
    return problems


async def run(
    interpreter: Interpreter, cases: list[dict[str, Any]], concurrency: int
) -> list[dict[str, Any]]:
    resolver = QueryResolver(interpreter, today=lambda: TODAY)
    gate = asyncio.Semaphore(concurrency)

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        async with gate:
            outcome = await resolver.resolve(QueryRequest.model_validate(case["request"]))
        problems = failures_for(outcome, case["expect"])
        return {
            "id": case["id"],
            "tags": case["tags"],
            "passed": not problems,
            "problems": problems,
            "interpreter": outcome.interpreter,
            "outcome": outcome.model_dump(mode="json", exclude_none=True),
        }

    return list(await asyncio.gather(*(one(case) for case in cases)))


def summarize(results: list[dict[str, Any]]) -> str:
    by_tag: dict[str, list[bool]] = defaultdict(list)
    for result in results:
        for tag in result["tags"]:
            by_tag[tag].append(result["passed"])
    passed = sum(r["passed"] for r in results)
    lines = [f"overall: {passed}/{len(results)}"]
    lines += [f"  {tag:<18} {sum(v)}/{len(v)}" for tag, v in sorted(by_tag.items())]
    lines += [f"FAIL {r['id']}: {'; '.join(r['problems'])}" for r in results if not r["passed"]]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--interpreter", choices=["rules", "claude"], required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    if args.interpreter == "claude":
        settings = Settings()
        if settings.anthropic_api_key is None:
            raise SystemExit("ANTHROPIC_API_KEY is not set")
        interpreter = build_interpreter(settings)
    else:
        interpreter = RuleBasedInterpreter()

    cases = json.loads((HERE / "cases.json").read_text())
    results = asyncio.run(run(interpreter, cases, args.concurrency))
    fell_back = [
        r["id"] for r in results if args.interpreter == "claude" and r["interpreter"] == "rules"
    ]
    if fell_back:
        print(f"warning: these cases fell back to the rules interpreter: {fell_back}")

    output = HERE / "results" / f"{args.interpreter}.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({"today": TODAY.isoformat(), "results": results}, indent=2) + "\n")
    print(summarize(results))


if __name__ == "__main__":
    main()
