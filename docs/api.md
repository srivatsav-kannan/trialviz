# API reference

The service publishes an OpenAPI document at `/openapi.json` and interactive docs at `/docs`, both generated from the same Pydantic models that validate requests. This page is the readable version.

## `POST /v1/query`

Answers a question with a recommended visualization, alternative views of the same data, and citations to the registry records behind every number. It takes the [request](#request) described below and returns either an [answer](#answer) or a [clarification](#needs_clarification), told apart by `status`.

## `POST /v1/interpret`

Takes the same request and returns the resolved plan, or what is needed to resolve it, without fetching any data. It shows exactly how a question was understood before anything runs.

## Request

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `query` | string | yes | 1 to 2000 characters after trimming |
| `filters` | object | no | Which trials. See [Filters](#filters). |
| `analysis` | object | no | What to compute. See [Analysis](#analysis). |
| `presentation` | object | no | Display preferences. See [Presentation](#presentation). |
| `retrieval` | object | no | `{"name_matching": "named" or "mentioned"}`. See [Retrieval](#retrieval). |
| `inference` | `"auto"` or `"off"` | no | `off` skips reading the question. Default `auto`. |
| `drug_name`, `condition`, `trial_phase`, `sponsor`, `country` | string | no | Shorthands for `filters.interventions`, `conditions`, `phases`, `sponsors`, `countries` |
| `start_year`, `end_year` | integer | no | Shorthands for `filters.start_year_from` and `filters.start_year_to`. `end_year` bounds the start date. It is not an end date. |

The schema is closed. An unknown or misspelled field (`"drug_nmae"`) is rejected, not ignored. Setting a shorthand and its nested field together is rejected as ambiguous.

### Filters

Values inside one field are alternatives (any of). Separate fields must all hold (all of). Omit a field rather than sending an empty list. An empty list is rejected.

| Field | Type | Accepted values |
| --- | --- | --- |
| `conditions` | string[] | Free text, as written |
| `interventions` | string[] | Free text, as written |
| `sponsors` | string[] | Free text, as written |
| `keywords` | string[] | Free text, matched anywhere in a trial record |
| `phases` | string[] | `EARLY_PHASE1`, `PHASE1` to `PHASE4`, `NA`. Also "Phase III", "3", "Phase 0" (= early phase 1), "N/A". "1/2" means both phases. |
| `statuses` | string[] | Registry statuses such as `RECRUITING`, `ACTIVE_NOT_RECRUITING`, `COMPLETED`. "Active, not recruiting" style spellings are accepted. "active" and "ongoing" are refused, since they mean several statuses. |
| `sponsor_classes` | string[] | `INDUSTRY`, `NIH`, `FED`, `OTHER_GOV`, `INDIV`, `NETWORK`, `OTHER`, `UNKNOWN`, `AMBIG` |
| `intervention_types` | string[] | `DRUG`, `BIOLOGICAL`, `DEVICE`, `PROCEDURE`, `RADIATION`, `BEHAVIORAL`, `GENETIC`, `DIETARY_SUPPLEMENT`, `COMBINATION_PRODUCT`, `DIAGNOSTIC_TEST`, `OTHER` |
| `study_types` | string[] | `INTERVENTIONAL`, `OBSERVATIONAL`, `EXPANDED_ACCESS` |
| `countries` | string[] | The registry's 226 country names. Aliases such as "USA", "UK", "Türkiye", "Ivory Coast" are accepted. "Korea" and "Congo" are refused as ambiguous. |
| `start_year_from` | integer | 1900 to 2100, inclusive bound on the trial start year |
| `start_year_to` | integer | 1900 to 2100, inclusive, not earlier than `start_year_from` |

Lists are cleaned deterministically: free-text names are deduplicated case-insensitively (first spelling kept), and enum lists come back in their canonical order.

### Analysis

Any subset. `kind` picks the shape of the answer, and each kind accepts only its own parameters.

| Kind | Required | Optional | Answer |
| --- | --- | --- | --- |
| `count` | | | One number |
| `distribution` | `group_by` | `series_by` | Counts per value of a dimension |
| `time_series` | | `time_field` (`start_date`), `granularity` (`year`), `series_by` | Counts over time |
| `comparison` | `cohorts` (2 to 6) | `group_by` | Cohorts side by side |
| `histogram` | `measure` | | Spread of a numeric measure |
| `scatter` | `x`, `y` | `series_by` | One point per trial |
| `network` | `node_types` (a pair) | | Entities linked when they share a trial |

Dimensions: `phase`, `status`, `sponsor_class`, `lead_sponsor`, `intervention`, `intervention_type`, `condition`, `country`, `study_type`, `start_year`. Measures: `enrollment`, `duration_months`, `site_count`.

Checks applied to any subset: `group_by` and `series_by` must differ, `x` and `y` must differ, and a network can only link a dimension to itself if a trial can hold several values of it (drugs can co-occur in one trial, lead sponsors cannot). Each cohort needs a unique label and at least one filter, and no two cohorts may have the same filters. A cohort's filters apply on top of the shared `filters`.

### Presentation

| Field | Type | Notes |
| --- | --- | --- |
| `chart_type` | string | `stat`, `bar_chart`, `grouped_bar_chart`, `stacked_bar_chart`, `time_series`, `histogram`, `scatter_plot`, `network_graph`, `table` |
| `sort` | string | `natural`, `value_desc`, `value_asc`, `label_asc` |
| `top_n` | integer | 1 to 100 |
| `citations_per_datum` | integer | 0 to 25, default 3 |

### Retrieval

| Field | Type | Notes |
| --- | --- | --- |
| `name_matching` | string | `named` (default): a drug or condition must be listed among a trial's interventions or conditions for the trial to count. `mentioned`: trials that mention it only in descriptions count too. Mention-only trials are retrieved and reported either way. |

Like every other slot, it can be set by the caller, inferred from the question ("trials that mention Keytruda anywhere"), or defaulted, and its source is reported in `provenance`.

## Answer

`POST /v1/query` returns status 200 with `"status": "resolved"`:

| Field | Contents |
| --- | --- |
| `query` | The question as asked |
| `visualization` | The recommended chart: `type`, `title`, `subtitle`, `reason`, `encoding`, `data`, `options`, `vega_lite` |
| `alternatives` | Other charts that draw the same data, each with `type`, `reason`, `encoding`, `options`, `vega_lite`, and `data` (null when it reuses the visualization's rows) |
| `meta.plan`, `meta.provenance` | The resolved plan and the source of each value, as described below |
| `meta.notes` | Every decision a reader might otherwise misjudge (codes listed below) |
| `meta.chart_choice` | The recommended chart, the alternatives and the charts ruled out, each with a reason |
| `meta.cohorts` | Per cohort: the exact total, how many records were fetched, whether the fetch was capped, and the number of mention-only trials |
| `meta.mentions` | Trials that only mention the drug or condition: total, trial IDs, and quoted sentences |
| `meta.registry_requests` | Every ClinicalTrials.gov request behind the numbers |
| `meta.data_version` | The registry's data timestamp at the time of answering |

How charts are chosen, and the full encoding conventions, are in [visualization.md](visualization.md). Real responses are in [examples/](../examples).

### Rows by chart

Every count-like datum has `trial_count`, `nct_ids` (every contributing trial) and `citations` (see [aggregation.md](aggregation.md#citations)).

| Charts | `data` |
| --- | --- |
| `stat` | `[{"label", "trial_count", "citations"}]` |
| bar, horizontal bar, donut, treemap, map, table over categories | One row per group: the dimension field (such as `"country"`), `key`, the datum fields, and for countries `iso_a3` and `iso_numeric` |
| grouped, stacked, normalized bars, heatmap | One row per combination: both dimension fields and the datum fields, including zero cells |
| time series, stacked area, bars over time | One row per period (and series): `period` ("2019" or "2019-03"), the series field if any, and the datum fields |
| `histogram` | One row per bin: `bin_start`, `bin_end`, `label`, and the datum fields |
| `box_plot` | One row per group: `n`, `min`, `q1`, `median`, `q3`, `max`, `whisker_low`, `whisker_high`, `mean`, `small_sample`, `outliers`, and the datum fields |
| scatter, bubble, 3D scatter | One row per trial: `nct_id`, `title`, the measure fields (`enrollment`, `duration_months`, `site_count`), the color field if any, and `citations` |
| network (2D and 3D) | `{"nodes": [...], "edges": [...]}`. A node has `id`, `label`, `type`, `degree`, `community`, `x`, `y`, `x3`, `y3`, `z3` and the datum fields. An edge has `source`, `target` and the datum fields. |
| `sankey` | `{"nodes": [...], "links": [...]}`. A node has `id`, `stage`, `stage_label`, `label` and the datum fields. A link has `source`, `target` and the datum fields. |
| `timeline` | One row per trial: `nct_id`, `title`, `phase`, `status`, `sponsor`, `start`, `end`, `enrollment`, `url`, `citations` |

## Response to `/v1/interpret`

Status 200 with one of two shapes, discriminated by `status`.

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

This is the actual response to `{"query": "How has the number of trials for this drug changed over time?", "drug_name": "Pembrolizumab", "filters": {"phases": ["Phase III"]}}` with the rule-based interpreter. Every plan field is always present, with `null` for unset values, so a client never has to test for missing keys. `analysis` carries exactly the fields of its `kind`.

`provenance` has one entry per non-null plan field, and no others. Sources are `user`, `inferred`, `derived` and `default`.

`notes` explain decisions. Each has a `code`, a `message`, and the `slot` it concerns where there is one.

| Code | When |
| --- | --- |
| `field_overrides_question` | The question implied a different value than a field the caller set |
| `inferred_value_rejected` | The interpreter proposed an invalid value, or one that broke a pair rule |
| `inferred_value_not_applicable` | The interpreter proposed a parameter the final analysis kind does not use |
| `constraint_not_applied` | The question asked for something no filter can express |
| `cohort_narrowed_by_shared_filter` | A cohort and the shared filters constrain the same field |
| `counted_in_several_groups` | A trial can be in several groups, so groups can add up to more than the trial total |
| `computed_from_subset` | More trials matched than could be fetched, and the figures describe the most recently started ones |
| `counts_exact_from_registry` | Counts for a capped result set were taken directly from the registry |
| `values_missing` | Some trials had no value for a measure or date, and were left out |
| `network_pruned` | The network was cut to its most frequent entities and strongest links |
| `mentions_reported`, `mentions_counted` | Trials that only mention the drug or condition, and whether they were counted |
| `top_n_applied` | Only the largest groups are shown, as requested |
| `chart_preference_not_fitting` | The requested chart cannot draw the data, and why |
| `interpreter_fallback` | The first interpreter was unavailable, and another read the question |
| `interpreter_unavailable` | No interpreter could read the question, so only fields were used |
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

| Reason | When |
| --- | --- |
| `missing_parameter` | The analysis needs a value nobody supplied. `missing` lists the slot paths. |
| `ambiguous_reference` | The question points at something it never names |
| `conflicting_analysis` | The fields and the question describe incompatible analyses |
| `out_of_scope` | Registry data cannot answer the question |

## Errors

A request that fails validation returns 422:

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

A failure talking to ClinicalTrials.gov uses the same envelope: 503 with `registry_unavailable` after retries are exhausted, or 502 with `registry_rejected_query` if the registry refuses a query.

## `GET /health`

Returns `{"status": "ok"}`.
