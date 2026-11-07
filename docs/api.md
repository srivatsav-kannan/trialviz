# API reference

The service publishes an OpenAPI document at `/openapi.json` and interactive docs at `/docs`, both generated from the same Pydantic models that validate requests. This page is the readable version.

## `POST /v1/interpret`

Reads a question and returns the resolved plan, or what is needed to resolve it. No trial data is fetched. It shows exactly how a question was understood before anything runs.

## Request

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `query` | string | yes | 1 to 2000 characters after trimming |
| `filters` | object | no | Which trials. See [Filters](#filters). |
| `analysis` | object | no | What to compute. See [Analysis](#analysis). |
| `presentation` | object | no | Display preferences. See [Presentation](#presentation). |
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

## Response

Status 200 with one of two shapes, discriminated by `status`.

### `resolved`

```json
{
  "status": "resolved",
  "plan": {
    "filters": { "interventions": ["Pembrolizumab"], "phases": ["PHASE3"], "...": null },
    "analysis": { "kind": "time_series", "time_field": "start_date", "granularity": "year", "series_by": null },
    "presentation": { "chart_type": null, "sort": null, "top_n": null, "citations_per_datum": 3 }
  },
  "provenance": {
    "analysis.granularity": "default",
    "analysis.kind": "inferred",
    "analysis.time_field": "default",
    "filters.interventions": "user",
    "filters.phases": "user",
    "presentation.citations_per_datum": "default"
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

## `GET /health`

Returns `{"status": "ok"}`.
