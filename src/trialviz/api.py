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
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from trialviz.answer import Answer, QueryResponse
from trialviz.export import Part, to_csv
from trialviz.limits import BodySizeLimit, RateLimited, SecurityHeaders, ServiceBusy
from trialviz.llm.budget import Ledger
from trialviz.mcp_server import build_mcp
from trialviz.openapi import (
    ANSWER_NOT_FOUND,
    ANSWER_TIMEOUT,
    DESCRIPTION,
    INTERNAL_ERROR,
    INVALID_PARAMETER,
    INVALID_REQUEST,
    RATE_LIMIT_HEADERS,
    RATE_LIMITED,
    REGISTRY_REJECTED,
    REGISTRY_UNAVAILABLE_OR_BUSY,
    REQUEST_EXAMPLES,
    REQUEST_TOO_LARGE,
    SAMPLE_NOT_FOUND,
    SERVICE_BUSY,
    TAGS,
    Health,
    SampleSummary,
    SavedRequest,
    ServiceStatus,
    answer_examples,
    json_error,
    resolution_examples,
)
from trialviz.pipeline import AnswerTimeout
from trialviz.progress import Progress, listening
from trialviz.query.interpreters.base import Interpreter
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import Resolution
from trialviz.retrieval.client import RegistryQueryError, RegistryUnavailable
from trialviz.retrieval.retriever import Retriever
from trialviz.service import Service, load_sample, sample_summaries
from trialviz.settings import Settings
from trialviz.store import AnswerStore
from trialviz.visualization.contract import component_schemas

UI_DIRECTORY = Path(__file__).parent / "ui"
"""The demo UI and the API reference page, plain HTML, CSS and JavaScript."""

QueryBody = Annotated[QueryRequest, Body(openapi_examples=REQUEST_EXAMPLES)]
"""The request body of the question endpoints, with the examples the reference offers."""

log = logging.getLogger(__name__)

_REVALIDATE = {"Cache-Control": "no-cache"}


class _RevalidatedStaticFiles(StaticFiles):
    """The UI's files are checked against their ETag on every load, so a browser never runs
    a stylesheet or script older than the page that loads it after a redeploy."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers.update(_REVALIDATE)
        return response


def create_app(
    settings: Settings | None = None,
    interpreter: Interpreter | None = None,
    retriever: Retriever | None = None,
    ledger: Ledger | None = None,
    store: AnswerStore | None = None,
) -> FastAPI:
    settings = settings or Settings()
    _quiet_client_logs()
    service = Service(settings, interpreter, retriever, ledger, store)

    # AI agents reach the same answers over MCP at /mcp, under the same limits.
    agents, mcp_app = build_mcp(
        answer=service.answer,
        interpret=service.read,
        saved=service.saved,
        asked=service.asked,
        quota=service.question_quota,
        failure=_failure,
        samples=sample_summaries,
        sample=load_sample,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with agents.session_manager.run():
            yield
        await service.aclose()

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
    app.state.service = service
    _publish_row_models(app)

    for page, file in [("/", "index.html"), ("/samples", "index.html"), ("/docs", "api.html")]:
        app.add_api_route(page, _page(file), methods=["GET"], include_in_schema=False)
    app.mount("/ui", _RevalidatedStaticFiles(directory=UI_DIRECTORY), name="ui")

    @app.middleware("http")
    async def request_quota(request: Request, call_next: Any) -> Any:
        # Every request counts against a general per-client limit, except the health check,
        # which the host polls.
        if request.url.path != "/health":
            try:
                service.requests.hit(service.client(request))
            except RateLimited as error:
                return _respond_to_failure(error)
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
        expose_headers=[
            "Retry-After",
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "RateLimit-Reset",
            "Content-Disposition",
        ],
        max_age=600,
    )

    app.include_router(router)
    for failure in (
        RateLimited,
        ServiceBusy,
        AnswerTimeout,
        RegistryQueryError,
        RegistryUnavailable,
        Exception,
    ):
        app.add_exception_handler(failure, _failed)
    app.add_exception_handler(StarletteHTTPException, _http_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _invalid_request)  # type: ignore[arg-type]
    app.router.routes.extend(mcp_app.routes)
    return app


def _publish_row_models(app: FastAPI) -> None:
    """The chart row models are referenced by the `data` fields but used by no route, so
    FastAPI does not collect them. They are added to the document once, when it is built."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = generate()
            schema["components"]["schemas"].update(component_schemas())
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def _service(request: Request) -> Service:
    service: Service = request.app.state.service
    return service


