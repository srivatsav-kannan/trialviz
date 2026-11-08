# API reference

This page lists every endpoint, request field and response field, with the values each one accepts, for anyone writing a client. The service also publishes an OpenAPI document at `/openapi.json`, generated from the same models that validate requests, and renders it as a reference at `/docs` with request examples, response examples taken from a saved answer, and a curl command for every endpoint. The text and examples of that reference live in `src/trialviz/openapi.py`, and its request examples are checked against the request model when the service starts, so they cannot drift from what the API accepts.

## Endpoints

| Method and path | What it does |
| --- | --- |
| `POST /v1/query` | Answers a question with a recommended visualization, alternative views of the same data, and citations to the registry records behind every number. It returns either an [answer](#answer) or a [clarification](#needs_clarification), told apart by `status`. |
| `POST /v1/interpret` | Takes the same request and returns the resolved plan, or what is needed to resolve it, without fetching any data, so a caller can see exactly how a question was understood before anything runs. |
| `POST /v1/query/stream` | Takes the same request and streams the answer as [server-sent events](#streaming-progress): progress while it is computed, then the answer itself. |
| `GET /v1/answers/{answer_id}` | Reopens a [saved answer](#saved-answers-and-csv) by its `meta.answer_id`, without recomputing it, or returns a 404 once it has expired. |
| `GET /v1/answers/{answer_id}/csv` | Downloads a saved answer's rows as CSV. |
| `GET /v1/answers/{answer_id}/request` | Returns the request an answer was given for, kept long after the answer expires, with `answer_available` saying whether the answer itself can still be reopened. |
| `POST /mcp` | The same answers as tools for AI agents, over the [Model Context Protocol](#ai-agents-over-mcp). |
| `GET /health` | Returns `{"status": "ok"}`. It is never rate limited, so a host can poll it. |
| `GET /v1/status` | Reports whether Claude or the rules read questions, how much of the language-model budget is left, and how many questions you may still ask. See [Limits](#limits). |
| `GET /`, `GET /samples` | Serve the demo UI's two pages, one for asking questions and one for browsing saved answers. `/?answer={answer_id}` reopens a saved answer and `/samples?open={name}` opens a sample; these are the links that the UI copies. Their files live under `/ui`. |
| `GET /docs` | Serves the interactive API reference. |
| `GET /v1/samples` | Lists the saved sample answers in `examples/` with each one's request, title, chart type and totals. The demo UI's samples page reads it. |
| `GET /v1/samples/{name}` | Returns one saved answer exactly as `POST /v1/query` produced it, or a 404 for an unknown name. |
| `GET /v1/samples/{name}/csv` | Downloads a sample's rows as CSV. |

## Request

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `query` | string | yes | 1 to 2,000 characters after trimming |
| `filters` | object | no | Which trials to include, described under [Filters](#filters) |
| `analysis` | object | no | What to compute, described under [Analysis](#analysis) |
| `presentation` | object | no | Display preferences, described under [Presentation](#presentation) |
| `retrieval` | object | no | `{"name_matching": "named" or "mentioned"}`, described under [Retrieval](#retrieval) |
| `inference` | `"auto"` or `"off"` | no | `off` skips reading the question. The default is `auto`. |
| `drug_name`, `condition`, `trial_phase`, `sponsor`, `country` | string | no | Shorthands for `filters.interventions`, `conditions`, `phases`, `sponsors` and `countries` |
| `start_year`, `end_year` | integer | no | Shorthands for `filters.start_year_from` and `filters.start_year_to`. Both bound the trial's start date, so `end_year` is the latest start year and has nothing to do with when a trial ends. |

The schema is closed, which means an unknown or misspelled field such as `"drug_nmae"` is rejected with a 422 instead of being ignored. Setting a shorthand together with the nested field that it stands for is rejected as ambiguous. Every structured field is optional, and whatever a caller sets is binding, as [query-resolution.md](query-resolution.md) explains.

### Filters

Values inside one field are alternatives, while separate fields must all hold. For example, `"phases": ["PHASE2", "PHASE3"]` matches trials in either phase, and adding `"countries": ["Germany"]` keeps only those with a site in Germany. An empty list is rejected, so a field with nothing to say should be left out.

| Field | Type | Accepted values |
| --- | --- | --- |
| `conditions` | string[] | Free text, as written. A name of several words is matched as a phrase, and the MeSH terms under a condition match too ([retrieval.md](retrieval.md#conditions-and-the-mesh-tree)). |
| `interventions` | string[] | Free text, as written |
| `sponsors` | string[] | Free text, matched against the lead sponsor |
| `keywords` | string[] | Free text, matched anywhere in a trial record |
| `phases` | string[] | `EARLY_PHASE1`, `PHASE1` to `PHASE4`, and `NA`. Spellings such as "Phase III", "3", "Phase 0" (meaning early phase 1) and "N/A" are accepted, and "1/2" means both phases. |
| `statuses` | string[] | Registry statuses such as `RECRUITING`, `ACTIVE_NOT_RECRUITING` and `COMPLETED`, also accepted in the form "Active, not recruiting". "active" and "ongoing" are refused, since each could mean several statuses. |
| `sponsor_classes` | string[] | `INDUSTRY`, `NIH`, `FED`, `OTHER_GOV`, `INDIV`, `NETWORK`, `OTHER`, `UNKNOWN`, `AMBIG` |
| `intervention_types` | string[] | `DRUG`, `BIOLOGICAL`, `DEVICE`, `PROCEDURE`, `RADIATION`, `BEHAVIORAL`, `GENETIC`, `DIETARY_SUPPLEMENT`, `COMBINATION_PRODUCT`, `DIAGNOSTIC_TEST`, `OTHER` |
| `study_types` | string[] | `INTERVENTIONAL`, `OBSERVATIONAL`, `EXPANDED_ACCESS` |
| `countries` | string[] | The registry's 226 country names, with aliases such as "USA", "UK", "Türkiye" and "Ivory Coast" accepted. "Korea" and "Congo" are refused as ambiguous. |
| `start_year_from` | integer | 1900 to 2100, the earliest trial start year, inclusive |
| `start_year_to` | integer | 1900 to 2100, the latest trial start year, inclusive, and no earlier than `start_year_from` |

A name that reduces to no searchable words once query syntax and parentheticals are removed, such as "???" or "(CD)", is refused with a 422, since it would otherwise drop its filter. Lists are cleaned deterministically: free-text names are deduplicated without regard to case, keeping the first spelling, and enum lists come back in their canonical order.

### Analysis

A caller can set any subset of the analysis. `kind` picks the shape of the answer, and each kind accepts only its own parameters.

| Kind | Required | Optional | Answer |
| --- | --- | --- | --- |
| `count` | | | One number |
| `distribution` | `group_by` | `series_by` | Counts per value of a dimension |
| `time_series` | | `time_field` (`start_date` or `completion_date`, default `start_date`), `granularity` (`year` or `month`, default `year`), `series_by` | Counts over time |
| `comparison` | `cohorts` (2 to 6) | `group_by` | Cohorts side by side |
| `histogram` | `measure` | `group_by` | The spread of a numeric measure, with box statistics per group |
| `scatter` | `x`, `y` | `z`, `series_by` | One point per trial, in 3D when `z` is set |
| `network` | `node_types` (a pair) | | Entities linked when they share a trial |
| `flow` | `stages` (2 or 3 dimensions) | | How trials move from one stage to the next |
| `trial_list` | | | The individual trials, with their dates |

The dimensions are `phase`, `status`, `sponsor_class`, `lead_sponsor`, `intervention`, `intervention_type`, `condition`, `country`, `investigator`, `site`, `study_type` and `start_year`, and the measures are `enrollment`, `duration_months` and `site_count`. An investigator is one of a trial's overall officials, and a site is a facility with its city and country ([aggregation.md](aggregation.md#grouping-trials)).

A few checks apply to whatever subset is sent. `group_by` and `series_by` must differ, and so must `x`, `y` and `z`. Flow stages must be different dimensions that hold one value per trial, which rules out countries, drugs, conditions, investigators and sites. A network can link a dimension to itself only when a trial can hold several values of it, so drugs can be linked to drugs while lead sponsors cannot be linked to lead sponsors. Each cohort needs a unique label and at least one filter, no two cohorts may have the same filters, and a cohort's filters apply on top of the shared `filters`.

### Presentation

| Field | Type | Notes |
| --- | --- | --- |
| `chart_type` | string | One of `stat`, `bar_chart`, `horizontal_bar_chart`, `donut_chart`, `treemap`, `choropleth_map`, `table`, `grouped_bar_chart`, `stacked_bar_chart`, `normalized_stacked_bar_chart`, `heatmap`, `time_series`, `stacked_area_chart`, `histogram`, `box_plot`, `scatter_plot`, `bubble_chart`, `scatter_plot_3d`, `network_graph`, `network_graph_3d`, `sankey`, `timeline`. It is a preference that wins whenever the chart can draw the data, and a note explains when it cannot. |
| `sort` | string | `natural`, `value_desc`, `value_asc` or `label_asc` |
| `top_n` | integer | 1 to 100 |
| `citations_per_datum` | integer | 0 to 25, default 3: how many trials per data point are cited, each with every citation that places it there |

### Retrieval

| Field | Type | Notes |
| --- | --- | --- |
| `name_matching` | string | With `named`, the default, a drug or condition must be listed among a trial's interventions or conditions for the trial to count. With `mentioned`, trials that mention it only in their descriptions count as well. Mention-only trials are retrieved and reported either way. |

Like every other slot, it can be set by the caller, inferred from the question (for example, "trials that mention Keytruda anywhere") or left to its default, and its source is reported in `provenance`. [retrieval.md](retrieval.md#named-trials-and-mentions) explains why the default is `named`.

## Answer

`POST /v1/query` returns status 200 with `"status": "resolved"` and the following fields.

| Field | Contents |
| --- | --- |
| `query` | The question as asked |
| `visualization` | The recommended chart, with `type`, `title`, `subtitle`, `reason`, `encoding`, `data`, `options` and `vega_lite` |
| `alternatives` | The other charts that can draw the same data, each with `type`, `reason`, `encoding`, `options`, `vega_lite` and `data`, which is null when it reuses the visualization's rows |
| `meta.plan`, `meta.provenance` | The resolved plan and the source of each value, described under [`resolved`](#resolved) |
| `meta.notes` | Every decision a reader might otherwise misjudge, with the codes listed below |
| `meta.chart_choice` | The recommended chart, the alternatives and the charts ruled out, each with a reason |
| `meta.cohorts` | For each cohort, its `label`, the exact `total`, how many records were `fetched`, `truncated` when the results describe only part of the matching trials, and the number of mention-only trials. An answer made of the registry's own counts, such as a count or a comparison of totals, fetches only the records it cites and is not truncated. |
| `meta.mentions` | For each cohort with mention-only trials, their exact total, whether they were counted, the IDs of the trials fetched (up to 30 when they are not counted, all of them when they are), and quoted sentences as citations |
| `meta.registry_requests` | Every ClinicalTrials.gov request made for the answer, as a full URL a reader can open, including the lookups of MeSH terms under a condition and the exact counts |
| `meta.data_version` | The registry's data timestamp at the time of answering |
| `meta.interpreter` | Which interpreter read the question: `rules`, or `claude:` followed by the model name, such as `claude:claude-opus-5-5`. It is null when the question was not read. `GET /v1/status` names the interpreter family only (`claude` or `rules`). |
| `meta.source` | Always `clinicaltrials.gov` |
| `meta.answer_id` | The id the answer is saved under, for `GET /v1/answers/{answer_id}` and its CSV, or null for a saved sample |

[visualization.md](visualization.md) explains how charts are chosen and the encoding conventions, and real responses are saved in [examples/](../examples).

### Rows by chart

Every row has an `id` that is unique within its list and stays the same for the same answer: the group's key for categories, `"row|column"` for combinations, the period (with `"|series"`) over time, the trial's NCT number for trials and points, and `"source|target"` for network edges (`"source>target"` for Sankey links). Every count-like datum other than `stat` also has `trial_count`, `nct_ids` and `citations`, whose format [aggregation.md](aggregation.md#citations) describes. `nct_ids` lists every trial behind the count, with one exception: when the counts were taken from the registry (a `counts_exact_from_registry` note, a count, or a comparison of totals), only part of the trials was fetched, so `trial_count` can be larger than `nct_ids`, which then lists the fetched trials, and a group can have a count with no IDs at all. A `stat` row carries only its count and citations, since the number can run to thousands of trials.

| Charts | `data` |
| --- | --- |
| `stat` | `[{"label", "trial_count", "citations"}]` |
| Bar, horizontal bar, donut, treemap, map, and a table over categories | One row per group, with the dimension field (such as `"country"`), `key`, the datum fields, and for countries `iso_a3` and `iso_numeric` |
| Grouped, stacked and normalized bars, and heatmaps | One row per combination, with both dimension fields and the datum fields, including combinations with no trials |
| Time series, stacked area, and bars over time | One row per period (and series), with `period` ("2019" or "2019-03"), the series field if there is one, and the datum fields |
| `histogram` | One row per bin, with `bin_start`, `bin_end`, `label`, `open_ended` (true for a last bin that also holds every larger value), and the datum fields |
| `box_plot` | One row per group, with `n`, `min`, `q1`, `median`, `q3`, `max`, `whisker_low`, `whisker_high`, `mean`, `small_sample`, `outliers`, and the datum fields |
| Scatter, bubble and 3D scatter | One row per trial, with `nct_id`, `title`, the measure fields (`enrollment`, `duration_months`, `site_count`), the color field if there is one, and `citations` |
| Network, in 2D and 3D | `{"nodes": [...], "edges": [...]}`. A node has `id`, `label`, `type`, `degree`, `community`, `x`, `y`, `x3`, `y3`, `z3` and the datum fields. An edge has `source`, `target` and the datum fields, and its citations show each trial's value for both ends, or for two drugs, the arm that gives them together. |
| `sankey` | `{"nodes": [...], "links": [...]}`. A node has `id`, `stage`, `stage_label`, `label` and the datum fields. A link has `source`, `target` and the datum fields, with citations for both of its stages. |
| `timeline` | One row per trial, with `nct_id`, `title`, `phase`, `status`, `sponsor`, `start`, `end`, `enrollment`, `url` and `citations` |

### Drawing an answer

The rows are the data, and `encoding` says which field each visual channel reads, so a frontend that does not use the Vega-Lite spec can draw every chart from these two alone. Each channel has `field`, `type` (`nominal`, `ordinal`, `quantitative` or `temporal`), `title`, and where it matters `sort` (a list giving the order of categories, or `descending`), `scale` (`symlog` for skewed measures) and `stack` (`zero` or `normalize`).

| Chart | Channels |
| --- | --- |
| `stat` | `value` |
| `bar_chart`, `time_series`, `stacked_area_chart` | `x`, `y`, and `color` for a series |
| `horizontal_bar_chart` | `y` for the category, `x` for the count |
| `donut_chart` | `theta` for the count, `color` for the category |
| `treemap` | `size` for the count, `label` for the category |
| `choropleth_map` | `geo` (the field holding the numeric ISO code), `color`, `label` |
| `grouped_bar_chart` | `x`, `y`, `color`, and `x_offset`, the field that places bars side by side within a group |
| `stacked_bar_chart`, `normalized_stacked_bar_chart` | `x`, `y` with its `stack`, `color` |
| `heatmap` | `x`, `y`, and `color` for the count |
| `histogram` | `x` (`bin_start`), `x2` (`bin_end`), `y` |
| `box_plot` | `x` for the group, and `whisker_low`, `q1`, `median`, `q3`, `whisker_high` |
| `scatter_plot` | `x`, `y`, `tooltip`, and `color` for a series |
| `bubble_chart` | as `scatter_plot`, with `size` for the third measure |
| `scatter_plot_3d` | as `scatter_plot`, with `z` for the third measure |
| `network_graph`, `network_graph_3d` | `node_id`, `node_label`, `node_size`, `node_color`, `edge_source`, `edge_target`, `edge_width`, and positions `x`, `y` (2D) or `x`, `y`, `z` read from `x3`, `y3`, `z3` (3D) |
| `sankey` | `source`, `target`, `value`, `node_label` |
| `timeline` | `x` (`start`), `x2` (`end`), `y` (`title`), `color` (`phase`) |
| `table` | None. `options.columns` lists the fields to show, in order, except for a network or a Sankey diagram, whose table shows its nodes and then its links |

`options` carries what a chart needs beyond its channels. `display_limit` is the number of bars worth showing when there are many, which the Vega-Lite spec already applies with a rank filter. `bins_precomputed` says histogram rows are already binned, and `skewed` that the bins grow geometrically. `outliers_field` names the box plot field holding each group's outlying trials. A map has `topology` (a TopoJSON URL), `feature` and `id_matches`, the row field to join to each shape's `id`. A network's `positions` says its coordinates come from a fixed force layout scaled to -1 to 1, which a frontend may use or replace, and a Sankey's `stages` lists its stages in order.

Every row shape is also published as a schema in the OpenAPI document (`StatRow`, `CategoryRow`, `MatrixRow`, `PeriodRow`, `BinRow`, `BoxRow`, `PointRow`, `TrialRow`, `GraphData` and `FlowData`), so a client generated from `/openapi.json` gets typed rows, and the contract tests check every saved answer against them.

A few details save guesswork. The Vega-Lite spec's `data.values` are the rows in the same order, keeping `id` but leaving out `citations`, `nct_ids` and `outliers` to keep the spec small, so a clicked mark's `datum.id` finds its row in `data`, where its trials and citations are. Periods are strings, `"2019"` for years and `"2019-03"` for months, and dates are `"2019-03-01"`, or `"2019-03"` when a record gives only the month. Read them as UTC: the specs use UTC time units, because a browser west of UTC would otherwise draw 2019 as the last day of 2018. The map's topology is Natural Earth's world outline at the 1:110 million scale, which leaves out a few small places such as Singapore and Hong Kong, so that those countries appear in the rows and the table but not on the map. A `needs_clarification` answer lists in `missing` the request fields that would resolve it, for example `filters.interventions` for a question about "this drug".

### Streaming progress

`POST /v1/query/stream` takes the same body as `POST /v1/query` and answers with server-sent events, so that a frontend can show what the service is doing while a long answer runs, in place of a bare spinner. While the answer is computed it sends `progress` events, each with the `stage` (`reading`, `retrieving`, `aggregating`, `counting` or `presenting`), a `message` to show, and running totals of `registry_requests`, `records_fetched` and `records_expected`, so `records_fetched / records_expected` makes a progress bar during retrieval. It ends with one `answer` event carrying exactly what `POST /v1/query` returns, a clarification included, or one `error` event carrying the error envelope with its `status` (and `retry_after` when busy). A comment line arrives every 15 seconds when nothing else does, so proxies keep the connection open. Limits apply as for `POST /v1/query`, and errors that happen before the stream starts (422, 429, a 503 when busy) are ordinary HTTP errors.

Browsers' `EventSource` only sends GET requests, so a page reads the stream with `fetch`:

```js
const response = await fetch(`${API}/v1/query/stream`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ query: "Which countries have the most recruiting melanoma trials?" }),
});
const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
let buffer = "";
for (let chunk; !(chunk = await reader.read()).done; ) {
  buffer += chunk.value;
  let end;
  while ((end = buffer.indexOf("\n\n")) >= 0) {
    const block = buffer.slice(0, end);
    buffer = buffer.slice(end + 2);
    const kind = block.match(/^event: (.*)$/m)?.[1];
    const data = block.match(/^data: (.*)$/m)?.[1];
    if (kind === "progress") showProgress(JSON.parse(data));
    if (kind === "answer") showAnswer(JSON.parse(data));
    if (kind === "error") showError(JSON.parse(data));
  }
}
```

### Saved answers and CSV

Every answer is saved under `meta.answer_id`, a hash of the request and the registry's data version, so the same request against the same data always has the same id. `GET /v1/answers/{answer_id}` reopens it without recomputing anything, which makes an answer shareable as a link, and asking the same question again returns the saved answer without calling the language model or the registry. A new registry data version, which arrives daily, gives new ids, so a saved answer is never served for data it was not computed from. The newest 1,000 answers are kept (`ANSWERS_KEPT`), in Postgres on a deployed service and in files locally, and a store that fails only means the answer is computed again. The request behind each answer, a few hundred bytes, is kept for the newest 100,000, so `GET /v1/answers/{answer_id}/request` can still say what an expired answer asked. Sending that request to `POST /v1/query` answers it again with today's registry data, which is a new answer with a new id, since the original counts cannot be recovered once the registry has moved on. An id is the first 80 bits of a SHA-256 hash, so two answers sharing one is not a practical concern.

`GET /v1/answers/{answer_id}/csv` and `GET /v1/samples/{name}/csv` return the recommended chart's rows as CSV, with every field except citations and lists such as `nct_ids` joined by semicolons. A network or Sankey diagram has two tables, so `?part=links` (the default, with both ends' labels) or `?part=nodes` picks one. Text that a spreadsheet would run as a formula is prefixed with an apostrophe, since titles and sponsor names are free text that anyone can write.

### AI agents over MCP

`/mcp` serves the same answers as tools for AI agents, over the Model Context Protocol's streamable HTTP transport, so an assistant such as Claude Desktop or Claude Code can answer clinical-trial questions with trialviz instead of from memory. In Claude Code:

```bash
claude mcp add --transport http trialviz https://trialviz.onrender.com/mcp
```

| Tool | What it does |
| --- | --- |
| `ask` | Answers a request (the same body as `POST /v1/query`) and reports progress to clients that ask for it |
| `interpret_question` | Returns the plan a request resolves to, as `POST /v1/interpret` does |
| `get_answer` | Reopens a saved answer by its `answer_id`, compact by default or complete with `full`; for an expired answer, it returns the original request so the agent can ask it again |
| `list_samples`, `get_sample` | Browse the saved sample answers |

An agent reads every token it is given, so `ask` returns a compact answer: the chart, its reason, the alternatives, the cohort totals, up to 50 rows without their trial ID lists but with one citation each, the notes, and links to the answer in the UI, its CSV and the complete response. The tools run the same code as the HTTP endpoints, so that a question asked through MCP counts against the same per-client limits and spending cap, is refused with the same message when a limit is reached, and is saved under the same `answer_id`. The endpoint is stateless and needs no sign-in, since the API is public and read-only.

## Response to `/v1/interpret`

`/v1/interpret` returns status 200 with one of two shapes, told apart by `status`. `/v1/query` returns the same `needs_clarification` shape when a question cannot be resolved.

### `resolved`

```json
{
  "status": "resolved",
  "plan": {
    "filters": { "interventions": ["Pembrolizumab"], "phases": ["PHASE3"], "...": null },
    "analysis": { "kind": "time_series", "time_field": "start_date", "granularity": "year", "series_by": null },
    "presentation": { "chart_type": null, "sort": null, "top_n": null, "citations_per_datum": 3 },
    "retrieval": { "name_matching": "named" }
  },
  "provenance": {
    "analysis.granularity": "default",
    "analysis.kind": "inferred",
    "analysis.time_field": "default",
    "filters.interventions": "user",
    "filters.phases": "user",
    "presentation.citations_per_datum": "default",
    "retrieval.name_matching": "default"
  },
  "notes": [],
  "interpreter": "rules"
}
```

This is the actual response to `{"query": "How has the number of trials for this drug changed over time?", "drug_name": "Pembrolizumab", "filters": {"phases": ["Phase III"]}}` with the rule-based interpreter. Every plan field is always present, with `null` for values nobody set, so a client never has to test for missing keys, and `analysis` carries exactly the fields of its `kind`.

`provenance` has one entry for each plan field that is not null, and no others. Its sources are `user`, `inferred`, `derived` and `default`.

Each note in `notes` has a `code`, a `message`, and the `slot` it concerns when there is one.

| Code | When it appears |
| --- | --- |
| `field_overrides_question` | The question implied a different value from a field the caller set |
| `inferred_value_rejected` | The interpreter proposed an invalid value, or one that broke a pair rule |
| `inferred_value_not_applicable` | The interpreter proposed a parameter that the final analysis kind does not use |
| `constraint_not_applied` | The question asked for something no filter can express |
| `cohort_narrowed_by_shared_filter` | A cohort and the shared filters constrain the same field |
| `counted_in_several_groups` | A trial can be in several groups, so the groups can add up to more than the number of trials |
| `computed_from_subset` | More trials matched than could be fetched, and the figures describe the most recently started ones |
| `counts_exact_from_registry` | Counts for a capped result set were taken directly from the registry |
| `values_missing` | Some trials had no value for a measure or date and were left out |
| `network_pruned` | The network was cut to its most frequent entities and strongest links |
| `mentions_reported`, `mentions_counted` | Some trials only mention the drug or condition, and the code says whether they were counted |
| `top_n_applied` | Only the largest groups are shown, as requested |
| `grid_limited` | A grid over two unordered dimensions shows only its largest rows and columns, 30 and 12 or `top_n` of each |
| `condition_includes_mesh_terms` | A condition also matched trials filed under MeSH terms that sit under it, which the note names |
| `phase_filter_includes_combined` | A phase filter includes trials listed under two phases, such as Phase 1/2 for a Phase 2 filter |
| `periods_incomplete` | The last period of a time series is still in progress, or counts trials planned to start later |
| `chart_preference_not_fitting` | The requested chart cannot draw the data, or the question names a chart the service does not draw, with the reason |
| `interpreter_fallback` | The first interpreter was unavailable and another one read the question |
| `interpreter_unavailable` | No interpreter could read the question, so only the fields were used |
| `question_not_interpreted` | `inference` was `off` |

### `needs_clarification`

```json
{
  "status": "needs_clarification",
  "reason": "ambiguous_reference",
  "message": "The question refers to \"this drug\" without naming it. Name it in the question, or set filters.interventions (or drug_name).",
  "missing": ["filters.interventions"],
  "notes": [],
  "interpreter": "rules"
}
```

| Reason | When it appears |
| --- | --- |
| `missing_parameter` | The analysis needs a value nobody supplied, and `missing` lists the slot paths that would supply it |
| `ambiguous_reference` | The question points at something that it never names, and `missing` lists the filter that would name it |
| `conflicting_analysis` | The fields and the question describe incompatible analyses |
| `out_of_scope` | Registry data cannot answer the question |

## Errors

Every error uses one envelope, `{"error": {"code", "message", "details"}}`, so a client needs a single error handler. A request that fails validation returns 422 with a path and message for each problem:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "The request does not match the schema. See details.",
    "details": [
      { "path": "filters.countries[0]", "message": "'Korea' could mean South Korea or North Korea. Name the country exactly." },
      { "path": "drug_nmae", "message": "Extra inputs are not permitted" }
    ]
  }
}
```

A shorthand field is reported under the field it stands for, so an unknown `country` comes back at `filters.countries[0]`. Every other failure uses the same envelope with an empty `details` list:

| Status | Code | Meaning |
| --- | --- | --- |
| 404 | `not_found` | There is nothing at the path |
| 404 | `sample_not_found` | No saved sample has that name |
| 404 | `answer_not_found` | No saved answer has that id, or it has expired |
| 405 | `method_not_allowed` | The path does not accept that HTTP method |
| 413 | `request_too_large` | The request body is larger than `MAX_BODY_BYTES` (16 KB by default) |
| 429 | `rate_limited` | The client, or all clients together, asked too many questions or made too many requests; `Retry-After` gives the seconds to wait |
| 500 | `internal_error` | The service failed unexpectedly |
| 502 | `registry_rejected_query` | ClinicalTrials.gov refused a query it was sent |
| 503 | `registry_unavailable` | ClinicalTrials.gov could not be reached after four attempts |
| 503 | `busy` | The service is already answering as many questions as it allows at once; `Retry-After` says when to try again |
| 504 | `answer_timeout` | The answer needed more registry time than `ANSWER_DEADLINE_SECONDS` (180 by default) allows |

## Limits

The service protects itself and its language-model budget before doing any work on a request. Questions, meaning `POST /v1/query`, its stream and `POST /v1/interpret`, are limited per client to 10 a minute and 60 an hour, and to 600 an hour across all clients, since each one can call the model and make dozens of registry requests. Every other request is limited to 120 a minute per client, except `GET /health`. Only four questions are answered at once, and a fifth gets a 503 telling it to retry shortly. Request bodies larger than 16 KB are refused while they are being read. Every limit can be changed through the settings in the README.

Each question's response carries three headers that say where the client stands against its tightest limit:

| Header | Meaning |
| --- | --- |
| `RateLimit-Limit` | Questions allowed in the window |
| `RateLimit-Remaining` | Questions left in it |
| `RateLimit-Reset` | Seconds until another question frees up |

A refused request carries `Retry-After` instead. Every question counts against the limit as soon as it arrives, including one that turns out to be invalid, so a client should validate obvious mistakes before sending. Behind a proxy, the client is identified by the address the proxy added to `X-Forwarded-For` (`TRUSTED_PROXY_HOPS`), so writing other addresses into that header does not earn a fresh quota.

The language-model budget has a total cap and an optional daily cap. Each call sets aside its worst-case cost before it is made and settles to the real cost afterwards, so concurrent calls cannot jointly cross a cap. On a deployed service the running total is kept in Postgres (`DATABASE_URL`), shared by every process and kept across restarts, and a hold left by a process that died mid-call is charged in full after ten minutes. If the database cannot be reached, the model is not called and the rule-based interpreter reads the question instead. `GET /v1/status` returns:

```json
{
  "interpreter": "claude",
  "claude_available": true,
  "reason": null,
  "budget": { "cap_usd": 5.0, "daily_cap_usd": 1.0, "spent_usd": 0.42, "spent_today_usd": 0.12, "remaining_usd": 0.88 },
  "limits": {
    "questions_per_minute": 10, "questions_per_hour": 60, "questions_remaining": 9, "resets_in_seconds": 51,
    "answers_in_progress": 0, "max_answers_in_progress": 4, "max_body_bytes": 16384
  },
  "client": "203.0.113.7"
}
```

A page on any origin can call the API from a browser. The API uses no cookies or credentials, so every response allows any origin by default (`CORS_ALLOW_ORIGINS` narrows it to a comma-separated list), a preflight is answered without counting against any limit, and `Retry-After` and the three RateLimit headers are exposed to scripts, so a frontend can show a countdown after a 429.

Every response also carries security headers: a content security policy that lets the pages load scripts only from this service and the jsDelivr CDN, and fonts only from Google Fonts, and that forbids framing, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and `Strict-Transport-Security` when served over HTTPS.
