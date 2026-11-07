# trialviz

A backend that answers questions about clinical trials with visualization specs, backed by the [ClinicalTrials.gov API](https://clinicaltrials.gov/data-api/api). You send a question in plain language, optionally with structured fields, and get back a chart description a frontend can render, with every number traceable to the trial records behind it.

## Status

The service is being built stage by stage. This README describes only what runs today.

| Stage | State |
| --- | --- |
| Query understanding: request schema, interpretation, resolution into a plan | Done, tested, evaluated |
| Retrieval from ClinicalTrials.gov | Next |
| Aggregation, visualization specs, citations | Planned |

Today the service turns a question into a validated **plan**: which trials to look at and what to compute. You can try that through `POST /v1/interpret`.

## Quick start

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env    # optional: add ANTHROPIC_API_KEY to use Claude as the interpreter
uv run uvicorn trialviz.api:create_app --factory --reload
```

```bash
curl -s localhost:8000/v1/interpret -H 'content-type: application/json' \
  -d '{"query": "How has the number of trials for this drug changed over time?", "drug_name": "Pembrolizumab"}'
```

Without an API key the service uses its rule-based interpreter. With one, Claude reads the question, and the rule-based interpreter takes over if Claude is unavailable for any reason. Spending on Claude is capped (see [Configuration](#configuration)).

Interactive API docs are served at `http://localhost:8000/docs`.

## How it works

```
question + structured fields
  → interpret   a model (or rules) proposes plan slots, as an untrusted proposal
  → resolve     validate every value, caller's fields win, fill defaults, record provenance
  → plan        a complete, typed description of what to compute
  → (next)      retrieve, aggregate, build the visualization spec with citations
```

The language model never produces a number. Its only job is to translate the question into a small, closed set of plan slots. Everything after that is deterministic code, so every value in a chart comes from the registry and can be cited. More in [docs/architecture.md](docs/architecture.md).

## Request and response

The request is a question plus any part of the plan you want to pin down. Every structured field is optional, and whatever you set is binding.

```json
{
  "query": "Compare phases for trials involving Keytruda vs Opdivo",
  "filters": { "start_year_from": 2018, "countries": ["USA"] },
  "analysis": { "group_by": "phase" },
  "presentation": { "top_n": 10 }
}
```

The assignment's flat fields (`drug_name`, `condition`, `trial_phase`, `sponsor`, `country`, `start_year`, `end_year`) are accepted as shorthands for the matching `filters` fields, so its example request works verbatim.

The response is one of two shapes, told apart by `status`:

- `resolved`: the plan, a `provenance` map saying where each value came from (`user`, `inferred`, `derived` or `default`), and `notes` explaining every decision a caller might otherwise guess at.
- `needs_clarification`: a `reason`, a message a person can act on, and the slot paths that would resolve it.

Invalid requests get a 422 with one error shape: `{"error": {"code", "message", "details": [{"path", "message"}]}}`.

The full field reference, with validation rules and examples, is in [docs/api.md](docs/api.md).

## Key design decisions

- **One plan, filled from two sources.** Structured fields and the question both fill the same plan, slot by slot. The caller's field wins every overlap, the question fills the gaps, and documented defaults cover the rest. Neither input is restricted to its own slots, so a caller can rely entirely on the question, entirely on fields (`"inference": "off"` makes the request fully deterministic), or any mix. See [docs/query-resolution.md](docs/query-resolution.md).
- **The interpreter proposes, code decides.** Interpreter output goes through the same parsers and consistency rules as caller input. An invalid guess is dropped with a note instead of failing the request, and a guess that contradicts a caller's field changes nothing but a note.
- **Nothing is dropped silently.** Overridden values, rejected guesses, fallbacks between interpreters, and constraints the plan cannot express ("more than 500 participants") all come back as notes.
- **A typed plan per analysis kind.** The resolved analysis is one of seven concrete shapes (count, distribution, time series, comparison, histogram, scatter, network), each with exactly its own parameters. A single table defines which parameters belong to which kind, and validation, resolution and the model's output schema all read it.
- **Values in the registry's own vocabulary.** Phases, statuses, sponsor classes and the 226 country names are the exact strings ClinicalTrials.gov stores, verified against its field statistics endpoint. Loose spellings ("Phase III", "USA", "Türkiye") are parsed into them. Ambiguous ones ("Korea", "active") are refused rather than guessed.
- **A hard spend cap.** Each Claude call reserves its worst-case cost before it is made and refuses if that could cross the cap. Spending is persisted, so the cap holds across restarts.

## Testing

```bash
uv run pytest                 # offline suite, about 4 seconds
uv run pytest --run-live      # adds a few real Claude calls (about $0.03 each)
uv run python -m evals.interpretation.run --interpreter rules
uv run python -m evals.interpretation.run --interpreter claude
```

There are three layers. Section suites test each stage on its own with deliberately hostile inputs. Property-based tests check the resolver's guarantees over thousands of generated combinations. An evaluation scores both interpreters on 49 labeled questions. Workflow tests that run every stage together come once the pipeline is complete. Details in [docs/testing.md](docs/testing.md).

Current interpretation results (49 questions, scored on the final resolved plan):

| Interpreter | Score |
| --- | --- |
| Claude (`claude-opus-5-5`, low effort) | 49/49 on each of the last three runs |
| Rule-based | 16/49 |

The rule-based interpreter fails mostly on purpose: it cannot tell a drug from a disease, so it files every name it finds as a free-text keyword instead of guessing. Read the Claude score with the caveats in [evals/interpretation/README.md](evals/interpretation/README.md), chief among them that the questions and the prompt share an author.

## Configuration

Settings are read from the environment or `.env`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | unset | Enables the Claude interpreter |
| `INTERPRETER_MODEL` | `claude-opus-5-5` | Model used for interpretation |
| `INTERPRETER_EFFORT` | `low` | Reasoning effort (`low`, `medium`, `high`) |
| `INTERPRETER_MAX_TOKENS` | `8000` | Output ceiling per call, which also bounds the cost reservation |
| `LLM_SPEND_CAP_USD` | `20` | Hard cap on total Claude spending |
| `LLM_LEDGER_PATH` | `var/llm_spend.json` | Where spending is recorded |

## Limitations

- Year filters bound the trial **start** date only. A question about trials completed in a given year gets a completion-date time series, but its year bounds still apply to start dates.
- Values inside one filter are alternatives (any of). "Keytruda in combination with chemotherapy" cannot be expressed as one filter, so it is reported back as a constraint that was not applied.
- Drug and condition names are passed on as written. Matching them against the registry (synonyms, brand and generic names) happens at retrieval, which is the next stage.
- The rule-based interpreter is a fallback and a baseline, not a peer of the language model.
