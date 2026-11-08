"""HTTP interface.

`POST /v1/query` answers a question with a recommended visualization, alternative views
of the same data, and citations to the registry records behind every number.

`POST /v1/query/stream` answers the same way as server-sent events, with progress while
the answer is computed. Every answer is saved under `meta.answer_id`, which
`GET /v1/answers/{answer_id}` reopens and `/csv` downloads, and a repeated question is served
from it at no cost.

`/mcp` offers the same answers to AI agents as tools over the Model Context Protocol.

`POST /v1/interpret` resolves a request into a plan without fetching any data, so a
caller can see exactly how a question was read (and which values came from where) before
running it.

Every error response has one shape, `{"error": {"code", "message", "details"}}`, so a
client needs a single error handler. The service also serves the demo UI at `/` and `/samples`,
the saved sample answers under `/v1/samples`, and the API reference at `/docs`, whose text and
examples live in `openapi.py`.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from trialviz.answer import Answer, QueryResponse
from trialviz.export import Part, to_csv
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
from trialviz.mcp_server import build_mcp
from trialviz.openapi import (
    ANSWER_NOT_FOUND,
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
from trialviz.progress import Progress, listening
from trialviz.query.interpreters.base import FallbackInterpreter, Interpreter
from trialviz.query.interpreters.claude import ClaudeInterpreter
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import ClarificationNeeded, Resolution
from trialviz.query.resolver import QueryResolver
from trialviz.retrieval.client import RegistryQueryError, RegistryUnavailable
from trialviz.retrieval.retriever import Retriever, build_retriever
from trialviz.settings import Settings
from trialviz.store import AnswerStore, FileStore, NoStore, PostgresStore, key_for
from trialviz.visualization.contract import component_schemas

UI_DIRECTORY = Path(__file__).parent / "ui"
"""The demo UI and the API reference page, plain HTML, CSS and JavaScript."""

QueryBody = Annotated[QueryRequest, Body(openapi_examples=REQUEST_EXAMPLES)]
"""The request body of both question endpoints, with the examples the reference offers."""

_SAMPLE_NAME = re.compile(r"[0-9a-z_]+")
_ANSWER_ID = re.compile(r"[0-9a-f]{20}")

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


def build_store(settings: Settings) -> AnswerStore:
    """Saved answers in Postgres when a database is configured, else in local files, or
    nowhere when the folder is unset."""
    if settings.database_url is not None:
        return PostgresStore(settings.database_url.get_secret_value(), settings.answers_kept)
    if settings.answer_store_dir is not None:
        return FileStore(settings.answer_store_dir, settings.answers_kept)
    return NoStore()


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
    store: AnswerStore | None = None,
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
    store = store or build_store(settings)

    async def answer(request: QueryRequest) -> Answer | ClarificationNeeded | dict[str, Any]:
        """A saved answer to the same request against the same data if there is one, which
        costs nothing; otherwise a new answer, saved under its id."""
        key = key_for(request, await retriever.data_version())
        if (saved := await store.get(key)) is not None:
            return saved
        async with slots.take():
            result = await pipeline.run(request)
        if result.answer is not None:
            result.answer.meta.answer_id = key
            await store.put(
                key,
                result.answer.model_dump(mode="json"),
                request.model_dump(mode="json", exclude_none=True),
            )
        return result.response

    async def read(request: QueryRequest) -> Resolution:
        async with slots.take():
            return await resolver.resolve(request)

    async def load(key: str) -> dict[str, Any] | None:
        return await store.get(key) if _ANSWER_ID.fullmatch(key) else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with agents.session_manager.run():
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

    generate_openapi = app.openapi

    def openapi_with_row_models() -> dict[str, Any]:
        # The chart row models are referenced by the `data` fields but used by no route,
        # so FastAPI does not collect them. They are added here, once.
        if app.openapi_schema is None:
            schema = generate_openapi()
            schema["components"]["schemas"].update(component_schemas())
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi_with_row_models  # type: ignore[method-assign]

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
        allow_headers=["content-type", "mcp-protocol-version"],
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
        return await answer(request)

    @app.post(
        "/v1/query/stream",
        tags=["Answers"],
        summary="Answer a question, reporting progress",
        operation_id="answerQuestionWithProgress",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Server-sent events: `progress` events (a Progress object) while "
                "the answer is computed, then one `answer` event carrying exactly what "
                "`POST /v1/query` returns, or one `error` event carrying the error envelope "
                "with its `status`. A comment line is sent every 15 seconds while nothing "
                "else is, so proxies keep the connection open.",
                "headers": RATE_LIMIT_HEADERS,
                "content": {"text/event-stream": {"schema": Progress.model_json_schema()}},
            },
            413: REQUEST_TOO_LARGE,
            422: INVALID_REQUEST,
            429: RATE_LIMITED,
            503: SERVICE_BUSY,
        },
        dependencies=[Depends(question_quota)],
    )
    async def query_stream(request: QueryBody) -> StreamingResponse:
        """Takes the same body as `POST /v1/query` and streams the answer as it is computed,
        so a frontend can show which stage it is in and how many trial records have arrived
        instead of a bare spinner. Limits, errors and saved answers work exactly as for
        `POST /v1/query`, except that an error after the stream has started arrives as an
        `error` event, since the status line has already been sent.
        """
        if slots.free == 0:
            raise ServiceBusy(
                f"The service is already answering {slots.size} questions. Try again shortly.",
                retry_after=10,
            )
        return StreamingResponse(
            _events(answer, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/v1/answers/{answer_id}",
        response_model=Answer,
        response_model_exclude_none=False,
        tags=["Answers"],
        summary="Reopen a saved answer",
        operation_id="getAnswer",
        responses={404: ANSWER_NOT_FOUND, 422: INVALID_REQUEST},
    )
    async def saved_answer(answer_id: str) -> Any:
        """Returns an answer by the `meta.answer_id` it was given, without recomputing it, so
        an answer can be shared as a link. The newest answers are kept, so an old one can
        expire."""
        return await _saved(answer_id)

    @app.get(
        "/v1/answers/{answer_id}/csv",
        tags=["Answers"],
        summary="Download a saved answer's rows as CSV",
        operation_id="getAnswerCsv",
        response_class=PlainTextResponse,
        responses={
            200: {"content": {"text/csv": {}}, "description": "The rows, one per line."},
            404: ANSWER_NOT_FOUND,
        },
    )
    async def saved_answer_csv(answer_id: str, part: Part = "links") -> Response:
        """The recommended chart's rows as CSV, without citations, with lists such as
        `nct_ids` joined by semicolons. For a network or Sankey diagram, `part` picks its
        links (the default) or its nodes."""
        saved = await _saved(answer_id)
        if isinstance(saved, JSONResponse):
            return saved
        return _csv(saved, part, answer_id)

    @app.get(
        "/v1/answers/{answer_id}/request",
        tags=["Answers"],
        summary="See what a saved answer was asked",
        operation_id="getAnswerRequest",
        responses={404: ANSWER_NOT_FOUND},
    )
    async def saved_request(answer_id: str) -> Any:
        """The request an answer was given for, kept long after the answer itself expires, so
        a link to an expired answer can say what was asked and offer to ask it again.
        Sending it to `POST /v1/query` answers it with today's registry data, as a new answer
        with a new id. `answer_available` says whether the original answer can still be
        reopened."""
        request = await store.request(answer_id) if _ANSWER_ID.fullmatch(answer_id) else None
        if request is None:
            return _error(404, "answer_not_found", f"There is no saved answer {answer_id!r}.")
        return {"request": request, "answer_available": await load(answer_id) is not None}

    async def _saved(key: str) -> dict[str, Any] | JSONResponse:
        saved = await load(key)
        if saved is None:
            return _error(404, "answer_not_found", f"There is no saved answer {key!r}.")
        return saved

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
        return await read(request)

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
        "/v1/samples/{name}/csv",
        tags=["Samples"],
        summary="Download a saved sample's rows as CSV",
        operation_id="getSampleCsv",
        response_class=PlainTextResponse,
        responses={
            200: {"content": {"text/csv": {}}, "description": "The rows, one per line."},
            404: SAMPLE_NOT_FOUND,
        },
    )
    async def sample_csv(name: str, part: Part = "links") -> Response:
        """A saved sample's rows as CSV, exactly as for a saved answer."""
        path = SAMPLES_DIRECTORY / f"{name}.json"
        if not _SAMPLE_NAME.fullmatch(name) or not path.is_file():
            return _error(404, "sample_not_found", f"There is no saved sample named {name!r}.")
        return _csv(json.loads(path.read_text())["response"], part, name)

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

    async def failed(_: Request, error: Exception) -> JSONResponse:
        status, code, message, retry_after = _failure(error)
        response = _error(status, code, message)
        if retry_after is not None:
            response.headers["Retry-After"] = str(retry_after)
        return response

    for failure in (
        RateLimited,
        ServiceBusy,
        AnswerTimeout,
        RegistryQueryError,
        RegistryUnavailable,
        Exception,
    ):
        app.add_exception_handler(failure, failed)

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

    # AI agents reach the same answers over MCP at /mcp, under the same limits.
    agents, mcp_app = build_mcp(
        answer=answer,
        interpret=read,
        saved=load,
        asked=store.request,
        quota=question_quota,
        failure=_failure,
        samples=SAMPLES_DIRECTORY,
    )
    app.router.routes.extend(mcp_app.routes)

    return app


