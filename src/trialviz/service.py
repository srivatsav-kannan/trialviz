"""The service behind every way in: the HTTP routes, the stream and the MCP tools.

`Service` holds what they share, which is the interpreter, the pipeline, the answer store,
the rate limits and the cap on answers in progress, and it implements each operation once,
so a question asked over HTTP, over the stream or by an agent is limited, answered, saved
and reported in exactly the same way. `api.py` only maps HTTP onto it.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from starlette.requests import Request

from trialviz.answer import Answer
from trialviz.limits import AnswerSlots, SlidingWindowLimiter, Window, client_address
from trialviz.llm.budget import BudgetExceeded, FileLedger, Ledger, PostgresLedger
from trialviz.openapi import SAMPLES_DIRECTORY, BudgetStatus, LimitStatus, ServiceStatus
from trialviz.pipeline import Pipeline
from trialviz.query.interpreters.base import FallbackInterpreter, Interpreter
from trialviz.query.interpreters.claude import ClaudeInterpreter
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.request import QueryRequest
from trialviz.query.resolution import ClarificationNeeded, NoteCode, Resolution
from trialviz.query.resolver import QueryResolver
from trialviz.retrieval.retriever import Retriever, build_retriever
from trialviz.settings import Settings
from trialviz.store import AnswerStore, FileStore, NoStore, PostgresStore, key_for

_SAMPLE_NAME = re.compile(r"[0-9a-z_]+")
_ANSWER_ID = re.compile(r"[0-9a-f]{20}")


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


class Service:
    """One running service. The optional arguments replace a part built from `settings`,
    which is how tests run it against recordings."""

    def __init__(
        self,
        settings: Settings,
        interpreter: Interpreter | None = None,
        retriever: Retriever | None = None,
        ledger: Ledger | None = None,
        store: AnswerStore | None = None,
    ) -> None:
        if interpreter is None and ledger is None and settings.anthropic_api_key is not None:
            ledger = build_ledger(settings)
        self.settings = settings
        self.interpreter = interpreter or build_interpreter(settings, ledger)
        self.ledger = ledger
        self.resolver = QueryResolver(self.interpreter, today=date.today)
        self.owns_retriever = retriever is None
        self.retriever = retriever or build_retriever(settings)
        self.pipeline = Pipeline(
            self.resolver, self.retriever, deadline_seconds=settings.answer_deadline_seconds
        )
        self.store = store or build_store(settings)
        self.slots = AnswerSlots(settings.max_concurrent_answers)
        self.questions = SlidingWindowLimiter(
            [
                Window(settings.questions_per_minute, 60, "per minute"),
                Window(settings.questions_per_hour, 3600, "per hour"),
            ],
            what="questions",
        )
        self.all_questions = SlidingWindowLimiter(
            [Window(settings.questions_per_hour_total, 3600, "per hour across all clients")],
            what="questions",
        )
        self.requests = SlidingWindowLimiter(
            [Window(settings.requests_per_minute, 60, "per minute")], what="requests"
        )

    async def aclose(self) -> None:
        if self.owns_retriever:
            await self.retriever.aclose()

    def client(self, request: Request) -> str:
        return client_address(request, self.settings.trusted_proxy_hops)

    async def question_quota(self, request: Request) -> None:
        """Counts a question against its client's limits and the overall ceiling, before
        any work is done. The overall ceiling is checked first, so a client's own quota is
        not spent on a question that is refused anyway."""
        if self.all_questions.peek("all").remaining == 0:
            self.all_questions.hit("all")  # raises with the time until a slot frees up
        request.state.quota = self.questions.hit(self.client(request))
        self.all_questions.hit("all")

    async def answer(self, request: QueryRequest) -> Answer | ClarificationNeeded | dict[str, Any]:
        """A saved answer to the same request against the same data if there is one, which
        costs nothing; otherwise a new answer, saved under its id."""
        key = key_for(request, await self.retriever.data_version())
        if (saved := await self.store.get(key)) is not None:
            return saved
        async with self.slots.take():
            result = await self.pipeline.run(request)
        # An answer read by the fallback while Claude was unavailable is not saved, so that
        # the same request is read properly once Claude is back.
        if result.answer is not None and not any(
            note.code in (NoteCode.INTERPRETER_FALLBACK, NoteCode.INTERPRETER_UNAVAILABLE)
            for note in result.answer.meta.notes
        ):
            result.answer.meta.answer_id = key
            await self.store.put(
                key,
                result.answer.model_dump(mode="json"),
                request.model_dump(mode="json", exclude_none=True),
            )
        return result.response

    async def read(self, request: QueryRequest) -> Resolution:
        """The plan a request resolves to, without fetching anything."""
        async with self.slots.take():
            return await self.resolver.resolve(request)

    async def saved(self, answer_id: str) -> dict[str, Any] | None:
        return await self.store.get(answer_id) if _ANSWER_ID.fullmatch(answer_id) else None

    async def asked(self, answer_id: str) -> dict[str, Any] | None:
        return await self.store.request(answer_id) if _ANSWER_ID.fullmatch(answer_id) else None

    async def status(self, request: Request) -> ServiceStatus:
        """Which interpreter reads questions, what is left of the budget, and where the
        client stands against its limits."""
        uses_claude = _reads_with_claude(self.interpreter)
        budget, reason = None, None
        if not uses_claude:
            reason = "No language-model key is configured, so questions are read by rules."
        elif self.ledger is not None:
            try:
                spend = await self.ledger.status()
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
        standing = self.questions.peek(self.client(request))
        return ServiceStatus(
            interpreter="claude" if uses_claude else "rules",
            claude_available=uses_claude and reason is None,
            reason=reason,
            budget=budget,
            limits=LimitStatus(
                questions_per_minute=self.settings.questions_per_minute,
                questions_per_hour=self.settings.questions_per_hour,
                questions_remaining=standing.remaining,
                resets_in_seconds=standing.reset_seconds,
                answers_in_progress=self.slots.size - self.slots.free,
                max_answers_in_progress=self.slots.size,
                max_body_bytes=self.settings.max_body_bytes,
            ),
            client=self.client(request),
        )


def _reads_with_claude(interpreter: Interpreter) -> bool:
    chain = interpreter.chain if isinstance(interpreter, FallbackInterpreter) else (interpreter,)
    return any(isinstance(step, ClaudeInterpreter) for step in chain)


def sample_summaries() -> list[dict[str, Any]]:
    """The saved samples in `examples/`, each with its request, title, charts and totals."""
    return [_summarize(path) for path in sorted(SAMPLES_DIRECTORY.glob("*.json"))]


def load_sample(name: str) -> dict[str, Any] | None:
    """A saved sample's response, or None for a name that is not a saved sample."""
    path = SAMPLES_DIRECTORY / f"{name}.json"
    if not _SAMPLE_NAME.fullmatch(name) or not path.is_file():
        return None
    response: dict[str, Any] = json.loads(path.read_text())["response"]
    return response


def _summarize(path: Path) -> dict[str, Any]:
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
