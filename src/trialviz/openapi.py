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
anything is fetched, and `POST /v1/query/stream` to receive progress events while a long
answer is computed. Every answer has a `meta.answer_id`: `GET /v1/answers/{id}` reopens it
at no cost, `GET /v1/answers/{id}/csv` downloads its rows, and asking the same question
again against the same registry data returns the saved answer without spending anything.
Any web page can call the API, since it allows every origin, and AI agents can use the same
answers as tools over the Model Context Protocol at `/mcp`.

## How a request is read

Structured fields and the question fill the same plan. Any field you set is binding, the
question fills the fields you leave unset, and documented defaults cover the rest. Every
answer reports where each plan value came from in `meta.provenance` (`user`, `inferred`,
`derived` or `default`), and `meta.notes` records every decision a reader might otherwise
misjudge, such as a field that overrode the question or a constraint that the plan could not
express. Setting `"inference": "off"` skips the language model entirely and makes the
request deterministic.

## Citations

Each data point carries a stable `id`, `trial_count`, the IDs of the trials behind it in
`nct_ids`, and `citations`. The rows of every chart type are typed in this document's
schemas (`CategoryRow`, `PeriodRow`, `GraphData` and the rest). A citation names a trial,
a JSON path into that trial's record in the ClinicalTrials.gov API, and the exact value
found there, so any number can be checked against the registry by following the path.

## Errors

Every error has the same shape, `{"error": {"code", "message", "details"}}`, where
`details` lists `{"path", "message"}` pairs for validation errors.

| Status | Code | Meaning |
| --- | --- | --- |
| 404 | `not_found` | There is nothing at this path. |
| 404 | `sample_not_found` | No saved sample has that name. |
| 404 | `answer_not_found` | No saved answer has that id, or it has expired. |
| 405 | `method_not_allowed` | The path does not accept that method. |
| 413 | `request_too_large` | The request body is larger than the service accepts. |
| 422 | `invalid_request` | The body breaks the schema, or a value is ambiguous, such as "Korea". |
| 429 | `rate_limited` | Too many requests; `Retry-After` says when to try again. |
| 500 | `internal_error` | The service failed unexpectedly. |
| 502 | `registry_rejected_query` | ClinicalTrials.gov rejected the query it was sent. |
| 503 | `registry_unavailable` | ClinicalTrials.gov could not be reached after retries. |
| 503 | `busy` | The service is answering as many questions as it allows at once; retry shortly. |
| 504 | `answer_timeout` | The answer needed more registry time than its deadline. |

## Limits

Questions (`POST /v1/query`, its stream, `POST /v1/interpret` and the MCP tools) are
limited per client to 10 a minute and 60 an hour by default, and to 600 an hour across all
clients. Every other request is limited to 120 a minute per client. Each question response
carries the `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset` headers, a
refused one carries `Retry-After`, and `GET /v1/status` shows your standing and how much of
the language-model budget is left. Four questions are answered at once, and request bodies
over 16 KB are refused.

Registry requests are spaced to 50 per minute, and at most 10,000 trial records are fetched
per question, shared among the cohorts of a comparison, with group counts taken from the
registry itself when more trials match. An answer may spend at most 180 seconds on registry
requests by default, after which it returns a 504. Answers reflect the registry at the time
stated in `meta.data_version`, which a running server checks again every 15 minutes.
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


class BudgetStatus(BaseModel):
    cap_usd: float = Field(description="Total the language model may ever spend.")
    daily_cap_usd: float | None = Field(description="Most it may spend in one UTC day, if set.")
    spent_usd: float = Field(description="Spent so far, including calls in progress.")
    spent_today_usd: float = Field(description="Spent today (UTC), including calls in progress.")
    remaining_usd: float = Field(description="What is left under both caps.")


class LimitStatus(BaseModel):
    questions_per_minute: int = Field(description="Questions one client may ask per minute.")
    questions_per_hour: int = Field(description="Questions one client may ask per hour.")
    questions_remaining: int = Field(description="Questions you may still ask now.")
    resets_in_seconds: int = Field(description="Seconds until another question frees up.")
    answers_in_progress: int = Field(description="Questions being answered right now.")
    max_answers_in_progress: int = Field(description="Most questions answered at once.")
    max_body_bytes: int = Field(description="Largest request body accepted.")


