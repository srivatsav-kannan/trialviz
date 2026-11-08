"""Run the example questions against the real services and save request and response.

    uv run python -m examples.run_examples          # every example
    uv run python -m examples.run_examples 06 07    # only those whose names start so

Each example is written to examples/<name>.json as {"request", "response"}, exactly as
`POST /v1/query` would return it. The demo UI's samples are read from these files.
Uses Claude when ANTHROPIC_API_KEY is set (a few cents each, against the spend cap).
"""

from __future__ import annotations

import asyncio
import json
import sys
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
            "that started in 2024",
            # Claude has read "drugs" both as a node type and as a filter on intervention
            # type. The structured field pins the second reading, as a caller would.
            "filters": {"intervention_types": ["DRUG"]},
        },
    ),
    (
        "05_lung_cancer_flow",
        {
            "query": "How do lung cancer trials started in 2016 flow from sponsor class to phase "
            "to overall status?"
        },
    ),
    (
        "06_semaglutide_recruiting_germany",
        {"query": "How many semaglutide trials are recruiting in Germany?"},
    ),
    (
        "07_asthma_enrollment_by_phase",
        {"query": "How does enrollment vary by phase for completed asthma trials?"},
    ),
    (
        "08_psoriasis_size_duration_sites",
        {
            "query": "How do enrollment, duration and number of sites relate for completed "
            "phase 3 psoriasis trials?"
        },
    ),
    (
        "09_tirzepatide_timeline",
        {"query": "List the recruiting phase 3 trials of tirzepatide"},
    ),
    (
        "10_melanoma_drug_combinations",
        {
            "query": "Which drugs frequently co-occur in combination studies for melanoma "
            "trials that started since 2020?"
        },
    ),
    (
        "11_sponsor_classes_two_cancers",
        {"query": "Compare sponsor categories across breast cancer and prostate cancer trials"},
    ),
    (
        "12_alzheimers_intervention_types",
        {"query": "What are the most common intervention types for Alzheimer's disease trials?"},
    ),
    (
        "13_crohns_study_types",
        {"query": "Show the split of Crohn's disease trials by study type as a donut chart"},
    ),
    (
        "14_obesity_phases_over_time",
        {"query": "How has the number of obesity trials in each phase changed since 2016?"},
    ),
]


async def main(only: list[str]) -> None:
    """Run every example, or only those whose names start with one of `only`."""
    settings = Settings()
    pipeline = Pipeline(QueryResolver(build_interpreter(settings)), build_retriever(settings))
    for name, request in EXAMPLES:
        if only and not any(name.startswith(prefix) for prefix in only):
            continue
        result = await pipeline.run(QueryRequest.model_validate(request))
        payload = {"request": request, "response": result.response.model_dump(mode="json")}
        (HERE / f"{name}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {name}.json")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
