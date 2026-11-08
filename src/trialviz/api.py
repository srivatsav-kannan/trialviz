"""HTTP interface.

`POST /v1/query` answers a question with a recommended visualization, alternative views
of the same data, and citations to the registry records behind every number.

`POST /v1/interpret` resolves a request into a plan without fetching any data, so a
caller can see exactly how a question was read (and which values came from where) before
running it.

Every error response has one shape, `{"error": {"code", "message", "details"}}`, so a
client needs a single error handler. The service also serves the demo UI at `/` and `/samples`,
the saved sample answers under `/v1/samples`, and the API reference at `/docs`, whose text and
examples live in `openapi.py`.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from trialviz.answer import Answer, QueryResponse
from trialviz.llm.budget import SpendLedger
from trialviz.openapi import (
    DESCRIPTION,
    INVALID_REQUEST,
    REGISTRY_REJECTED,
    REGISTRY_UNAVAILABLE,
    REQUEST_EXAMPLES,
    SAMPLE_NOT_FOUND,
    SAMPLES_DIRECTORY,
    TAGS,
    Health,
    SampleSummary,
    answer_examples,
    resolution_examples,
)
from trialviz.pipeline import Pipeline
from trialviz.query.interpreters.base import FallbackInterpreter, Interpreter
from trialviz.query.interpreters.claude import ClaudeInterpreter
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import Resolution
from trialviz.query.resolver import QueryResolver
from trialviz.retrieval.client import RegistryQueryError, RegistryUnavailable
from trialviz.retrieval.retriever import Retriever, build_retriever
from trialviz.settings import Settings

UI_DIRECTORY = Path(__file__).parent / "ui"
"""The demo UI and the API reference page, plain HTML, CSS and JavaScript."""

QueryBody = Annotated[QueryRequest, Body(openapi_examples=REQUEST_EXAMPLES)]
"""The request body of both question endpoints, with the examples the reference offers."""

_SAMPLE_NAME = re.compile(r"[0-9a-z_]+")

_REVALIDATE = {"Cache-Control": "no-cache"}


class _RevalidatedStaticFiles(StaticFiles):
    """The UI's files are checked against their ETag on every load, so a browser never runs
    a stylesheet or script older than the page that loads it after a redeploy."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers.update(_REVALIDATE)
        return response


def build_interpreter(settings: Settings) -> Interpreter:
    """Claude first when a key is configured, with the rule-based interpreter behind it."""
    rules = RuleBasedInterpreter()
    if settings.anthropic_api_key is None:
        return rules
    claude = ClaudeInterpreter.from_api_key(
        settings.anthropic_api_key.get_secret_value(),
        SpendLedger(settings.llm_ledger_path, settings.llm_spend_cap_usd),
        model=settings.interpreter_model,
        effort=settings.interpreter_effort,
        max_tokens=settings.interpreter_max_tokens,
    )
    return FallbackInterpreter(claude, rules)