def _failure(error: Exception) -> tuple[int, str, str, int | None]:
    """The status, code, message and Retry-After for a failure while answering, shared by
    the JSON endpoints, the stream's `error` event and the MCP tools."""
    match error:
        case RateLimited():
            return 429, "rate_limited", str(error), error.retry_after
        case ServiceBusy():
            return 503, "busy", str(error), error.retry_after
        case AnswerTimeout():
            return 504, "answer_timeout", str(error), None
        case RegistryQueryError():
            return 502, "registry_rejected_query", str(error), None
        case RegistryUnavailable():
            return 503, "registry_unavailable", str(error), None
    return 500, "internal_error", f"The service failed unexpectedly ({type(error).__name__}).", None


KEEP_ALIVE_SECONDS = 15.0


async def _events(answer: Any, request: QueryRequest) -> AsyncIterator[str]:
    """The stream for one question: progress as it happens, then the answer or the error.
    The work runs in its own task, so a client that disconnects cancels it."""
    queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

    async def work() -> None:
        with listening(lambda step: queue.put_nowait(("progress", step.model_dump_json()))):
            try:
                response = await answer(request)
                body = response if isinstance(response, dict) else response.model_dump(mode="json")
                queue.put_nowait(("answer", json.dumps(body)))
            except Exception as error:
                status, code, message, retry_after = _failure(error)
                failure = {"code": code, "message": message, "details": []}
                payload: dict[str, Any] = {"status": status, "error": failure}
                if retry_after is not None:
                    payload["retry_after"] = retry_after
                queue.put_nowait(("error", json.dumps(payload)))
            finally:
                queue.put_nowait(None)

    task = asyncio.create_task(work())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), KEEP_ALIVE_SECONDS)
            except TimeoutError:
                yield ": still working\n\n"
                continue
            if event is None:
                return
            kind, data = event
            yield f"event: {kind}\ndata: {data}\n\n"
    finally:
        task.cancel()


def _csv(answer: dict[str, Any], part: Part, name: str) -> Response:
    return Response(
        to_csv(answer, part),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


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
