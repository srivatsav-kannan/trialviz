"""Run the example questions against the real services and save request and response.

    uv run python -m examples.run_examples

Each example is written to examples/<name>.json as {"request", "response"}, exactly as
`POST /v1/query` would return it, and examples/README.md is regenerated from those files.
Uses Claude when ANTHROPIC_API_KEY is set (a few cents each, against the spend cap).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from trialviz.api import build_interpreter
from trialviz.pipeline import Pipeline
from trialviz.query.request import QueryRequest
from trialviz.query.resolver import QueryResolver
from trialviz.retrieval.retriever import build_retriever
from trialviz.settings import Settings

HERE = Path(__file__).parent

EXAMPLES: list[tuple[str, dict[str, Any]]] = [
    (
        "01_assignment_example",
        {
            "query": "How has the number of trials for this drug changed over time?",
            "drug_name": "Pembrolizumab",
        },
    ),
    (
        "02_compare_keytruda_opdivo",
        {"query": "Compare phases for trials involving Keytruda vs Opdivo since 2018"},
    ),
    (
        "03_recruiting_melanoma_by_country",
        {"query": "Which countries have the most recruiting melanoma trials?"},
    ),
    (
        "04_myeloma_sponsor_drug_network",
        {
            "query": "Show a network of sponsors and drugs for multiple myeloma trials "
            "that started in 2024"
        },
    ),
    (
        "05_lung_cancer_flow",
        {
            "query": "How do lung cancer trials started in 2016 flow from sponsor class to phase "
            "to overall status?"
        },
    ),
]


async def main() -> None:
    settings = Settings()
    pipeline = Pipeline(QueryResolver(build_interpreter(settings)), build_retriever(settings))
    for name, request in EXAMPLES:
        result = await pipeline.run(QueryRequest.model_validate(request))
        payload = {"request": request, "response": result.response.model_dump(mode="json")}
        (HERE / f"{name}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {name}.json")
    write_readme()


def write_readme() -> None:
    lines = [
        "# Example runs",
        "",
        "Real questions answered by the running service, against the live registry, with the",
        "actual JSON each one produced. Regenerate with `uv run python -m examples.run_examples`.",
        "Because the registry updates daily, a rerun gives slightly different counts.",
        "",
        "| Example | Recommended chart, and why | Alternatives | Trials |",
        "| --- | --- | --- | --- |",
    ]
    for name, _ in EXAMPLES:
        response = json.loads((HERE / f"{name}.json").read_text())["response"]
        view, meta = response["visualization"], response["meta"]
        alternatives = ", ".join(a["type"] for a in response["alternatives"])
        trials = " / ".join(f"{c['total']:,}" for c in meta["cohorts"])
        chart = f"`{view['type']}`: {view['reason']}"
        lines.append(f"| [{view['title']}]({name}.json) | {chart} | {alternatives} | {trials} |")
    lines += [
        "",
        "Each file shows the full response: the visualization with its rows and citations, the",
        "alternative views, and `meta` with the resolved plan, the source of every plan value,",
        "every note, how the chart was chosen (including the charts ruled out and why), the trials",
        "that only mention the drug or condition, and every registry request made.",
    ]
    (HERE / "README.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
