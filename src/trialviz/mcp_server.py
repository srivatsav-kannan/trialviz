"""trialviz as tools for AI agents, over the Model Context Protocol at `/mcp`.

An agent such as Claude Desktop or Claude Code connects to `https://<host>/mcp` and can ask
questions, check how a question will be read, reopen saved answers and browse the samples.
The tools call the same code as the HTTP endpoints, so a question asked through MCP counts
against the same per-client limits and spending cap, is saved under the same `answer_id`,
and reports progress to clients that ask for it.

An agent reads every token it is given, so the tools return a compact answer: the chart,
its reason, the rows without their trial ID lists, one citation per row, the notes, and
links to the full answer, the UI and the CSV. `get_answer` with `full` returns the complete
response.

The endpoint is stateless, since every tool call stands alone, and needs no sign-in, since
the API is public and read-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request

from trialviz import __version__, progress
from trialviz.export import labelled_links
from trialviz.query.request import QueryRequest

Answering = Callable[[QueryRequest], Awaitable[Any]]
Failure = Callable[[Exception], tuple[int, str, str, int | None]]

log = logging.getLogger(__name__)

ROWS_SHOWN = 50
"""Rows in a compact answer. The full answer and the CSV have every row."""

INSTRUCTIONS = """\
trialviz answers questions about clinical trials from the ClinicalTrials.gov registry with a
recommended chart, the rows behind it and a citation for every number. Use `ask` with a
plain-English question, adding structured fields (filters, analysis, presentation) only to
pin down what the question leaves open. Use `interpret_question` to see how a question will be read
without fetching anything. Every answer has an answer_id and links: share `ui` with a person,
use `csv` for the rows. Numbers come from registry records, never from a model, so quote
them as given and cite the trial IDs in the citations."""

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=True)


def build_mcp(
    *,
    answer: Answering,
    interpret: Answering,
    saved: Callable[[str], Awaitable[dict[str, Any] | None]],
    asked: Callable[[str], Awaitable[dict[str, Any] | None]],
    quota: Callable[[Request], None],
    failure: Failure,
    samples: Callable[[], list[dict[str, Any]]],
    sample: Callable[[str], dict[str, Any] | None],
) -> tuple[MCPServer, Starlette]:
    """The MCP server, and the Starlette app whose `/mcp` route serves it."""
    server = MCPServer("trialviz", instructions=INSTRUCTIONS, version=__version__)

    async def run(ctx: Context, work: Answering, request: QueryRequest) -> Any:
        http = ctx.request_context.request
        try:
            if isinstance(http, Request):
                quota(http)
            return await work(request)
        except Exception as error:
            status, _, message, _ = failure(error)
            if status == 500:
                log.exception("unexpected failure in an MCP tool", exc_info=error)
            raise ToolError(message) from error

    @server.tool(annotations=_READ_ONLY)
    async def ask(request: QueryRequest, ctx: Context) -> dict[str, Any]:
        """Answer a question about clinical trials with a recommended chart, its rows and
        citations to the registry records behind every number. `request.query` is required;
        every other field is optional and binding when set. Large questions take up to a
        couple of minutes, and progress is reported to clients that ask for it."""
        pending: set[asyncio.Task[None]] = set()

        def report(step: progress.Progress) -> None:
            total = step.records_expected or None
            task = asyncio.create_task(
                ctx.report_progress(step.records_fetched, total, step.message)
            )
            pending.add(task)
            task.add_done_callback(pending.discard)

        with progress.listening(report):
            response = await run(ctx, answer, request)
        return _compact(response, _base(ctx))

    @server.tool(annotations=_READ_ONLY)
    async def interpret_question(request: QueryRequest, ctx: Context) -> dict[str, Any]:
        """Show how a question will be read, as the plan it resolves to with the source of
        every value and any notes, without fetching any data."""
        resolution = await run(ctx, interpret, request)
        result: dict[str, Any] = resolution.model_dump(mode="json")
        return result

    @server.tool(annotations=_READ_ONLY)
    async def get_answer(answer_id: str, ctx: Context, full: bool = False) -> dict[str, Any]:
        """Reopen a saved answer by its answer_id, compact by default or the complete
        response with `full`. Saved answers expire after a while."""
        body = await saved(answer_id)
        if body is None:
            request = await asked(answer_id)
            if request is None:
                raise ToolError(f"There is no saved answer {answer_id!r}.")
            raise ToolError(
                f"The answer {answer_id!r} has expired. It was asked as {json.dumps(request)}; "
                "call ask with that request to answer it with today's registry data."
            )
        return body if full else _compact(body, _base(ctx))

    @server.tool(annotations=_READ_ONLY)
    async def list_samples() -> list[dict[str, Any]]:
        """The saved sample answers: real questions with their chart types and totals. Open
        one with `get_sample`."""
        return samples()

    @server.tool(annotations=_READ_ONLY)
    async def get_sample(name: str, ctx: Context) -> dict[str, Any]:
        """One saved sample answer, compact, by the name `list_samples` gives."""
        saved = sample(name)
        if saved is None:
            raise ToolError(f"There is no sample named {name!r}.")
        return _compact(saved, _base(ctx), sample=name)

    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, host="0.0.0.0"
    )
    return server, app


def _base(ctx: Context) -> str:
    http = ctx.request_context.request
    if not isinstance(http, Request):
        return ""
    scheme = http.headers.get("x-forwarded-proto", http.url.scheme).split(",")[0].strip()
    return f"{scheme}://{http.url.netloc}"


def _compact(body: dict[str, Any], base: str, sample: str | None = None) -> dict[str, Any]:
    """An answer cut to what an agent needs to state it and cite it."""
    if body.get("status") != "resolved":
        return body
    view, meta = body["visualization"], body["meta"]
    data = view["data"]
    rows = labelled_links(data) if isinstance(data, dict) else data
    answer_id = meta.get("answer_id")
    if sample:
        links = {"ui": f"{base}/samples?open={sample}", "csv": f"{base}/v1/samples/{sample}/csv"}
    elif answer_id:
        links = {
            "ui": f"{base}/?answer={answer_id}",
            "csv": f"{base}/v1/answers/{answer_id}/csv",
            "full": f"{base}/v1/answers/{answer_id}",
        }
    else:
        links = {}
    return {
        "status": "resolved",
        "answer_id": answer_id,
        "title": view["title"],
        "subtitle": view.get("subtitle"),
        "chart": view["type"],
        "reason": view["reason"],
        "alternatives": [alt["type"] for alt in body["alternatives"]],
        "cohorts": [{"label": c["label"], "total": c["total"]} for c in meta["cohorts"]],
        "rows": [
            {k: v for k, v in row.items() if k not in ("nct_ids", "citations", "outliers")}
            | {"citation": (row.get("citations") or [None])[0]}
            for row in rows[:ROWS_SHOWN]
        ],
        "rows_total": len(rows),
        "notes": [note["message"] for note in meta["notes"]],
        "data_version": meta["data_version"],
        "links": links,
    }
