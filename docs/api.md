# API reference

This page lists every endpoint, request field and response field, with the values each one accepts. It is meant for anyone writing a client. The service also publishes an OpenAPI document at `/openapi.json`, generated from the same Pydantic models that validate requests, and renders it as an interactive reference at `/docs` with request examples, response examples cut from a saved answer, and a curl command for every endpoint. The overview, examples and error descriptions in that reference live in `src/trialviz/openapi.py`, and its request examples are validated against the request model when the service starts.

## Endpoints

| Method and path | What it does |
| --- | --- |
| `POST /v1/query` | Answers a question with a recommended visualization, alternative views of the same data, and citations to the registry records behind every number. It returns either an [answer](#answer) or a [clarification](#needs_clarification), told apart by `status`. |
| `POST /v1/interpret` | Takes the same request and returns the resolved plan, or what is needed to resolve it, without fetching any data, so a caller can see exactly how a question was understood before anything runs. |
| `GET /health` | Returns `{"status": "ok"}`. |
| `GET /`, `GET /samples` | Serve the demo UI's two pages, asking questions and browsing saved answers. Their files live under `/ui`. |
| `GET /docs` | Serves the interactive API reference. |
| `GET /v1/samples` | Lists the saved sample answers in `examples/` with each one's request, title, chart type and totals. The demo UI's samples page reads it. |
| `GET /v1/samples/{name}` | Returns one saved answer exactly as `POST /v1/query` produced it, or a 404 for an unknown name. |

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

The schema is closed, which means an unknown or misspelled field such as `"drug_nmae"` is rejected with a 422 instead of being ignored. Setting a shorthand together with the nested field it stands for is rejected as ambiguous. Every structured field is optional, and whatever a caller sets is binding, as [query-resolution.md](query-resolution.md) explains.

### Filters

Values inside one field are alternatives, so `"phases": ["PHASE2", "PHASE3"]` matches trials in either phase, while separate fields must all hold. An empty list is rejected, so a field with nothing to say should be left out.

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

The dimensions are `phase`, `status`, `sponsor_class`, `lead_sponsor`, `intervention`, `intervention_type`, `condition`, `country`, `study_type` and `start_year`, and the measures are `enrollment`, `duration_months` and `site_count`.

A few checks apply to whatever subset is sent. `group_by` and `series_by` must differ, and so must `x`, `y` and `z`. Flow stages must be different dimensions that hold one value per trial, which rules out countries, drugs and conditions. A network can link a dimension to itself only when a trial can hold several values of it, so drugs can be linked to drugs while lead sponsors cannot be linked to lead sponsors. Each cohort needs a unique label and at least one filter, no two cohorts may have the same filters, and a cohort's filters apply on top of the shared `filters`.

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

Like every other slot, it can be set by the caller, inferred from the question ("trials that mention Keytruda anywhere") or left to its default, and its source is reported in `provenance`. [retrieval.md](retrieval.md#named-trials-and-mentions) explains why the default is `named`.

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
| `meta.cohorts` | For each cohort, its `label`, the exact `total`, how many records were `fetched`, whether results describe only part of the matching trials (`truncated`, which is false for answers built from the registry's counts, such as a count or a comparison of totals, even though they fetch only the records they cite), and the number of mention-only trials |
| `meta.mentions` | For each cohort with mention-only trials, their exact total, whether they were counted, the IDs of the trials fetched (up to 30 when they are not counted, all of them when they are), and quoted sentences as citations |
| `meta.registry_requests` | Every ClinicalTrials.gov request made for the answer, including the lookups of MeSH terms under a condition and the exact counts |
| `meta.data_version` | The registry's data timestamp at the time of answering |
| `meta.interpreter` | Which interpreter read the question, or null when the question was not read |
| `meta.source` | Always `clinicaltrials.gov` |

[visualization.md](visualization.md) explains how charts are chosen and the encoding conventions, and real responses are saved in [examples/](../examples).

### Rows by chart

Every count-like datum has `trial_count`, `nct_ids` listing every contributing trial, and `citations`, whose format [aggregation.md](aggregation.md#citations) describes.

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
| `chart_preference_not_fitting` | The requested chart cannot draw the data, with the reason |
| `interpreter_fallback` | The first interpreter was unavailable and another one read the question |
| `interpreter_unavailable` | No interpreter could read the question, so only the fields were used |
| `question_not_interpreted` | `inference` was `off` |

### `needs_clarification`

```json
{
  "status": "needs_clarification",
  "reason": "ambiguous_reference",
  "message": "The question refers to \"this drug\" without naming it. Name it in the question, or set filters.interventions (or drug_name).",
  "missing": [],
  "notes": [],
  "interpreter": "rules"
}
```

| Reason | When it appears |
| --- | --- |
| `missing_parameter` | The analysis needs a value nobody supplied, and `missing` lists the slot paths that would supply it |
| `ambiguous_reference` | The question points at something it never names |
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

Every other failure uses the same envelope with an empty `details` list:

| Status | Code | Meaning |
| --- | --- | --- |
| 404 | `not_found` | There is nothing at the path |
| 404 | `sample_not_found` | No saved sample has that name |
| 405 | `method_not_allowed` | The path does not accept that HTTP method |
| 500 | `internal_error` | The service failed unexpectedly |
| 502 | `registry_rejected_query` | ClinicalTrials.gov refused a query it was sent |
| 503 | `registry_unavailable` | ClinicalTrials.gov could not be reached after four attempts |
| 504 | `answer_timeout` | The answer needed more registry time than `ANSWER_DEADLINE_SECONDS` (180 by default) allows |
