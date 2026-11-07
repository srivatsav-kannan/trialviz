"""HTTP interface.

`POST /v1/interpret` resolves a request into a plan without fetching any data, so a
caller can see exactly how a question was read (and which values came from where) before
running it. The full query endpoint builds on the same resolver.

Every error response has one shape, `{"error": {"code", "message", "details"}}`, so a
client needs a single error handler.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from trialviz.llm.budget import SpendLedger
from trialviz.query.interpreters.base import FallbackInterpreter, Interpreter
from trialviz.query.interpreters.claude import ClaudeInterpreter
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import Resolution
from trialviz.query.resolver import QueryResolver
from trialviz.settings import Settings


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


def create_app(settings: Settings | None = None, interpreter: Interpreter | None = None) -> FastAPI:
    settings = settings or Settings()
    resolver = QueryResolver(interpreter or build_interpreter(settings), today=date.today)

    app = FastAPI(
        title="trialviz",
        summary="Questions about clinical trials in, visualization specs out.",
        version="0.1.0",
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/interpret", response_model=Resolution)
    async def interpret(request: QueryRequest) -> Any:
        """Read the question and return the resolved plan, or what is needed to resolve it."""
        return await resolver.resolve(request)

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


def _path(location: tuple[Any, ...]) -> str:
    """Render a validation location like ("body", "filters", "phases", 0) as "filters.phases[0]"."""
    parts = [part for part in location if part != "body"]
    rendered = ""
    for part in parts:
        rendered += (
            f"[{part}]" if isinstance(part, int) else (f".{part}" if rendered else str(part))
        )
    return rendered
