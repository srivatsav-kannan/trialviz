"""Building the real pipeline around a cassette, and reading evidence out of raw records."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import anthropic
import httpx

from tests.workflows.cassette import RECORDING, Cassette
from trialviz.aggregation.evidence import at
from trialviz.llm.budget import FileLedger
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
    interpreter: Callable[[MessagesClient, FileLedger], Interpreter] | None = None,
    registry_failures: Mapping[int, int] | None = None,
    cache: ResponseCache | None = None,
) -> Run:
    settings = Settings()
    ledger = (
        FileLedger(settings.llm_ledger_path, settings.llm_spend_cap_usd)
        if RECORDING
        else FileLedger(tmp_path / "spend.json", 20.0)
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


def default_interpreter(messages: MessagesClient, ledger: FileLedger) -> Interpreter:
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


def mention_text(study: Mapping[str, Any]) -> str:
    """Titles, summary, keywords and intervention and arm descriptions, lowercased: where a
    mention-only trial mentions its drug or condition."""
    protocol = study.get("protocolSection", {})
    identification = protocol.get("identificationModule", {})
    arms = protocol.get("armsInterventionsModule", {})
    parts = [
        identification.get("briefTitle", ""),
        identification.get("officialTitle", ""),
        protocol.get("descriptionModule", {}).get("briefSummary", ""),
        *protocol.get("conditionsModule", {}).get("keywords", []),
        *(i.get("description", "") for i in arms.get("interventions", [])),
        *(g.get("description", "") for g in arms.get("armGroups", [])),
    ]
    return " | ".join(parts).lower()


def answer_problems(run: Run) -> list[str]:
    """Checks every answer must pass, whatever the question:

    - every citation resolves: following its path through the cited trial's raw record
      gives exactly its excerpt (a value citation) or text containing it (a quote);
    - every datum's trials were really fetched, each listed once, and its count agrees
      with them: equal, or larger when the count came from the registry;
    - the recommended chart and each alternative carry a Vega-Lite spec exactly when
      Vega-Lite can draw that chart.
    """
    answer, retrieval = run.result.answer, run.result.retrieval
    assert answer is not None and retrieval is not None
    records = {
        nct_id(study): study
        for cohort in retrieval.cohorts
        for study in (*cohort.named.studies, *(cohort.mentions.studies if cohort.mentions else ()))
    }
    # Counts can exceed the listed trials only when they came from the registry: exact
    # counts for a capped set, or a comparison of totals that fetched records only to cite.
    exact = any(n.code.value == "counts_exact_from_registry" for n in answer.meta.notes) or all(
        cohort.sampled for cohort in retrieval.cohorts
    )
    problems: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for citation in node.get("citations", []):
                problems.extend(_citation_problem(citation, records))
            if "nct_ids" in node and "trial_count" in node:
                unknown = [i for i in node["nct_ids"] if i not in records]
                if unknown:
                    problems.append(f"datum cites unfetched trials {unknown[:3]}")
                if len(set(node["nct_ids"])) != len(node["nct_ids"]):
                    problems.append("a datum counts the same trial more than once")
                count, listed = node["trial_count"], len(node["nct_ids"])
                if (count < listed) or (count != listed and not exact):
                    problems.append(f"datum count {count} disagrees with its {listed} trials")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(answer.visualization.model_dump(mode="json")["data"])
    for view in answer.alternatives:
        walk(view.model_dump(mode="json")["data"])
    for report in answer.meta.mentions:
        for citation in report.citations:
            problems.extend(_citation_problem(citation.model_dump(mode="json"), records))
    for view in [answer.visualization, *answer.alternatives]:
        if (view.vega_lite is not None) != (view.type.value in VEGA_CHARTS):
            problems.append(f"{view.type.value} has the wrong Vega-Lite presence")
    return problems


VEGA_CHARTS = {
    "bar_chart", "horizontal_bar_chart", "donut_chart", "choropleth_map", "grouped_bar_chart",
    "stacked_bar_chart", "normalized_stacked_bar_chart", "heatmap", "time_series",
    "stacked_area_chart", "histogram", "box_plot", "scatter_plot", "bubble_chart", "timeline",
}  # fmt: skip


def _citation_problem(
    citation: Mapping[str, Any], records: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    study = records.get(citation["nct_id"])
    if study is None:
        return [f"citation of unfetched trial {citation['nct_id']}"]
    try:
        value = at(study, citation["field"])
    except (KeyError, IndexError, TypeError):
        return [f"{citation['nct_id']}: path {citation['field']} does not exist"]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    holds = (
        citation["excerpt"] in text if citation["kind"] == "quote" else citation["excerpt"] == text
    )
    return (
        []
        if holds
        else [
            f"{citation['nct_id']}: {citation['field']} is {text[:60]!r}, "
            f"not {citation['excerpt'][:60]!r}"
        ]
    )


def violations(
    studies: Iterable[Mapping[str, Any]], holds: Callable[[Mapping[str, Any]], bool]
) -> list[str]:
    """NCT IDs of the records that break `holds`; empty means every record satisfies it."""
    return [nct_id(study) for study in studies if not holds(study)]
