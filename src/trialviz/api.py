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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from trialviz.answer import Answer, QueryResponse
from trialviz.limits import (
    AnswerSlots,
    BodySizeLimit,
    RateLimited,
    SecurityHeaders,
    ServiceBusy,
    SlidingWindowLimiter,
    Window,
    client_address,
)
from trialviz.llm.budget import BudgetExceeded, FileLedger, Ledger, PostgresLedger
from trialviz.openapi import (
    ANSWER_TIMEOUT,
    DESCRIPTION,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    RATE_LIMIT_HEADERS,
    RATE_LIMITED,
    REGISTRY_REJECTED,
    REGISTRY_UNAVAILABLE_OR_BUSY,
    REQUEST_EXAMPLES,
    REQUEST_TOO_LARGE,
    SAMPLE_NOT_FOUND,
    SAMPLES_DIRECTORY,
    SERVICE_BUSY,
    TAGS,
    BudgetStatus,
    Health,
    LimitStatus,
    SampleSummary,
    ServiceStatus,
    answer_examples,
    resolution_examples,
)
from trialviz.pipeline import AnswerTimeout, Pipeline
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


def build_ledger(settings: Settings) -> Ledger:
    """Postgres when a database is configured, as on a deployed service whose disk is wiped
    on every restart, and a local JSON file otherwise."""
    if settings.database_url is not None:
        return PostgresLedger(
            settings.database_url.get_secret_value(),
            settings.llm_spend_cap_usd,
            settings.llm_daily_cap_usd,
        )
    return FileLedger(
        settings.llm_ledger_path, settings.llm_spend_cap_usd, settings.llm_daily_cap_usd
    )


def build_interpreter(settings: Settings, ledger: Ledger | None = None) -> Interpreter:
    """Claude first when a key is configured, with the rule-based interpreter behind it."""
    rules = RuleBasedInterpreter()
    if settings.anthropic_api_key is None:
        return rules
    claude = ClaudeInterpreter.from_api_key(
        settings.anthropic_api_key.get_secret_value(),
        ledger or build_ledger(settings),
        model=settings.interpreter_model,
        effort=settings.interpreter_effort,
        max_tokens=settings.interpreter_max_tokens,
    )
    return FallbackInterpreter(claude, rules)


