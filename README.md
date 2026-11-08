# trialviz

trialviz answers questions about clinical trials with charts that a frontend can draw directly, using data from the [ClinicalTrials.gov API](https://clinicaltrials.gov/data-api/api). You ask a question in plain English, optionally with structured fields such as a drug or a phase, and get back a recommended chart with the reason it was chosen, the other charts that could show the same data, and a citation for every number that points to the trial record and the exact value it was counted from.

A live instance runs at **https://trialviz.onrender.com**, on Render's free plan, so the first visit after a quiet spell takes about a minute to wake. Its home page asks questions, [`/samples`](https://trialviz.onrender.com/samples) opens fourteen saved answers without spending anything, and [`/docs`](https://trialviz.onrender.com/docs) is the API reference. The same service runs locally with the steps below.

## Running it

The project needs Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env    # add ANTHROPIC_API_KEY here to let Claude read questions
uv run uvicorn trialviz.api:create_app --factory --reload
```

The service then listens on `http://localhost:8000`. Without an API key, questions are read by a rule-based interpreter that understands years, phases, statuses and common phrasing but cannot tell a drug from a disease. With a key, Claude reads the question, and the rule-based interpreter takes over whenever Claude is unavailable. Spending on Claude is capped at $20 by default, and the cap survives restarts because each call sets aside its worst-case cost before it is made.

The example request from the assignment works as written:

```bash
curl -s localhost:8000/v1/query -H 'content-type: application/json' \
  -d '{"query": "How has the number of trials for this drug changed over time?", "drug_name": "Pembrolizumab"}'
```

## What it can answer

Every question is read into the same kind of plan, so there is no special path for any one type of question. A plan filters trials by drug, condition, sponsor, phase, status, country, study type, intervention type and start year, and then computes one of nine things: a count, a distribution across some property, a trend over time, a comparison of two to six groups of trials, the spread of a number such as enrollment, a scatter of trials by two or three numbers, a network of entities that share trials, a flow of trials across two or three stages, or a list of the trials themselves. Trials can be grouped or linked by phase, status, sponsor, sponsor class, drug, intervention type, condition, country, study type, start year, investigator and site.

That covers every example in the assignment's appendix, including sponsor categories compared across two conditions, the most common intervention types for a drug, and both kinds of network (sponsors to drugs, and drugs given together in the same treatment arm), and it goes further, to Sankey flows, box plots, world maps, trial timelines and 3D networks. The answer's chart is chosen from 22 types by rules about the shape of the data, described below.

## Requests and responses

A request carries a question and any part of the plan the caller wants to pin down. Whatever the caller sets is binding, and the question fills in the rest.

```json
{
  "query": "Compare phases for trials involving Keytruda vs Opdivo",
  "filters": { "start_year_from": 2018, "countries": ["United States"] },
  "analysis": { "group_by": "phase" },
  "presentation": { "top_n": 10 }
}
```

The assignment's flat fields (`drug_name`, `condition`, `trial_phase`, `sponsor`, `country`, `start_year`, `end_year`) are accepted as shorthands for the matching filters. `POST /v1/interpret` takes the same request and returns the plan without fetching anything, which shows how a question will be read before it runs.

A successful response has `"status": "resolved"` and three parts. `visualization` is the recommended chart: its type, title, the reason it was chosen, an encoding that says which field goes on which axis, the rows to draw, and a Vega-Lite spec where Vega-Lite can draw the chart. `alternatives` holds every other chart that can show the same data, each with its own reason, so a frontend can offer a switch without asking again. `meta` explains the answer: the plan and where each of its values came from (the caller, the question, or a default), a note for every decision a reader might otherwise misjudge, the charts that were ruled out and why, the trials that mention the drug or condition only in passing, and the registry requests behind the numbers. When a question cannot be answered as asked, the response says `"status": "needs_clarification"` instead, with a message a person can act on.

Each data point carries its trial count, the IDs of every trial behind it, and citations like this one, which says that this trial is counted under Early Phase 1 because that is what its record lists:

```json
{
  "nct_id": "NCT05204290",
  "field": "protocolSection.designModule.phases",
  "excerpt": "[\"EARLY_PHASE1\"]",
  "kind": "value",
  "url": "https://clinicaltrials.gov/study/NCT05204290"
}
```

Every error has the same shape, `{"error": {"code", "message", "details"}}`. The full reference, with the rows each chart type uses, is in [docs/api.md](docs/api.md) and at `/docs` on the running service.

## How a question becomes a chart

```
question and structured fields
  → interpret   Claude (or the rules) proposes values for the plan
  → resolve     every value is checked, the caller's fields win, defaults fill the gaps
  → retrieve    the plan becomes registry queries, and the matching trials are fetched
  → aggregate   trials are grouped and counted, with a citation behind every number
  → visualize   a chart is recommended, with its alternatives
```

The language model only does the first step. It proposes values for a small, fixed set of plan fields, and those values are checked exactly like a caller's input, so a wrong guess is dropped with a note and a guess that contradicts the caller changes nothing. Everything after the plan is ordinary code, which means every number comes from registry records and the same plan always gives the same chart. [docs/architecture.md](docs/architecture.md) explains the stages and why the boundaries sit where they do.

The chart is chosen by fixed rules in code, and the model plays no part in it. The shape of the data decides which charts can draw it, each candidate checks the actual data (a donut needs two to five groups that make up one whole, for instance), and the ones that fit are ranked by plain preferences such as lines for trends and sorted bars for rankings. A chart the caller or the question asks for wins whenever it fits, and a note explains when it does not. [docs/visualization.md](docs/visualization.md) lists every chart and rule.

## Samples

Fourteen questions were run against the live registry in November 2026, and their complete responses are saved in [examples/](examples). The `/samples` page shows the same fourteen. Between them they use all 22 chart types, as the recommendation or as an alternative. Running `uv run python -m examples.run_examples` regenerates them, and the counts drift a little because the registry updates daily.

| Question | Recommended chart | Alternatives | Trials |
| --- | --- | --- | --- |
| [How has the number of trials for this drug changed over time? (drug: Pembrolizumab)](examples/01_assignment_example.json) | Time series | Bar chart, table | 2,659 |
| [Compare phases for trials involving Keytruda vs Opdivo since 2018](examples/02_compare_keytruda_opdivo.json) | Grouped bars | Stacked bars, normalized bars, heatmap, table | 2,053 and 1,265 |
| [Which countries have the most recruiting melanoma trials?](examples/03_recruiting_melanoma_by_country.json) | Ranked horizontal bars | World map, treemap, table | 471 |
| [Show a network of sponsors and drugs for multiple myeloma trials that started in 2024 (intervention type: drug)](examples/04_myeloma_sponsor_drug_network.json) | Network | 3D network, table | 117 |
| [How do lung cancer trials started in 2016 flow from sponsor class to phase to overall status?](examples/05_lung_cancer_flow.json) | Sankey diagram | Table | 565 |
| [How many semaglutide trials are recruiting in Germany?](examples/06_semaglutide_recruiting_germany.json) | Single number | None | 5 |
| [How does enrollment vary by phase for completed asthma trials?](examples/07_asthma_enrollment_by_phase.json) | Box plot | Histogram, table | 3,480 |
| [How do enrollment, duration and number of sites relate for completed phase 3 psoriasis trials?](examples/08_psoriasis_size_duration_sites.json) | Bubble chart | Scatter plot, 3D scatter plot, table | 345 |
| [List the recruiting phase 3 trials of tirzepatide](examples/09_tirzepatide_timeline.json) | Timeline | Table | 11 |
| [Which drugs frequently co-occur in combination studies for melanoma trials that started since 2020?](examples/10_melanoma_drug_combinations.json) | Network | 3D network, table | 704 |
| [Compare sponsor categories across breast cancer and prostate cancer trials](examples/11_sponsor_classes_two_cancers.json) | Normalized stacked bars | Stacked bars, heatmap, grouped bars, table | 16,270 and 7,327 |
| [What are the most common intervention types for Alzheimer's disease trials?](examples/12_alzheimers_intervention_types.json) | Ranked horizontal bars | Bar chart, treemap, table | 4,040 |
| [Show the split of Crohn's disease trials by study type as a donut chart](examples/13_crohns_study_types.json) | Donut | Bar chart, horizontal bars, table | 2,035 |
| [How has the number of obesity trials in each phase changed since 2016?](examples/14_obesity_phases_over_time.json) | Heatmap | Stacked area, table | 8,472 |

Claude has read the fourth question both as trials with a drug intervention (117) and as every myeloma trial started in 2024 (186, which also draws biopsies and scans), so the saved request sets the intervention type as a structured field. That is what the fields are for: whatever a caller sets is binding, and the subtitle states the reading either way. The thirteenth asks for a donut by name, which is how a question gets a chart that would not win on its own, and the fourteenth opens on a heatmap because its ten phase groups are more lines than a reader can tell apart.

## Design decisions

### Reading the question

The question and the structured fields fill the same plan, with the caller's fields winning any disagreement, so a caller can rely on the question alone, on fields alone (`"inference": "off"` skips the model entirely), or on both. Values are stored in the registry's own vocabulary: loose spellings such as "Phase III" or "Türkiye" are converted to the registry's terms, and an ambiguous one such as "Korea" is rejected with both possible countries named. When a question asks for something the plan cannot express, such as "more than 500 participants", the answer says so in a note, so a broader question is never answered quietly in its place. [docs/query-resolution.md](docs/query-resolution.md) has the details.

### Retrieval

How many requests a question becomes follows fixed rules: one query per group being compared, a count before any download, only the fields the chart needs, and at most 10,000 trials per query, taken in a fixed order when more match. An answer whose numbers are the registry's own counts, such as a single total, downloads only the few trials it cites.

A drug or condition has to appear in the fields where a trial names its interventions or conditions. The registry's default search also reads descriptions, and in recorded queries about one in ten "Keytruda" trials only listed pembrolizumab as one allowed option among several. Searching the naming fields raised precision from about 90 percent to over 99 percent, at the cost of 3 to 15 percent of matches, and the trials left out are still reported in every answer as mentions, with quoted examples. Names of several words are searched as phrases, so that "type 1 diabetes" no longer matches a type 2 trial that happens to have the keyword "phase 1". A condition also includes the narrower MeSH terms filed under it, which the registry cannot search for directly, so "lung cancer" counts trials filed only under "Carcinoma, Non-Small-Cell Lung". [docs/retrieval.md](docs/retrieval.md) covers the measurements behind these choices.

### Protecting the service

Every question can call the language model and make dozens of registry requests, so the service limits them before doing any work: per client each minute and hour, across all clients each hour, and to a few answered at once, with a `Retry-After` on every refusal and `GET /v1/status` showing where a client stands. Spending on Claude has a total and a daily cap, and each call sets aside its worst-case cost before it is made. On the deployed service the running total lives in Postgres, so the caps survive restarts, and if that database cannot be reached the model is simply not called. [docs/api.md](docs/api.md#limits) lists every limit.

### Citations and counts

A citation shows why a trial belongs to a particular data point: its phase for a phase bar, the matching site for a country bar, the treatment arm that gives two drugs together for a combination. When more than 10,000 trials match, each bar is counted by the registry itself with a query that defines the group exactly as the chart does, so the bars are exact even though only part of the data was downloaded. For breast cancer by status, the bars add up to the registry's total of 16,344. A chart that cannot be counted that way is marked as an estimate throughout, since mixing exact and estimated bars would mislead. [docs/aggregation.md](docs/aggregation.md) describes the groups and the counting.

## Testing and evaluation

```bash
uv run pytest                                    # offline, about 5 seconds
uv run pytest --run-live                         # adds requests to the real registry
TRIALVIZ_RECORD=1 uv run pytest tests/workflows  # re-records Claude and registry traffic
uv run python -m evals.run --interpreter claude  # scores interpretation, about $0.40 a run
```

Most of the 50 offline tests run a whole question through the service, replayed from recordings of real Claude and registry traffic, and check that every trial fetched really matches the question, that the chart is the right one, and that every citation leads to the exact value in the record it cites. Three live tests check that the registry's search still behaves the way the service assumes. Beyond the tests, every saved answer was recomputed independently against the registry with separate queries. The last recount found two real errors, which were fixed before the answers were saved again. [docs/testing.md](docs/testing.md) describes each test.

Interpretation is measured on 54 labeled questions, scored on the final plan. Claude (`claude-opus-5-5` at low effort) gets all 54 right and the rule-based interpreter gets 15, mostly because it cannot tell drugs from diseases. The questions and the prompt were written together, so [docs/evaluation.md](docs/evaluation.md) explains how far that score can be trusted.

## Configuration

Settings are read from the environment or from `.env`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | unset | Enables the Claude interpreter |
| `INTERPRETER_MODEL` | `claude-opus-5-5` | Model used to read questions |
| `INTERPRETER_EFFORT` | `low` | Reasoning effort (`low`, `medium` or `high`) |
| `INTERPRETER_MAX_TOKENS` | `8000` | Longest answer the model may give, which also bounds what each call sets aside |
| `LLM_SPEND_CAP_USD` | `20` | Hard cap on total Claude spending |
| `LLM_DAILY_CAP_USD` | unset | Optional cap on Claude spending per UTC day; empty means none |
| `DATABASE_URL` | unset | Postgres for the spend total, so the caps hold across restarts and processes; without it, spending is kept in `LLM_LEDGER_PATH` |
| `LLM_LEDGER_PATH` | `var/llm_spend.json` | Where spending is recorded locally |
| `CTGOV_REQUESTS_PER_MINUTE` | `50` | Spacing between registry requests |
| `CTGOV_CACHE_DIR` | `var/cache/ctgov` | Registry response cache, disabled when set empty |
| `RETRIEVAL_MAX_RECORDS` | `10000` | Most trials downloaded per query |
| `CTGOV_TIMEOUT_SECONDS` | `30` | Timeout for each registry request |
| `ANSWER_DEADLINE_SECONDS` | `180` | Longest one answer may spend on registry requests |
| `QUESTIONS_PER_MINUTE`, `QUESTIONS_PER_HOUR` | `10`, `60` | Questions one client may ask |
| `QUESTIONS_PER_HOUR_TOTAL` | `600` | Questions all clients together may ask per hour |
| `REQUESTS_PER_MINUTE` | `120` | Other requests one client may make per minute |
| `MAX_CONCURRENT_ANSWERS` | `4` | Questions answered at once |
| `MAX_BODY_BYTES` | `16384` | Largest request body accepted |
| `TRUSTED_PROXY_HOPS` | `0` | Proxies in front of the service, so clients are told apart by the address the proxy saw |

## Limitations

- Year filters apply to a trial's start date only, even when the question is about completion dates.
- Values within one filter are alternatives, so "Keytruda in combination with chemotherapy" cannot be expressed as a filter, and the answer says so.
- Names are matched by the registry's own search, which knows synonyms and brand names but does not correct spelling.
- When more than 10,000 trials match and a chart cannot be counted by the registry (sponsors, drugs, investigators and sites are free text), its figures describe the 10,000 most recently started trials, and the answer says so.
- A drug network treats two drugs in the same treatment arm as a combination unless the arm's wording says they are alternatives, so an arm that offers a choice without saying so is still read as a combination.
- An intervention named as a combination is split into its drugs only at joining words such as "plus" or "+", so "Nivolumab-Relatlimab" stays one drug under its own name.
- A trial that lists a drug only as one of several alternative names for an intervention still matches that drug, which affects about 1.4 percent of pembrolizumab trials.
- A status filter uses the trial's overall status, so "recruiting trials by country" can count a country where the trial's sites are not yet recruiting.
- Narrower MeSH terms are learned from samples of nearby trials, so a rare one can be missed.
- Investigator names are written by each trial, so one person can appear under two spellings.
- An answer that would need more than three minutes of registry requests stops with a suggestion to narrow the question.
- Claude can read an ambiguous question differently from one run to the next, although the answer always states the reading it used.
- The demo pages load their chart libraries from public CDNs, so the browser needs internet access.

## What I would improve with more time

With more time, I would take on the trials that the service reports but never counts, the ones that mention a drug only in their descriptions. Deciding that "patients will receive pembrolizumab" belongs in a count and "agents may include pembrolizumab" does not takes judgment, so I would let a model make that call only in a form the code can check: it returns a verdict together with the exact sentence that supports it, the code confirms that sentence is in the record, and the trial counts with the quote attached as its citation. The same approach could read each treatment arm into a structured regimen, which would let the drug network tell real combinations apart from arms that offer a choice between drugs, and in both cases every judgment would stay as traceable and reproducible as a field value.

## How this was built

Claude Code, running Opus 5.5, wrote most of the code, and I made the architectural decisions it built to. I chose the framework (Python with FastAPI and Pydantic) and the style and structure of the documentation, and I decided how determinism and the language model would share the work of reading a question: the model proposes values for a fixed set of plan fields, code checks each one the way it checks a caller's input, the caller's own fields always win, and no number in an answer ever comes from the model. The same thinking carried into the later stages, where I decided that retrieval follows fixed rules down to how many requests a question becomes, that trials which only mention a drug are reported in every answer and counted only on request, that the backend recommends one chart with its reasons and alternatives, including 3D networks, and that the deployed service keeps its spending cap in a database so that it survives restarts. I also set the approach to testing, a small number of demanding tests that run real questions end to end and follow every citation back to its record, and I had every saved answer recomputed independently against the registry before trusting it.

## Documentation

- [docs/architecture.md](docs/architecture.md): the stages, the boundaries between them, and how to extend each part
- [docs/query-resolution.md](docs/query-resolution.md): how fields and the question become one plan
- [docs/retrieval.md](docs/retrieval.md): how a plan becomes registry queries, and the measurements behind name matching
- [docs/aggregation.md](docs/aggregation.md): groups, citations and exact counts for large result sets
- [docs/visualization.md](docs/visualization.md): the chart catalog, the selection rules and what a frontend receives
- [docs/api.md](docs/api.md): the complete request and response reference
- [docs/testing.md](docs/testing.md): the workflow tests and the recordings they replay
- [docs/evaluation.md](docs/evaluation.md): the interpretation eval, its results and its caveats
