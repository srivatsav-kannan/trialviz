"""What the API reference at /docs says beyond the models themselves: the overview, the
endpoint groups, the error shape, and request and response examples.

Response examples are cut down from a saved answer in examples/, so the reference shows
what the service really returns. Request examples are validated against `QueryRequest`
when this module loads, so a stale example fails at startup instead of misleading a reader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from trialviz.query.request import QueryRequest

SAMPLES_DIRECTORY = Path(__file__).parents[2] / "examples"
"""Saved responses to real questions, written by examples/run_examples.py."""

DESCRIPTION = """
trialviz answers questions about clinical trials with chart specifications backed by the
[ClinicalTrials.gov API](https://clinicaltrials.gov/data-api/api). You send a question in
plain English, optionally with structured fields, and get back a recommended chart with the
reason it was chosen, every other chart that can draw the same data, a Vega-Lite spec where
Vega-Lite can draw the chart, and a citation behind every number.

## Getting started

```bash
curl -s http://localhost:8000/v1/query \\
  -H 'content-type: application/json' \\
  -d '{"query": "How has the number of trials for this drug changed over time?",
       "drug_name": "Pembrolizumab"}'
```

Use `POST /v1/interpret` with the same body to see how a question will be read before
anything is fetched.

## How a request is read

Structured fields and the question fill the same plan. Any field you set is binding, the
question fills the fields you leave unset, and documented defaults cover the rest. Every
answer reports where each plan value came from in `meta.provenance` (`user`, `inferred`,
`derived` or `default`), and `meta.notes` records every decision a reader might otherwise
misjudge, such as a field that overrode the question or a constraint the plan could not
express. Setting `"inference": "off"` skips the language model entirely and makes the
request deterministic.

## Citations

Each data point carries `trial_count`, the IDs of every trial behind it in `nct_ids`, and
`citations`. A citation names a trial, a JSON path into that trial's record in the
ClinicalTrials.gov API, and the exact value found there, so any number can be checked
against the registry by following the path.

## Errors

Every error has the same shape, `{"error": {"code", "message", "details"}}`, where
`details` lists `{"path", "message"}` pairs for validation errors.

| Status | Code | Meaning |
| --- | --- | --- |
| 404 | `sample_not_found` | No saved sample has that name. |
| 422 | `invalid_request` | The body breaks the schema, or a value is ambiguous ("Korea"). |
| 502 | `registry_rejected_query` | ClinicalTrials.gov rejected the query it was sent. |
| 503 | `registry_unavailable` | ClinicalTrials.gov could not be reached after retries. |

## Limits

Registry requests are spaced to 50 per minute, and at most 10,000 trial records are fetched
per cohort, with group counts taken from the registry itself when more trials match.
Answers reflect the registry at the time stated in `meta.data_version`.
""".strip()

TAGS = [
    {
        "name": "Answers",
        "description": "Turn a question into a chart specification with citations.",
    },
    {
        "name": "Planning",
        "description": "See how a question is read without fetching any data.",
    },
    {
        "name": "Samples",
        "description": "Saved answers to real questions, returned exactly as they were produced.",
    },
    {"name": "Service", "description": "Operational endpoints."},
]


class ErrorDetail(BaseModel):
    path: str | None = Field(description="Where in the request the problem is, if anywhere.")
    message: str = Field(description="What is wrong, in a sentence.")


class ErrorBody(BaseModel):
    code: str = Field(description="A stable, machine-readable error code.")
    message: str = Field(description="A summary a person can read.")
    details: list[ErrorDetail] = Field(description="One entry per problem, for validation errors.")


class ErrorResponse(BaseModel):
    """The shape of every error the service returns."""

    error: ErrorBody


class Health(BaseModel):
    status: str = Field(description="`ok` while the service is running.")


class SampleCohort(BaseModel):
    label: str | None = Field(description="The cohort's label in a comparison, or null.")
    total: int = Field(description="Exact number of matching trials.")


class SampleSummary(BaseModel):
    """One saved answer, summarized for listing."""

    name: str = Field(description="Pass this to `GET /v1/samples/{name}`.")
    request: dict[str, Any] = Field(description="The request body that produced the answer.")
    title: str = Field(description="The recommended chart's title.")
    chart_type: str = Field(description="The recommended chart type.")
    alternatives: list[str] = Field(description="The alternative chart types offered.")
    cohorts: list[SampleCohort]
    data_version: str = Field(description="The registry's data timestamp at the time.")


REQUEST_EXAMPLES: dict[str, dict[str, Any]] = {
    "assignment": {
        "summary": "A question with one structured field",
        "description": 'The assignment\'s example. The question refers to "this drug", '
        "and `drug_name` supplies it.",
        "value": {
            "query": "How has the number of trials for this drug changed over time?",
            "drug_name": "Pembrolizumab",
        },
    },
    "comparison": {
        "summary": "A comparison read entirely from the question",
        "description": "Claude reads two cohorts and a shared start-year bound.",
        "value": {"query": "Compare phases for trials involving Keytruda vs Opdivo since 2018"},
    },
    "structured": {
        "summary": "Fully structured, with inference off",
        "description": "Every value is given, so the language model is not called and the "
        "answer is deterministic.",
        "value": {
            "query": "Recruiting phase 3 melanoma trials by country",
            "inference": "off",
            "filters": {
                "conditions": ["melanoma"],
                "phases": ["PHASE3"],
                "statuses": ["RECRUITING"],
            },
            "analysis": {"kind": "distribution", "group_by": "country"},
            "presentation": {"chart_type": "choropleth_map"},
        },
    },
    "network": {
        "summary": "A network of sponsors and drugs",
        "value": {
            "query": "Show a network of sponsors and drugs for multiple myeloma trials "
            "that started in 2024"
        },
    },
    "mentions": {
        "summary": "Counting trials that only mention the drug",
        "description": "By default, trials that mention a name only in their descriptions are "
        "reported in `meta.mentions` and left out of the counts. This counts them too.",
        "value": {
            "query": "How many semaglutide trials are recruiting?",
            "retrieval": {"name_matching": "mentioned"},
        },
    },
}

for _example in REQUEST_EXAMPLES.values():
    QueryRequest.model_validate(_example["value"])


def _error_example(status: int, code: str, message: str, details: list[dict[str, Any]]) -> Any:
    return {
        "model": ErrorResponse,
        "content": {
            "application/json": {
                "example": {"error": {"code": code, "message": message, "details": details}}
            }
        },
    }


INVALID_REQUEST = _error_example(
    422,
    "invalid_request",
    "The request does not match the schema. See details.",
    [
        {
            "path": "filters.countries[0]",
            "message": "'Korea' could mean South Korea or North Korea. Name the country exactly.",
        }
    ],
)
REGISTRY_REJECTED = _error_example(
    502,
    "registry_rejected_query",
    "ClinicalTrials.gov rejected the query: <the registry's own error text>",
    [],
)
REGISTRY_UNAVAILABLE = _error_example(
    503,
    "registry_unavailable",
    "ClinicalTrials.gov did not answer /studies after 4 attempts (last: HTTP 503)",
    [],
)
SAMPLE_NOT_FOUND = _error_example(
    404, "sample_not_found", "There is no saved sample named 'missing'.", []
)


def _trim(value: Any, depth: int = 0) -> Any:
    """A saved answer cut down to a readable size: three rows, three trial IDs and one
    citation per datum, keeping every field so the shape stays exact."""
    if isinstance(value, list):
        keep = 1 if depth and value and isinstance(value[0], dict) and "nct_id" in value[0] else 3
        return [_trim(item, depth + 1) for item in value[:keep]]
    if isinstance(value, dict):
        return {key: _trim(item, depth + 1) for key, item in value.items()}
    return value


def _saved_answer() -> dict[str, Any] | None:
    path = SAMPLES_DIRECTORY / "01_assignment_example.json"
    if not path.is_file():
        return None
    answer: dict[str, Any] = json.loads(path.read_text())["response"]
    return answer


def answer_examples() -> dict[str, Any]:
    examples: dict[str, Any] = {
        "clarification": {
            "summary": "The question needs more detail",
            "value": {
                "status": "needs_clarification",
                "reason": "ambiguous_reference",
                "message": 'The question refers to "this drug" without naming it. Name it '
                "in the question, or set filters.interventions (or drug_name).",
                "missing": [],
                "notes": [],
                "interpreter": "rules",
            },
        }
    }
    if answer := _saved_answer():
        examples = {
            "answer": {
                "summary": "An answer (trimmed from a saved response)",
                "value": _trim(answer),
            },
            **examples,
        }
    return examples


def resolution_examples() -> dict[str, Any]:
    answer = _saved_answer()
    if answer is None:
        return {}
    meta = answer["meta"]
    return {
        "resolved": {
            "summary": "The plan for the assignment's example",
            "value": {
                "status": "resolved",
                "plan": meta["plan"],
                "provenance": meta["provenance"],
                "notes": meta["notes"],
                "interpreter": meta["interpreter"],
            },
        }
    }