ServiceDep = Annotated[Service, Depends(_service)]


async def _question_quota(request: Request, service: ServiceDep) -> None:
    await service.question_quota(request)


router = APIRouter()


@router.post(
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
    dependencies=[Depends(_question_quota)],
)
async def query(request: QueryBody, service: ServiceDep) -> Any:
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
    return await service.answer(request)


@router.post(
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
    dependencies=[Depends(_question_quota)],
)
async def query_stream(request: QueryBody, service: ServiceDep) -> StreamingResponse:
    """Takes the same body as `POST /v1/query` and streams the answer as it is computed,
    so a frontend can show which stage it is in and how many trial records have arrived
    instead of a bare spinner. Limits, errors and saved answers work exactly as for
    `POST /v1/query`, except that an error after the stream has started arrives as an
    `error` event, since the status line has already been sent.
    """
    if service.slots.free == 0:
        raise ServiceBusy(
            f"The service is already answering {service.slots.size} questions. Try again shortly.",
            retry_after=10,
        )
    return StreamingResponse(
        _events(service.answer, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/v1/answers/{answer_id}",
    response_model=Answer,
    response_model_exclude_none=False,
    tags=["Answers"],
    summary="Reopen a saved answer",
    operation_id="getAnswer",
    responses={404: ANSWER_NOT_FOUND, 422: INVALID_PARAMETER},
)
async def saved_answer(answer_id: str, service: ServiceDep) -> Any:
    """Returns an answer by the `meta.answer_id` it was given, without recomputing it, so
    an answer can be shared as a link. The newest answers are kept, so an old one can
    expire."""
    saved = await service.saved(answer_id)
    return _no_answer(answer_id) if saved is None else saved


@router.get(
    "/v1/answers/{answer_id}/csv",
    tags=["Answers"],
    summary="Download a saved answer's rows as CSV",
    operation_id="getAnswerCsv",
    response_class=Response,
    responses={
        200: {"content": {"text/csv": {}}, "description": "The rows, one per line."},
        404: json_error(ANSWER_NOT_FOUND),
        422: json_error(INVALID_PARAMETER),
    },
)
async def saved_answer_csv(answer_id: str, service: ServiceDep, part: Part = "links") -> Response:
    """The recommended chart's rows as CSV, without citations, with lists such as
    `nct_ids` joined by semicolons. For a network or Sankey diagram, `part` picks its
    links (the default) or its nodes."""
    saved = await service.saved(answer_id)
    return _no_answer(answer_id) if saved is None else _csv(saved, part, answer_id)


@router.get(
    "/v1/answers/{answer_id}/request",
    tags=["Answers"],
    summary="See what a saved answer was asked",
    operation_id="getAnswerRequest",
    response_model=SavedRequest,
    responses={404: ANSWER_NOT_FOUND, 422: INVALID_PARAMETER},
)
async def saved_request(answer_id: str, service: ServiceDep) -> Any:
    """The request an answer was given for, kept long after the answer itself expires, so
    a link to an expired answer can say what was asked and offer to ask it again.
    Sending it to `POST /v1/query` answers it with today's registry data, as a new answer
    with a new id. `answer_available` says whether the original answer can still be
    reopened."""
    request = await service.asked(answer_id)
    if request is None:
        return _no_answer(answer_id)
    return {"request": request, "answer_available": await service.saved(answer_id) is not None}


@router.post(
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
    dependencies=[Depends(_question_quota)],
)
async def interpret(request: QueryBody, service: ServiceDep) -> Any:
    """Takes the same body as `POST /v1/query` and returns the plan it would run, with
    the source of every value and every note, without fetching any data. Use it to check
    how a question will be read before spending time on retrieval. It counts against the
    same question limits as `POST /v1/query`, since it can call the language model.
    """
    return await service.read(request)


@router.get(
    "/v1/samples",
    response_model=list[SampleSummary],
    tags=["Samples"],
    summary="List saved answers",
    operation_id="listSamples",
)
async def samples() -> list[dict[str, Any]]:
    """Lists the answers saved in the project's `examples/` folder, which were produced by
    real questions against the live registry. The demo UI's samples page reads this."""
    return sample_summaries()


@router.get(
    "/v1/samples/{name}",
    response_model=Answer,
    response_model_exclude_none=False,
    tags=["Samples"],
    summary="Retrieve a saved answer",
    operation_id="getSample",
    responses={404: SAMPLE_NOT_FOUND, 422: INVALID_PARAMETER},
)
async def sample(name: str) -> Any:
    """Returns a saved answer exactly as `POST /v1/query` produced it."""
    saved = load_sample(name)
    return _no_sample(name) if saved is None else saved


@router.get(
    "/v1/samples/{name}/csv",
    tags=["Samples"],
    summary="Download a saved sample's rows as CSV",
    operation_id="getSampleCsv",
    response_class=Response,
    responses={
        200: {"content": {"text/csv": {}}, "description": "The rows, one per line."},
        404: json_error(SAMPLE_NOT_FOUND),
        422: json_error(INVALID_PARAMETER),
    },
)
async def sample_csv(name: str, part: Part = "links") -> Response:
    """A saved sample's rows as CSV, exactly as for a saved answer."""
    saved = load_sample(name)
    return _no_sample(name) if saved is None else _csv(saved, part, name)


@router.get(
    "/health",
    response_model=Health,
    tags=["Service"],
    summary="Check the service is up",
    operation_id="getHealth",
)
async def health() -> dict[str, str]:
    """Returns `ok` while the service is running. It does not contact the registry."""
    return {"status": "ok"}


@router.get(
    "/v1/status",
    response_model=ServiceStatus,
    tags=["Service"],
    summary="See the service's limits and your standing",
    operation_id="getStatus",
)
async def status(request: Request, service: ServiceDep) -> ServiceStatus:
    """Reports which interpreter reads questions, how much of the language-model budget is
    left, and how many questions you may still ask this minute and this hour. It does not
    count against the question limits."""
    return await service.status(request)


async def _failed(_: Request, error: Exception) -> JSONResponse:
    status, _code, _message, _retry_after = _failure(error)
    if status == 500:
        log.exception("unexpected failure", exc_info=error)
    return _respond_to_failure(error)


def _respond_to_failure(error: Exception) -> JSONResponse:
    status, code, message, retry_after = _failure(error)
    response = _error(status, code, message)
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    return response


async def _http_error(_: Request, error: StarletteHTTPException) -> JSONResponse:
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


async def _invalid_request(_: Request, error: RequestValidationError) -> JSONResponse:
    details = [
        {"path": _path(issue["loc"]), "message": str(issue["msg"]).removeprefix("Value error, ")}
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


def _quiet_client_logs() -> None:
    """Every HTTP request would otherwise be logged at INFO, burying the errors that matter."""
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)


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
                queue.put_nowait(("answer", json.dumps(body, separators=(",", ":"))))
            except Exception as error:
                status, code, message, retry_after = _failure(error)
                if status == 500:
                    log.exception("unexpected failure in a stream", exc_info=error)
                failure = {"code": code, "message": message, "details": []}
                payload: dict[str, Any] = {"status": status, "error": failure}
                if retry_after is not None:
                    payload["retry_after"] = retry_after
                queue.put_nowait(("error", json.dumps(payload, separators=(",", ":"))))
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


def _no_answer(answer_id: str) -> JSONResponse:
    return _error(404, "answer_not_found", f"There is no saved answer {answer_id!r}.")


def _no_sample(name: str) -> JSONResponse:
    return _error(404, "sample_not_found", f"There is no saved sample named {name!r}.")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, "details": []}}
    )


def _path(location: tuple[Any, ...]) -> str:
    """Render a validation location like ("body", "filters", "phases", 0) as "filters.phases[0]",
    and a query or path parameter such as ("query", "part") as "part"."""
    located = bool(location) and location[0] in ("body", "query", "path")
    parts = list(location[1:] if located else location)
    rendered = ""
    for part in parts:
        rendered += (
            f"[{part}]" if isinstance(part, int) else (f".{part}" if rendered else str(part))
        )
    return rendered
