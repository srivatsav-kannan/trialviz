# trialviz

trialviz answers questions about clinical trials with charts that a frontend can render directly, using data from the [ClinicalTrials.gov API](https://clinicaltrials.gov/data-api/api). A request is a question in plain English, optionally with structured fields such as a drug or a phase. The response is a recommended chart together with the reason it was chosen, the other charts that can draw the same data, a Vega-Lite spec wherever Vega-Lite can draw the chart, and a citation for every number that points to the trial, the field in its registry record, and the exact value stored there.

The service runs locally. Its home page at `http://localhost:8000` asks questions, `/samples` opens five saved answers without spending anything, `/docs` is the API reference, and the [documentation](#documentation) covers each stage in depth.

## Running it

The project needs Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env    # add ANTHROPIC_API_KEY here to let Claude read questions
uv run uvicorn trialviz.api:create_app --factory --reload
```

The service then listens on `http://localhost:8000`. The API reference at `/docs` is generated from the service's own OpenAPI document and includes request examples, real response examples, every error, and a curl command for every endpoint. Without an API key, questions are read by a rule-based interpreter that understands years, phases, statuses and common analysis phrasing but cannot tell a drug from a disease. With a key, Claude reads the question and the rule-based interpreter takes over whenever Claude is unavailable. Spending on Claude is capped at $20 by default, and the cap holds across restarts because every call reserves its worst-case cost in a ledger on disk before it is made.

From the command line, the example request from the assignment looks like this:

```bash
curl -s localhost:8000/v1/query -H 'content-type: application/json' \
  -d '{"query": "How has the number of trials for this drug changed over time?", "drug_name": "Pembrolizumab"}'
```

## Requests and responses

A request carries a question and any part of the plan that the caller wants to pin down. Whatever the caller sets is binding, and the question fills in the rest.

```json
{
  "query": "Compare phases for trials involving Keytruda vs Opdivo",
  "filters": { "start_year_from": 2018, "countries": ["United States"] },
  "analysis": { "group_by": "phase" },
  "presentation": { "top_n": 10 }
}
```

The assignment's flat fields (`drug_name`, `condition`, `trial_phase`, `sponsor`, `country`, `start_year`, `end_year`) are accepted as shorthands for the matching filters, so its example request works unchanged. `POST /v1/interpret` takes the same request and returns the resolved plan without fetching any data, which shows how a question was read before anything runs.

A successful response has `"status": "resolved"` and three parts. `visualization` is the recommended chart, with its type, title, the reason it was chosen, an encoding that maps visual channels to row fields, the rows themselves and a Vega-Lite spec. `alternatives` lists every other chart that can draw the same data, each with its own reason, so a frontend can offer a switch without another request. `meta` records how the answer was produced: the resolved plan and where each value came from (the caller, the question, a derivation or a default), a note for every decision a reader might otherwise misjudge, the charts that were ruled out and why, the trials that mention the drug or condition only in their descriptions, and the exact registry requests behind the numbers. When a question cannot be answered as asked, the response has `"status": "needs_clarification"` instead, with a message a person can act on and the fields that would settle it.

Each data point carries its trial count, the IDs of every trial behind it, and citations such as this one:

```json
{
  "nct_id": "NCT05204290",
  "field": "protocolSection.designModule.phases",
  "excerpt": "[\"EARLY_PHASE1\"]",
  "kind": "value",
  "url": "https://clinicaltrials.gov/study/NCT05204290"
}
```

Every error has the same shape, `{"error": {"code", "message", "details": [{"path", "message"}]}}`, whether it is a 422 for an invalid request, a 404 for an unknown path, a 502 or 503 when the registry fails, or a 504 when an answer runs past its deadline. The full field reference, including the row format for every chart type, is in [docs/api.md](docs/api.md).

## How a question becomes a chart

```
question and structured fields
  → interpret   a language model (or the rules) proposes values for plan slots
  → resolve     every value is validated, the caller's fields win, defaults fill the gaps
  → retrieve    the plan becomes registry queries by a fixed table, and records are fetched
  → aggregate   records are grouped and measured, with a citation behind every number
  → visualize   a chart is recommended by fixed rules, with its alternatives and specs
```

The language model's only job is the first step, where it proposes values for a small, closed set of plan slots. Its proposal goes through the same parsers and consistency checks as the caller's fields, so an invalid guess is dropped with a note and a guess that contradicts the caller changes nothing. Everything after the plan is deterministic code, which means every number in a chart comes from registry records and the same plan always produces the same chart. [docs/architecture.md](docs/architecture.md) walks through the stages and the reasons for the boundaries between them.

The chart is chosen the same way. The shape of the data (counts per category, counts over time, numeric spreads, one point per trial, a network, a flow across stages, or a list of trials) determines which of the 22 chart types can draw it. Each candidate then checks rules against the actual data, such as a donut needing two to five groups that are shares of one whole, and the candidates that fit are scored on documented preferences such as lines for trends and sorted horizontal bars for rankings. A chart requested by the caller or named in the question wins whenever it fits, and a note explains the choice when it does not. [docs/visualization.md](docs/visualization.md) lists the catalog and every rule.

## Samples

Five questions were run against the live registry on 6 November 2026, and their complete responses are saved in [examples/](examples), which is also what the `/samples` page shows. Running `uv run python -m examples.run_examples` regenerates them, and the counts drift slightly because the registry updates daily.

| Question | Recommended chart | Alternatives | Trials |
| --- | --- | --- | --- |
| [How has the number of trials for this drug changed over time? (drug: Pembrolizumab)](examples/01_assignment_example.json) | Time series | Bar chart, table | 2,659 |
| [Compare phases for trials involving Keytruda vs Opdivo since 2018](examples/02_compare_keytruda_opdivo.json) | Grouped bars | Stacked bars, normalized bars, heatmap, table | 2,053 and 1,265 |
| [Which countries have the most recruiting melanoma trials?](examples/03_recruiting_melanoma_by_country.json) | Ranked horizontal bars | World map, treemap, table | 471 |
| [Show a network of sponsors and drugs for multiple myeloma trials that started in 2024 (intervention type: drug)](examples/04_myeloma_sponsor_drug_network.json) | Network | 3D network, table | 117 |
| [How do lung cancer trials started in 2016 flow from sponsor class to phase to overall status?](examples/05_lung_cancer_flow.json) | Sankey diagram | Table | 565 |

The fourth question can be read two ways, and Claude has read it both as only the trials with a drug intervention (117 trials) and as every myeloma trial started in 2024 (186 trials, which also draws biopsies and scans as interventions). The saved request therefore pins `intervention_types` to `DRUG`, which is what the structured fields are for: whatever a caller sets is binding, and the plan and the chart's subtitle state the reading either way.

## Design decisions

### Reading the question

Structured fields and the question fill the same plan slot by slot, with the caller's fields winning every overlap, the question filling the gaps and documented defaults covering the rest, so a caller can rely entirely on the question, entirely on fields (`"inference": "off"` makes a request fully deterministic) or on any mix of the two. The resolved analysis is one of nine typed shapes (count, distribution, time series, comparison, histogram, scatter, network, flow and trial list), and a single table in `query/plan.py` defines the parameters of each, which request validation, the resolver and the model's output schema all read. Values use the registry's own vocabulary: phases, statuses, sponsor classes and the 226 country names are the exact strings ClinicalTrials.gov stores, loose spellings such as "Phase III" or "Türkiye" are parsed into them, and an ambiguous name such as "Korea" gets an error that names both candidates. Constraints the plan cannot express, such as "more than 500 participants", come back as notes so that the caller knows the answer is broader than the question. [docs/query-resolution.md](docs/query-resolution.md) has the full resolution model.

### Retrieval

The plan maps to API parameters through a fixed table, and the number of requests follows fixed rules as well: one query per comparison cohort, a count before any fetch, only the fields the analysis reads, and a reproducible subset of 10,000 trials when more than that match, shared between the cohorts of a comparison. An answer whose numbers are the registry's own counts, such as a single count or a comparison of cohort totals, fetches only the few records it cites. Drug and condition names have to appear in the fields that name a trial's interventions or conditions, because the registry's default search also matches description text, and recordings of real queries showed that about one in ten "Keytruda" trials only listed pembrolizumab as a permitted option. That change raised precision from about 90 percent to over 99 percent at a cost of 3 to 15 percent of matches.

A name of several words is matched there as a phrase, because loose words match across a trial's separate conditions: "type 1 diabetes" and "type 2 diabetes" shared 641 trials as loose words and 506 as phrases, the rest being trials that really study both. A condition also matches the MeSH terms that sit under it, which the registry cannot search for directly, so retrieval learns them from a sample of trials before running the query. "Lung cancer" then includes trials filed only under "Carcinoma, Non-Small-Cell Lung", which an independent audit found being reported as mere mentions, and 565 lung cancer trials started in 2016 are counted where 516 were before. The trials that mention a name only in their descriptions are counted by a second query that splits the default search exactly, and every answer reports them with a few quoted examples, while counting them requires `retrieval.name_matching: "mentioned"`, since telling "patients will receive pembrolizumab" apart from "agents include pembrolizumab" takes a judgment the pipeline should not make on its own.

Names from callers or the model are reduced to plain lowercase words before they reach the registry, which would otherwise execute search syntax embedded in them, and a name that reduces to nothing, such as "???", is refused instead of silently dropping its filter. Cached responses are stored per registry data version, which the server checks again every 15 minutes, so a long-running server moves to the registry's daily refresh and discards the older responses. [docs/retrieval.md](docs/retrieval.md) covers the query translation and the measurements behind these choices.

### Citations and counts

Every citation is a JSON path into one trial's registry record together with the exact value found there, and it shows why that trial belongs to that data point: its phases for a phase bar, the matching entry in its locations for a country bar, or the treatment arm that gives two drugs together for a combination. A count cites every active filter for each trial it cites, and a drug or condition only where the record names it. When more than 10,000 trials match, each group in the chart is counted by the registry itself with a query that defines the group exactly as aggregation does, so that a "Phase 2" bar excludes Phase 1/2 trials, and the per-status counts for breast cancer sum to the registry's total of 16,344. A phase filter follows the registry instead, so a "Phase 2" filter includes Phase 1/2 and Phase 2/3 trials, and a note says so. A chart that cannot be counted exactly stays an estimate throughout and says so, because a chart that mixed exact and estimated bars would mislead. [docs/aggregation.md](docs/aggregation.md) describes the groups, the datasets and the exact-count queries.

## Testing and evaluation

```bash
uv run pytest                                    # offline, about 5 seconds
uv run pytest --run-live                         # adds requests to the real registry
TRIALVIZ_RECORD=1 uv run pytest tests/workflows  # re-records Claude and registry traffic
uv run python -m evals.run --interpreter claude  # scores interpretation, about $0.40 a run
```

Most of the 35 offline tests run a whole question through the pipeline using recordings of real Claude and registry traffic, and then check that every fetched record satisfies the question, that the recommended chart is the right one, and that every citation resolves to the exact value in the record it cites. Three live tests confirm that the registry's search still behaves the way retrieval assumes. To make sure the tests could fail, 32 bugs were introduced into the code one at a time, and the suite caught all of them. That did not make the suite complete: an independent code review and an independent audit of the answers against the registry later found bugs it had missed, among them unbounded responses, a filter that a name of punctuation silently dropped, citations of the wrong drug, a cache that a long-running server never refreshed, and lung cancer trials left out because they were filed under a narrower MeSH term. Each of those is fixed and now has a test. [docs/testing.md](docs/testing.md) describes each test and what it guards.

Interpretation is measured separately on 54 labeled questions, scored on the final resolved plan. Claude (`claude-opus-5-5` at low effort) gets all 54 right and the rule-based interpreter gets 16, mostly because it files every name it finds as a free-text keyword. The questions and the prompt were written in the same session, so [docs/evaluation.md](docs/evaluation.md) explains how far that score can be trusted.

## Configuration

Settings are read from the environment or from `.env`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | unset | Enables the Claude interpreter |
| `INTERPRETER_MODEL` | `claude-opus-5-5` | Model used for interpretation |
| `INTERPRETER_EFFORT` | `low` | Reasoning effort (`low`, `medium` or `high`) |
| `INTERPRETER_MAX_TOKENS` | `8000` | Output ceiling per call, which also bounds the cost reservation |
| `LLM_SPEND_CAP_USD` | `20` | Hard cap on total Claude spending |
| `LLM_LEDGER_PATH` | `var/llm_spend.json` | Where spending is recorded |
| `CTGOV_REQUESTS_PER_MINUTE` | `50` | Spacing between registry requests |
| `CTGOV_CACHE_DIR` | `var/cache/ctgov` | Registry response cache, disabled when set empty |
| `RETRIEVAL_MAX_RECORDS` | `10000` | Most records fetched per cohort |
| `CTGOV_TIMEOUT_SECONDS` | `30` | Timeout for each registry request |
| `ANSWER_DEADLINE_SECONDS` | `180` | Longest one answer may spend on registry requests before it returns a 504 |

## Limitations

- Year filters bound the trial's start date only, so a question about trials completed in a given year gets a completion-date time series whose year bounds still apply to start dates.
- Values within one filter are alternatives, so "Keytruda in combination with chemotherapy" cannot be expressed as a filter and comes back as a constraint that was not applied.
- Names are matched by the registry's own search, which expands synonyms and brand names but does not correct spelling, so a misspelled drug simply matches fewer trials.
- When more than 10,000 trials match and a chart cannot be counted exactly (sponsors and drugs are free text, and some charts would need more than 30 counts), its figures describe the 10,000 most recently started trials, and the response says so.
- A distribution of the drugs combined with a given drug shows that drug as its largest bar, which a drug network answers better.
- A drug network links two drugs given in the same arm unless the arm's label or description says they are alternatives ("investigator's choice", "monotherapy", "nivolumab or pembrolizumab"), so an arm that offers a choice without saying so is still read as a combination.
- A trial that lists a drug only among one intervention's other names, as one of several alternatives, still matches that drug, which the audit measured at about 1.4 percent of the pembrolizumab trials.
- A status filter applies to the trial's overall status, so "recruiting trials by country" counts a country even when none of the trial's sites there is recruiting yet (Germany has 38 recruiting melanoma trials by overall status and 34 by site status).
- MeSH terms under a condition are learned from samples of nearby trials, so a narrower term that never appears alone on such a trial is not added.
- An answer that would need more than three minutes of registry requests, such as a comparison of six very large cohorts, stops with a 504 and a suggestion to narrow the question.
- Scatter plots over thousands of trials produce large responses, about 1 MB for 1,300 points, because every point carries its citations.
- Claude can read an ambiguous question differently from one run to the next, as the fourth sample shows, although the plan always states the reading it used.
- The demo pages and the API reference load their libraries from public CDNs, so the browser needs internet access.

## Documentation

- [docs/architecture.md](docs/architecture.md): the stages, the boundaries between them, the module map and how to extend each part
- [docs/query-resolution.md](docs/query-resolution.md): how fields and the question become one plan, with provenance for every value
- [docs/retrieval.md](docs/retrieval.md): how a plan becomes registry queries, and the measurements behind name matching
- [docs/aggregation.md](docs/aggregation.md): groups, datasets, citations and exact counts for large result sets
- [docs/visualization.md](docs/visualization.md): the chart catalog, the selection rules and what a frontend receives
- [docs/api.md](docs/api.md): the complete request and response reference
- [docs/testing.md](docs/testing.md): the workflow tests, the recordings they replay and the bugs they caught
- [docs/evaluation.md](docs/evaluation.md): the interpretation eval, its results and its caveats