class ServiceStatus(BaseModel):
    """What the service can do right now, and your standing against its limits."""

    interpreter: str = Field(description="`claude`, or `rules` when no key is configured.")
    claude_available: bool = Field(
        description="Whether Claude will read the next question. False when no key is "
        "configured, the budget is used up, or the spend ledger cannot be reached."
    )
    reason: str | None = Field(description="Why Claude is unavailable, when it is.")
    budget: BudgetStatus | None = Field(description="The language-model budget, if any.")
    limits: LimitStatus
    client: str = Field(description="Your address as the service sees it.")


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


RETRY_AFTER = {
    "Retry-After": {
        "description": "Seconds to wait before trying again.",
        "schema": {"type": "integer"},
    }
}
RATE_LIMIT_HEADERS = {
    "RateLimit-Limit": {
        "description": "Questions allowed in the client's tightest window, a minute or an hour.",
        "schema": {"type": "integer"},
    },
    "RateLimit-Remaining": {
        "description": "Questions left in that window.",
        "schema": {"type": "integer"},
    },
    "RateLimit-Reset": {
        "description": "Seconds until a question in that window stops counting.",
        "schema": {"type": "integer"},
    },
}


def _error_example(
    status: int,
    code: str,
    message: str,
    details: list[dict[str, Any]],
    headers: dict[str, Any] | None = None,
) -> Any:
    return {
        "model": ErrorResponse,
        **({"headers": headers} if headers else {}),
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
REQUEST_TOO_LARGE = _error_example(
    413, "request_too_large", "The request body is larger than 16384 bytes.", []
)
RATE_LIMITED = _error_example(
    429,
    "rate_limited",
    "Too many questions: the limit is 10 per minute. Try again in 42 seconds.",
    [],
    RETRY_AFTER,
)
SERVICE_BUSY = _error_example(
    503, "busy", "The service is already answering 4 questions. Try again shortly.", [], RETRY_AFTER
)
ANSWER_TIMEOUT = _error_example(
    504,
    "answer_timeout",
    "The answer needed more than 180 seconds of registry requests. Narrow the question.",
    [],
)
INTERNAL_ERROR = _error_example(
    500, "internal_error", "The service failed unexpectedly (KeyError).", []
)
REGISTRY_UNAVAILABLE_OR_BUSY = {
    "model": ErrorResponse,
    "description": "The registry could not be reached, or the service is answering as many "
    "questions as it allows at once.",
    "headers": {"Retry-After": {**RETRY_AFTER["Retry-After"], "description": "Sent when busy."}},
    "content": {
        "application/json": {
            "examples": {
                "registry_unavailable": {
                    "value": REGISTRY_UNAVAILABLE["content"]["application/json"]["example"]
                },
                "busy": {"value": SERVICE_BUSY["content"]["application/json"]["example"]},
            }
        }
    },
}
INVALID_PARAMETER = _error_example(
    422,
    "invalid_request",
    "The request does not match the schema. See details.",
    [{"path": "part", "message": "Input should be 'links' or 'nodes'"}],
)


def json_error(response: Any) -> Any:
    """The same error response for a route whose success is not JSON (a CSV download), so
    its errors are still declared as the JSON envelope they are sent as."""
    content = response["content"]["application/json"]
    return {
        "description": "Error",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                "example": content["example"],
            }
        },
    }


class SavedRequest(BaseModel):
    """What a saved answer was asked, kept after the answer itself expires."""

    request: dict[str, Any] = Field(
        description="The request as the service read it, with shorthands such as `drug_name` "
        "moved into `filters`, ready to send to `POST /v1/query` again."
    )
    answer_available: bool = Field(
        description="Whether the original answer can still be reopened at /v1/answers/{id}."
    )


ANSWER_NOT_FOUND = _error_example(
    404, "answer_not_found", "There is no saved answer '0123456789abcdef0123'.", []
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
                "missing": ["filters.interventions"],
                "notes": [],
                "interpreter": "rules",
            },
        }
    }
    if answer := _saved_answer():
        examples = {
            "answer": {
                "summary": "An answer (trimmed from a saved response)",
                "description": "Cut down to three rows, three trial IDs and one citation per "
                "datum, so a `trial_count` here can exceed the IDs shown. Real answers list "
                "every trial behind each count, as GET /v1/samples/{name} shows.",
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
