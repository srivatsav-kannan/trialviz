"""Building the real pipeline around a cassette, and reading evidence out of raw records."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
import httpx

from tests.workflows.cassette import RECORDING, Cassette
from trialviz.llm.budget import SpendLedger
from trialviz.pipeline import Pipeline, PipelineResult
from trialviz.query.interpreters.base import FallbackInterpreter, Interpreter
from trialviz.query.interpreters.claude import ClaudeInterpreter, MessagesClient
from trialviz.query.interpreters.rules import RuleBasedInterpreter
from trialviz.query.request import QueryRequest
from trialviz.query.resolver import QueryResolver
from trialviz.retrieval.client import RateLimiter, RegistryClient, ResponseCache
from trialviz.retrieval.retriever import Retriever
from trialviz.settings import Settings

TODAY = date(2026, 11, 7)
MODEL = "claude-opus-5-5"
MAX_RECORDS = 100
"""Small on purpose: large result sets exercise the capped, sorted fetch, and cassettes
stay small enough to commit."""


@dataclass
class Sleeps:
    """Stands in for asyncio.sleep during replay, so retries and spacing cost no time."""

    calls: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@dataclass
class Run:
    result: PipelineResult
    cassette: Cassette
    sleeps: Sleeps
    spent_usd: float
    """Claude spending during this run only."""


async def run_workflow(
    cassette: Cassette,
    request: Mapping[str, Any],
    tmp_path: Path,
    *,
    interpreter: Callable[[MessagesClient, SpendLedger], Interpreter] | None = None,
    registry_failures: Mapping[int, int] | None = None,
    cache: ResponseCache | None = None,
    spend_cap: float = 20.0,
) -> Run:
    settings = Settings()
    ledger = (
        SpendLedger(settings.llm_ledger_path, settings.llm_spend_cap_usd)
        if RECORDING
        else SpendLedger(tmp_path / "spend.json", spend_cap)
    )
    real = None
    if RECORDING and settings.anthropic_api_key:
        real = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value()
        ).beta.messages
    messages = cassette.claude_messages(real)  # type: ignore[arg-type]
    build = interpreter or default_interpreter
    sleeps = Sleeps()
    http = httpx.AsyncClient(transport=cassette.registry_transport(registry_failures))
    client = RegistryClient(
        http,
        RateLimiter(40) if RECORDING else RateLimiter(1e9, sleep=sleeps),
        cache=cache,
        sleep=asyncio.sleep if RECORDING else sleeps,
    )
    pipeline = Pipeline(
        QueryResolver(build(messages, ledger), today=lambda: TODAY),
        Retriever(client, max_records=MAX_RECORDS),
    )
    spent_before = ledger.spent_usd()
    try:
        result = await pipeline.run(QueryRequest.model_validate(request))
    finally:
        await http.aclose()
    cassette.save()
    return Run(result, cassette, sleeps, ledger.spent_usd() - spent_before)


def default_interpreter(messages: MessagesClient, ledger: SpendLedger) -> Interpreter:
    claude = ClaudeInterpreter(messages, ledger, model=MODEL, effort="low", max_tokens=8000)
    return FallbackInterpreter(claude, RuleBasedInterpreter())


# --- Reading evidence from raw registry records ------------------------------------------


def nct_id(study: Mapping[str, Any]) -> str:
    return str(study["protocolSection"]["identificationModule"]["nctId"])


def _module(study: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return study.get("protocolSection", {}).get(name, {})


def start_date(study: Mapping[str, Any]) -> str:
    """The start date as recorded. The registry mixes "2026-12" and "2026-12-01", so compare
    these at month precision, never as full strings."""
    return str(_module(study, "statusModule").get("startDateStruct", {}).get("date", ""))


def phases(study: Mapping[str, Any]) -> list[str]:
    return list(_module(study, "designModule").get("phases", []))


def status(study: Mapping[str, Any]) -> str:
    return str(_module(study, "statusModule").get("overallStatus"))


def countries(study: Mapping[str, Any]) -> set[str]:
    locations = _module(study, "contactsLocationsModule").get("locations", [])
    return {location.get("country") for location in locations}


def _meshes(study: Mapping[str, Any], module: str) -> list[str]:
    browse = study.get("derivedSection", {}).get(module, {})
    return [mesh.get("term", "") for mesh in browse.get("meshes", [])]


def intervention_text(study: Mapping[str, Any]) -> str:
    """Intervention names, other names and MeSH terms, lowercased: the fields a drug
    filter must match in."""
    interventions = _module(study, "armsInterventionsModule").get("interventions", [])
    names = [i.get("name", "") for i in interventions]
    names += [other for i in interventions for other in i.get("otherNames", [])]
    return " | ".join([*names, *_meshes(study, "interventionBrowseModule")]).lower()


def condition_text(study: Mapping[str, Any]) -> str:
    """Conditions, keywords and condition MeSH terms, lowercased: the fields a condition
    filter must match in."""
    module = _module(study, "conditionsModule")
    names = [*module.get("conditions", []), *module.get("keywords", [])]
    return " | ".join([*names, *_meshes(study, "conditionBrowseModule")]).lower()


def violations(
    studies: Iterable[Mapping[str, Any]], holds: Callable[[Mapping[str, Any]], bool]
) -> list[str]:
    """NCT IDs of the records that break `holds`; empty means every record satisfies it."""
    return [nct_id(study) for study in studies if not holds(study)]