def create_app(
    settings: Settings | None = None,
    interpreter: Interpreter | None = None,
    retriever: Retriever | None = None,
) -> FastAPI:
    settings = settings or Settings()
    resolver = QueryResolver(interpreter or build_interpreter(settings), today=date.today)
    pipeline = Pipeline(resolver, retriever or build_retriever(settings))

    app = FastAPI(
        title="trialviz API",
        summary="Questions about clinical trials in, cited chart specifications out.",
        description=DESCRIPTION,
        version="1.0.0",
        openapi_tags=TAGS,
        servers=[{"url": "/", "description": "The server this reference is served from"}],
        contact={
            "name": "Source and documentation",
            "url": "https://github.com/srivatsav-kannan/trialviz",
        },
        docs_url=None,
        redoc_url=None,
    )

    for page, file in [("/", "index.html"), ("/samples", "index.html"), ("/docs", "api.html")]:
        app.add_api_route(page, _page(file), methods=["GET"], include_in_schema=False)

    app.mount("/ui", _RevalidatedStaticFiles(directory=UI_DIRECTORY), name="ui")

    @app.post(
        "/v1/query",
        response_model=QueryResponse,
        response_model_exclude_none=False,
        tags=["Answers"],
        summary="Answer a question",
        operation_id="answerQuestion",
        responses={
            200: {
                "description": "An answer, or what is needed before the question can be answered.",
                "content": {"application/json": {"examples": answer_examples()}},
            },
            422: INVALID_REQUEST,
            502: REGISTRY_REJECTED,
            503: REGISTRY_UNAVAILABLE,
        },
    )
    async def query(
        request: Annotated[QueryRequest, Body(openapi_examples=REQUEST_EXAMPLES)],
    ) -> Any:
        """Reads the question and any structured fields, fetches the matching trials from
        ClinicalTrials.gov, and returns the recommended chart with every alternative and a
        citation behind every number.

        The response is either an answer (`status: resolved`) or a request for clarification
        (`status: needs_clarification`) when the question cannot be answered as asked, such as
        a reference to "this drug" that nothing names. Most questions take a few seconds, and
        questions that match tens of thousands of trials can take up to a minute.
        """
        return (await pipeline.run(request)).response

    @app.post(
        "/v1/interpret",
        response_model=Resolution,
        tags=["Planning"],
        summary="Read a question without answering it",
        operation_id="interpretQuestion",
        responses={
            200: {
                "description": "The resolved plan, or what is needed to resolve it.",
                "content": {"application/json": {"examples": resolution_examples()}},
            },
            422: INVALID_REQUEST,
        },
    )
    async def interpret(request: QueryBody) -> Any:
        """Takes the same body as `POST /v1/query` and returns the plan it would run, with
        the source of every value and every note, without fetching any data. Use it to check
        how a question will be read before spending time on retrieval.
        """
        return await resolver.resolve(request)

    @app.get(
        "/v1/samples",
        response_model=list[SampleSummary],
        tags=["Samples"],
        summary="List saved answers",
        operation_id="listSamples",
    )
    async def samples() -> list[dict[str, Any]]:
        """Lists the answers saved in the project's `examples/` folder, which were produced by
        real questions against the live registry. The demo UI's samples page reads this."""
        return [_summarize_sample(path) for path in sorted(SAMPLES_DIRECTORY.glob("*.json"))]

    @app.get(
        "/v1/samples/{name}",
        response_model=Answer,
        response_model_exclude_none=False,
        tags=["Samples"],
        summary="Retrieve a saved answer",
        operation_id="getSample",
        responses={404: SAMPLE_NOT_FOUND, 422: INVALID_REQUEST},
    )
    async def sample(name: str) -> Any:
        """Returns a saved answer exactly as `POST /v1/query` produced it."""
        path = SAMPLES_DIRECTORY / f"{name}.json"
        if not _SAMPLE_NAME.fullmatch(name) or not path.is_file():
            return _error(404, "sample_not_found", f"There is no saved sample named {name!r}.")
        return json.loads(path.read_text())["response"]

    @app.get(
        "/health",
        response_model=Health,
        tags=["Service"],
        summary="Check the service is up",
        operation_id="getHealth",
    )
    async def health() -> dict[str, str]:
        """Returns `ok` while the service is running. It does not contact the registry."""
        return {"status": "ok"}

    @app.exception_handler(RegistryQueryError)
    async def registry_rejected(_: Request, error: RegistryQueryError) -> JSONResponse:
        return _error(502, "registry_rejected_query", str(error))

    @app.exception_handler(RegistryUnavailable)
    async def registry_down(_: Request, error: RegistryUnavailable) -> JSONResponse:
        return _error(503, "registry_unavailable", str(error))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, error: RequestValidationError) -> JSONResponse:
        details = [
            {
                "path": _path(issue["loc"]),
                "message": str(issue["msg"]).removeprefix("Value error, "),
            }
            for issue in error.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The request does not match the schema. See details.",
                    "details": details,
                }
            },
        )

    return app


def _page(file: str) -> Any:
    async def serve() -> FileResponse:
        return FileResponse(UI_DIRECTORY / file, headers=_REVALIDATE)

    return serve


def _summarize_sample(path: Path) -> dict[str, Any]:
    saved = json.loads(path.read_text())
    request, response = saved["request"], saved["response"]
    return {
        "name": path.stem,
        "request": request,
        "title": response["visualization"]["title"],
        "chart_type": response["visualization"]["type"],
        "alternatives": [view["type"] for view in response["alternatives"]],
        "cohorts": [
            {"label": cohort["label"], "total": cohort["total"]}
            for cohort in response["meta"]["cohorts"]
        ],
        "data_version": response["meta"]["data_version"],
    }


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, "details": []}}
    )


def _path(location: tuple[Any, ...]) -> str:
    """Render a validation location like ("body", "filters", "phases", 0) as "filters.phases[0]"."""
    parts = [part for part in location if part != "body"]
    rendered = ""
    for part in parts:
        rendered += (
            f"[{part}]" if isinstance(part, int) else (f".{part}" if rendered else str(part))
        )
    return rendered
