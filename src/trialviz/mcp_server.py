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
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request

from trialviz import progress
from trialviz.query.request import QueryRequest

Answering = Callable[[QueryRequest], Awaitable[Any]]
Failure = Callable[[Exception], tuple[int, str, str, int | None]]

ROWS_SHOWN = 50
"""Rows in a compact answer. The full answer and the CSV have every row."""

INSTRUCTIONS = """\
trialviz answers questions about clinical trials from the ClinicalTrials.gov registry with a
recommended chart, the rows behind it and a citation for every number. Use `ask` with a
plain-English question, adding structured fields (filters, analysis, presentation) only to
pin down what the question leaves open. Use `interpret` to see how a question will be read
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
    quota: Callable[[Request], Awaitable[None]],
    failure: Failure,
    samples: Path,
) -> tuple[MCPServer, Starlette]:
    """The MCP server, and the Starlette app whose `/mcp` route serves it."""
    server = MCPServer("trialviz", instructions=INSTRUCTIONS, version="1.0.0")

    async def run(ctx: Context, work: Answering, request: QueryRequest) -> Any:
        http = ctx.request_context.request
        try:
            if isinstance(http, Request):
                await quota(http)
            return await work(request)
        except Exception as error:
            raise ToolError(failure(error)[2]) from error

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
        body = response if isinstance(response, dict) else response.model_dump(mode="json")
        return _compact(body, _base(ctx))

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
        return [_sample_summary(path) for path in sorted(samples.glob("*.json"))]

    @server.tool(annotations=_READ_ONLY)
    async def get_sample(name: str, ctx: Context) -> dict[str, Any]:
        """One saved sample answer, compact, by the name `list_samples` gives."""
        path = samples / f"{name}.json"
        if not name.replace("_", "").isalnum() or not path.is_file():
            raise ToolError(f"There is no sample named {name!r}.")
        return _compact(json.loads(path.read_text())["response"], _base(ctx), sample=name)

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
    rows = data["edges" if "edges" in data else "links"] if isinstance(data, dict) else data
    if isinstance(data, dict):
        labels = {node["id"]: node["label"] for node in data["nodes"]}
        rows = [
            {**row, "source": labels.get(row["source"]), "target": labels.get(row["target"])}
            for row in rows
        ]
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


def _sample_summary(path: Path) -> dict[str, Any]:
    saved = json.loads(path.read_text())
    response = saved["response"]
    return {
        "name": path.stem,
        "question": saved["request"]["query"],
        "chart": response["visualization"]["type"],
        "totals": [c["total"] for c in response["meta"]["cohorts"]],
    }
