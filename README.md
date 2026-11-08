# trialviz

A backend that answers questions about clinical trials with visualization specs, backed by the [ClinicalTrials.gov API](https://clinicaltrials.gov/data-api/api). You send a question in plain language, optionally with structured fields. You get back a recommended chart with the reason for it, alternative charts over the same data, a ready-to-render Vega-Lite spec where one exists, and a citation for every number: the trial, the field in its registry record, and the exact value found there.

Five real questions and the JSON they produced are in [examples/](examples). A demo UI that draws every chart type from the response is served at `/`.

## Quick start

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env    # optional: add ANTHROPIC_API_KEY to use Claude as the interpreter
uv run uvicorn trialviz.api:create_app --factory --reload
```

```bash
curl -s localhost:8000/v1/query -H 'content-type: application/json' \
  -d '{"query": "How has the number of trials for this drug changed over time?", "drug_name": "Pembrolizumab"}'
```

`POST /v1/interpret` takes the same request and returns only the resolved plan, without fetching anything.

Without an API key the service uses its rule-based interpreter. With one, Claude reads the question, and the rule-based interpreter takes over if Claude is unavailable for any reason. Spending on Claude is capped (see [Configuration](#configuration)).

Open `http://localhost:8000` for the demo UI: ask a question, switch between the recommended chart and its alternatives, and select any bar, point or node to see the trials behind it and the registry field each was counted by. It is plain HTML and JavaScript with no build step ([docs/visualization.md](docs/visualization.md#the-demo-ui)). Interactive API docs are served at `http://localhost:8000/docs`.

## How it works

```
question + structured fields
  → interpret   a model (or rules) proposes plan slots, as an untrusted proposal
  → resolve     validate every value, caller's fields win, fill defaults, record provenance
  → plan        a complete, typed description of what to compute
  → retrieve    translate the plan into registry queries and fetch the records, by fixed rules
  → aggregate   group and measure the records, citing the field behind every number
  → visualize   recommend a chart by fixed rules, list the alternatives, write the specs
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

- `resolved`: the answer.
  - `visualization`: the recommended chart, with `type`, `title`, `reason`, `encoding` (channels mapped to row fields), `data` and `vega_lite`.
  - `alternatives`: every other chart that can draw the same data, each with its reason.
  - `meta`: the resolved plan with the source of each value (`user`, `inferred`, `derived` or `default`), every note, how the chart was chosen (including the charts ruled out and why), the trials that only mention the drug or condition, and the exact registry requests behind the numbers.
- `needs_clarification`: a `reason`, a message a person can act on, and the slot paths that would resolve it.

Every data point carries `trial_count`, the IDs of every contributing trial, and citations such as `{"nct_id": "NCT05204290", "field": "protocolSection.designModule.phases", "excerpt": "[\"EARLY_PHASE1\"]"}`.

Invalid requests get a 422 with one error shape: `{"error": {"code", "message", "details": [{"path", "message"}]}}`.

The full field reference, with row formats for every chart, is in [docs/api.md](docs/api.md).

## How the chart is chosen

The data's shape decides which charts can draw it: counts per category, counts over two categories, counts over time, numeric values, points, nodes and links, flows, or individual trials. Each candidate then checks facts about the actual data and fits or says why not. For example, a donut needs 2 to 5 groups that are shares of one whole, a line needs at least 3 periods, and grouped bars need 5 or fewer bars per group. Fitting charts are scored on documented preferences (lines for trends, sorted bars for rankings, a world map for many countries), and the best one is recommended. A chart the caller or the question asks for wins whenever it fits, and a note explains when it does not. The choice is made by rules, never by the model, so the same data always gets the same chart.

The catalog covers 22 chart types, including choropleth maps, heatmaps, box plots, Sankey diagrams, trial timelines, and 3D networks and scatter plots. Networks come with precomputed 2D and 3D layouts. Details in [docs/visualization.md](docs/visualization.md).

## Key design decisions

- **One plan, filled from two sources.** Structured fields and the question both fill the same plan, slot by slot. The caller's field wins every overlap, the question fills the gaps, and documented defaults cover the rest. Neither input is restricted to its own slots, so a caller can rely entirely on the question, entirely on fields (`"inference": "off"` makes the request fully deterministic), or any mix. See [docs/query-resolution.md](docs/query-resolution.md).
- **The interpreter proposes, code decides.** Interpreter output goes through the same parsers and consistency rules as caller input. An invalid guess is dropped with a note instead of failing the request, and a guess that contradicts a caller's field changes nothing but a note.
- **Nothing is dropped silently.** Overridden values, rejected guesses, fallbacks between interpreters, and constraints the plan cannot express ("more than 500 participants") all come back as notes.
- **A typed plan per analysis kind.** The resolved analysis is one of nine concrete shapes (count, distribution, time series, comparison, histogram, scatter, network, flow, trial list), each with exactly its own parameters. A single table defines which parameters belong to which kind, and validation, resolution and the model's output schema all read it.
- **Values in the registry's own vocabulary.** Phases, statuses, sponsor classes and the 226 country names are the exact strings ClinicalTrials.gov stores, verified against its field statistics endpoint. Loose spellings ("Phase III", "USA", "Türkiye") are parsed into them. Ambiguous ones ("Korea", "active") are refused rather than guessed.
- **A hard spend cap.** Each Claude call reserves its worst-case cost before it is made and refuses if that could cross the cap. Spending is persisted, so the cap holds across restarts.
- **Deterministic retrieval.** The plan maps to API parameters by a fixed table, and how many requests it becomes follows fixed rules: one query per comparison cohort, a count first, only the records and fields the analysis reads, and a reproducible capped subset for very large result sets. See [docs/retrieval.md](docs/retrieval.md).
- **Drugs and conditions must be named, not just mentioned.** The registry's default search also matches description text, and recording real queries showed one in ten "Keytruda" trials only listed pembrolizumab as a permitted option. Names must now appear in the fields that name a trial's interventions or conditions, with synonyms still expanded. Precision went from about 90 to 99 percent or more, at a cost of 3 to 15 percent of matches.
- **Mentions are reported, never silently counted or lost.** Trials that mention a drug or condition only in descriptions are fetched by a second query that splits the default search exactly. By default they are reported but not counted, because telling "patients will receive pembrolizumab" from "agents include pembrolizumab" takes judgment the pipeline should not guess at. `retrieval.name_matching: mentioned` counts them, still labeled.
- **Names cannot inject query syntax.** The registry executes syntax found inside search terms, so names from people or the model are reduced to plain lowercase words before use. A live test proves an injected `AREA[Phase]PHASE3` is searched as words, not run as a filter.
- **A cache that cannot go stale.** Registry responses are cached under a key that includes the registry's data version, so a cached answer always matches the current data.
- **Citations anyone can check.** Every number is backed by the trials that make it up, and each citation is a JSON path into the trial's registry record with the exact value there. Every workflow test follows every citation back into the raw record. See [docs/aggregation.md](docs/aggregation.md).
- **Exact counts even when only part of the data is fetched.** When more than 10,000 trials match, each group is counted by the registry directly, matching groups exactly as aggregation defines them ("Phase 2" excludes Phase 1/2). For breast cancer by status, the counts sum to exactly the registry's 16,344. Charts that cannot be counted exactly stay estimates and say so, rather than mixing exact and estimated bars.
- **Combinations come from treatment arms.** Two drugs are linked only when they are given in the same arm, so a drug and its placebo comparator are not a combination.

## Testing

```bash
uv run pytest                                    # offline, about 4 seconds
uv run pytest --run-live                         # adds real registry requests
TRIALVIZ_RECORD=1 uv run pytest tests/workflows  # re-record against Claude and the registry
uv run python -m evals.interpretation.run --interpreter claude
```

The suite is small on purpose: 27 tests, most of them whole workflows. Each workflow runs a real question through the entire pipeline, replayed from recordings of the real Claude and registry traffic. It checks that every record actually satisfies the question, that the recommended chart is right, and that every citation in the answer resolves to the exact value in the record it cites. Six guard tests cover what workflows cannot reach, and three live tests confirm the registry's search behavior still holds. Thirty-two deliberate bugs, introduced one at a time, were all caught. Details in [docs/testing.md](docs/testing.md).

Current interpretation results (54 questions, scored on the final resolved plan):

| Interpreter | Score |
| --- | --- |
| Claude (`claude-opus-5-5`, low effort) | 54/54 |
| Rule-based | 16/54 |

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
| `CTGOV_REQUESTS_PER_MINUTE` | `50` | Spacing between registry requests |
| `CTGOV_CACHE_DIR` | `var/cache/ctgov` | Registry response cache. Set empty to disable. |
| `RETRIEVAL_MAX_RECORDS` | `10000` | Most records fetched per cohort before truncating |
| `CTGOV_TIMEOUT_SECONDS` | `30` | Per-request timeout |

## Limitations

- Year filters bound the trial **start** date only. A question about trials completed in a given year gets a completion-date time series, but its year bounds still apply to start dates.
- Values inside one filter are alternatives (any of). "Keytruda in combination with chemotherapy" cannot be expressed as one filter, so it is reported back as a constraint that was not applied.
- Drug and condition names are matched by the registry's own search, which handles synonyms and brand names. A misspelled name simply matches fewer trials. There is no spelling correction.
- When more than 10,000 trials match, records are fetched for a reproducible 10,000-trial subset. Group counts are then taken from the registry where possible. Where they cannot be (free-text dimensions such as sponsors or drugs, or charts needing more than 30 counts), the figures describe the most recently started trials, and the response says so.
- Asking which drugs are combined with a given drug, as a distribution, shows that drug itself as the largest bar. A drug-drug network answers that question better.
- Scatter plots over thousands of trials produce large responses (about 1 MB for 1,300 points), since every point carries its citations.
- The rule-based interpreter is a fallback and a baseline, not a peer of the language model.
- Claude's reading of a question that allows two readings can differ between runs, such as whether "sponsors and drugs" also restricts the trials to those with a drug intervention. The plan, its provenance and the subtitle always state which reading was used, and a caller who needs a fixed reading can set those fields.
- The demo UI loads its chart libraries from a CDN, so it needs internet access in the browser.