def create_app(
    settings: Settings | None = None,
    interpreter: Interpreter | None = None,
    retriever: Retriever | None = None,
    ledger: Ledger | None = None,
) -> FastAPI:
    settings = settings or Settings()
    if interpreter is None and ledger is None and settings.anthropic_api_key is not None:
        ledger = build_ledger(settings)
    interpreter = interpreter or build_interpreter(settings, ledger)
    resolver = QueryResolver(interpreter, today=date.today)
    questions = SlidingWindowLimiter(
        [
            Window(settings.questions_per_minute, 60, "per minute"),
            Window(settings.questions_per_hour, 3600, "per hour"),
        ],
        what="questions",
    )
    all_questions = SlidingWindowLimiter(
        [Window(settings.questions_per_hour_total, 3600, "per hour across all clients")],
        what="questions",
    )
    requests = SlidingWindowLimiter(
        [Window(settings.requests_per_minute, 60, "per minute")], what="requests"
    )
    slots = AnswerSlots(settings.max_concurrent_answers)

    def client(request: Request) -> str:
        return client_address(request, settings.trusted_proxy_hops)

    async def question_quota(request: Request) -> None:
        """Counts a question against its client's limits and the overall ceiling, before
        any work is done. The overall ceiling is checked first, so a client's own quota is
        not spent on a question that is refused anyway."""
        if all_questions.peek("all").remaining == 0:
            all_questions.hit("all")  # raises with the time until a slot frees up
        request.state.quota = questions.hit(client(request))
        all_questions.hit("all")

    owned = retriever is None
    retriever = retriever or build_retriever(settings)
    pipeline = Pipeline(resolver, retriever, deadline_seconds=settings.answer_deadline_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owned:
            await retriever.aclose()

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
        lifespan=lifespan,
    )

    for page, file in [("/", "index.html"), ("/samples", "index.html"), ("/docs", "api.html")]:
        app.add_api_route(page, _page(file), methods=["GET"], include_in_schema=False)

    app.mount("/ui", _RevalidatedStaticFiles(directory=UI_DIRECTORY), name="ui")

    @app.middleware("http")
    async def request_quota(request: Request, call_next: Any) -> Any:
        # Every request counts against a general per-client limit, except the health check,
        # which the host polls.
        if request.url.path != "/health":
            try:
                requests.hit(client(request))
            except RateLimited as error:
                return _rate_limited(error)
        return await call_next(request)

    # Added last, so they run first: the size limit sees the body before anything reads
    # it, and every response, refusals included, carries the security headers. CORS runs
    # first of all, so a page on another origin can read refusals and their Retry-After
    # too, and a preflight is answered without counting against any limit.
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)
    app.add_middleware(SecurityHeaders)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
        expose_headers=["Retry-After", "RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset"],
        max_age=600,
    )

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
                "headers": RATE_LIMIT_HEADERS,
                "content": {"application/json": {"examples": answer_examples()}},
            },
            413: REQUEST_TOO_LARGE,
            422: INVALID_REQUEST,
            429: RATE_LIMITED,
            500: INTERNAL_ERROR,
            502: REGISTRY_REJECTED,
            503: REGISTRY_UNAVAILABLE_OR_BUSY,
            504: ANSWER_TIMEOUT,
        },
        dependencies=[Depends(question_quota)],
    )
    async def query(request: QueryBody) -> Any:
        """Reads the question and any structured fields, fetches the matching trials from
        ClinicalTrials.gov, and returns the recommended chart with every alternative and a
        citation behind every number.

        The response is either an answer (`status: resolved`) or a request for clarification
        (`status: needs_clarification`) when the question cannot be answered as asked, such as
        a reference to "this drug" that nothing names. Most questions take a few seconds, and
        questions that match tens of thousands of trials can take a minute or two, and an
        answer that would need more than the configured deadline returns a 504.

        Questions are rate limited per client and overall (see `GET /v1/status` and the
        RateLimit headers on every answer), and only a few are answered at once.
        """
        async with slots.take():
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
                "headers": RATE_LIMIT_HEADERS,
                "content": {"application/json": {"examples": resolution_examples()}},
            },
            413: REQUEST_TOO_LARGE,
            422: INVALID_REQUEST,
            429: RATE_LIMITED,
            500: INTERNAL_ERROR,
            503: SERVICE_BUSY,
        },
        dependencies=[Depends(question_quota)],
    )
    async def interpret(request: QueryBody) -> Any:
        """Takes the same body as `POST /v1/query` and returns the plan it would run, with
        the source of every value and every note, without fetching any data. Use it to check
        how a question will be read before spending time on retrieval. It counts against the
        same question limits as `POST /v1/query`, since it can call the language model.
        """
        async with slots.take():
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

    @app.get(
        "/v1/status",
        response_model=ServiceStatus,
        tags=["Service"],
        summary="See the service's limits and your standing",
        operation_id="getStatus",
    )
    async def status(request: Request) -> ServiceStatus:
        """Reports which interpreter reads questions, how much of the language-model budget is
        left, and how many questions you may still ask this minute and this hour. It does not
        count against the question limits."""
        uses_claude = "claude" in interpreter.name
        budget, reason = None, None
        if not uses_claude:
            reason = "No language-model key is configured, so questions are read by rules."
        elif ledger is not None:
            try:
                spend = await ledger.status()
                budget = BudgetStatus(
                    cap_usd=spend.cap_usd,
                    daily_cap_usd=spend.daily_cap_usd,
                    spent_usd=round(spend.spent_usd, 2),
                    spent_today_usd=round(spend.spent_today_usd, 2),
                    remaining_usd=round(spend.remaining_usd, 2),
                )
                if spend.remaining_usd <= 0:
                    reason = "The language-model budget is used up, so questions are read by rules."
            except BudgetExceeded as error:
                reason = f"{str(error).capitalize()}, so questions are read by rules."
        standing = questions.peek(client(request))
        return ServiceStatus(
            interpreter="claude" if uses_claude else "rules",
            claude_available=uses_claude and reason is None,
            reason=reason,
            budget=budget,
            limits=LimitStatus(
                questions_per_minute=settings.questions_per_minute,
                questions_per_hour=settings.questions_per_hour,
                questions_remaining=standing.remaining,
                resets_in_seconds=standing.reset_seconds,
                answers_in_progress=slots.size - slots.free,
                max_answers_in_progress=slots.size,
                max_body_bytes=settings.max_body_bytes,
            ),
            client=client(request),
        )

    @app.exception_handler(RateLimited)
    async def rate_limited(_: Request, error: RateLimited) -> JSONResponse:
        return _rate_limited(error)

    @app.exception_handler(ServiceBusy)
    async def busy(_: Request, error: ServiceBusy) -> JSONResponse:
        response = _error(503, "busy", str(error))
        response.headers["Retry-After"] = str(error.retry_after)
        return response

    @app.exception_handler(AnswerTimeout)
    async def too_slow(_: Request, error: AnswerTimeout) -> JSONResponse:
        return _error(504, "answer_timeout", str(error))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, error: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        message = {
            404: "There is nothing at this path.",
            405: "This path does not accept that method.",
        }
        return _error(
            error.status_code,
            codes.get(error.status_code, "http_error"),
            message.get(error.status_code, str(error.detail)),
        )

    @app.exception_handler(Exception)
    async def unexpected(_: Request, error: Exception) -> JSONResponse:
        return _error(
            500, "internal_error", f"The service failed unexpectedly ({type(error).__name__})."
        )

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


def _rate_limited(error: RateLimited) -> JSONResponse:
    response = _error(429, "rate_limited", str(error))
    response.headers["Retry-After"] = str(error.retry_after)
    return response


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
