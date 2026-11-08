# Aggregation and citations

How fetched records become chart data, with every number traceable to the records behind it. Aggregation is deterministic code. No model is involved.

## Groups

Each dimension maps a trial to zero or more groups (`aggregation/extract.py`). The definitions are the single place where the registry's record shape meets the analysis vocabulary:

| Dimension | Groups | Notes |
| --- | --- | --- |
| phase | One per phase combination | A trial listed as Phase 1 and Phase 2 is one "Phase 1/2" group, the way the registry labels it, not counted once under each phase. |
| status, sponsor class, study type | One per registry value | |
| lead sponsor | One per sponsor name | |
| intervention | One per drug | Identified by MeSH term when the registry provides one, so "Keytruda", "MK-3475" and "pembrolizumab 200 mg" are one drug. Placebo, sham and vehicle are left out. |
| intervention type, condition, country | One per value | A trial can be in several groups. |
| start year | One per year | Partial dates ("2019-03") count as the first of the month. |

A trial with no value for a dimension goes to an explicit "Not reported" group, so totals add up and missing data stays visible.

**Trials in several groups.** A trial with sites in three countries counts once for each, so country bars can add up to more than the number of trials. Whenever that can happen, the response says so in a note. Charts that show parts of one whole (donut, stacked bars, stacked area) are only offered when every trial sits in exactly one group.

## Datasets

Each analysis kind produces one dataset shape (`aggregation/datasets.py`), and charts are chosen per shape:

| Analysis | Dataset | What it holds |
| --- | --- | --- |
| count | Scalar | The registry's exact total |
| distribution | Categorical, or Matrix with a series | Groups with their trials |
| comparison | Categorical (totals) or Matrix (by a dimension) | One row per cohort |
| time series | TimeSeries | Every period from the first to the last, so gaps show as zeros |
| histogram | Distribution | Values per group, shared bins, and box statistics |
| scatter | Points | One point per trial, with up to three measures |
| network | Graph | Nodes, weighted edges, clusters, and 2D and 3D layout |
| flow | Flow | Nodes per stage and the links between consecutive stages |
| trial list | Trials | One row per trial with its dates |

Some details:

- **Measures.** Enrollment is the registry's count. Duration is the start date to the completion date (or the primary completion date), in months. Site count is the number of listed locations. Trials without a usable value are left out, and a note gives the number.
- **Histogram bins** have a "nice" width (1, 2 or 5 times a power of ten), chosen by the Freedman-Diaconis rule on the 1st to 99th percentiles, so a few very large trials do not flatten everything else. Values beyond that range fall in the last bin, which keeps the normal width and is marked `open_ended`, so one 30,000-patient trial does not stretch the axis.
- **Skewed measures.** When the largest value is more than 20 times the median, as with enrollment almost always, equal-width bins put nearly every trial in the first one. The bins then step 0, 10, 20, 50, 100, 200 and so on, and the dataset is flagged `skewed`, which puts the chart's axis on a symmetric log scale (see [visualization.md](visualization.md)). The first version used a plain log scale, and the box plot silently drew nothing: a log axis has no place for the trials that report an enrollment of zero.
- **Box statistics** use inclusive quartiles. The default exclusive method extrapolates past the data for small groups, and the box plot workflow caught it putting a quartile outside its own whisker. Groups with fewer than 5 values are still drawn and flagged as small samples.
- **Networks.** Two different dimensions (sponsors and drugs) link when they share a trial. The same multi-valued dimension links to itself by co-occurrence. For drugs, that means two drugs given in the same treatment arm, so a drug and a placebo comparator in different arms are not a combination. Each drug edge cites the arm, and each sponsor-drug edge cites both the trial's sponsor and its drug. The network keeps the most frequent entities (40 by default, or `presentation.top_n`) and the 300 strongest links. Isolated entities are left out, and a note reports what was pruned. Clusters come from label propagation in a fixed order, so they are reproducible.
- **Flows** need single-valued stages, since a trial with three countries would follow three paths at once. Every trial follows exactly one path, so flow is conserved at every stage, which a workflow test checks.

## Citations

Every data point carries `trial_count`, `nct_ids` (every contributing trial) and the citations for its first `citations_per_datum` trials, every citation that places each of those trials there (both ends of a network link, both values of a grid cell). A citation is checkable:

A real one, from the Keytruda vs Opdivo example:

```json
{
  "nct_id": "NCT05204290",
  "field": "protocolSection.designModule.phases",
  "excerpt": "[\"EARLY_PHASE1\"]",
  "kind": "value",
  "url": "https://clinicaltrials.gov/study/NCT05204290"
}
```

`field` is a JSON path into the registry's response for that trial, and `excerpt` is the exact value there, as JSON when it is not a string. A `quote` citation's excerpt is a passage within a longer text field instead. Each citation shows why the trial is in that particular data point: its phases for a phase bar, `locations[3].country` for a country bar, the treatment arm for a drug combination edge, the start and completion dates for a duration. A count cites the field behind each active filter.

Every workflow test follows every citation in the answer back into the raw record it cites and checks that the excerpt is exactly there.

## Exact counts for large result sets

When more trials match than the record cap (10,000 by default), only a subset is fetched, and counts computed from it are estimates. For dimensions the registry can filter on directly (phase, status, sponsor class, study type, intervention type, country, start year), each group is instead counted by the registry: the cohort's query plus one clause for the group (`aggregation/exact.py`).

- Groups are matched exactly as aggregation defines them. "Phase 2" means `AREA[Phase]PHASE2 AND NOT AREA[Phase](EARLY_PHASE1 OR PHASE1 OR PHASE3 OR PHASE4 OR NA)`, so it excludes Phase 1/2 trials. "Not reported" uses the registry's `MISSING` operator.
- For dimensions with a known set of values, every value is counted, not just those seen in the subset. The first version missed 32 breast cancer trials whose rare expanded-access statuses never appeared in the 10,000 most recent trials. With this rule, the per-status counts sum to exactly the registry's 16,344.
- A capped time series starts at the registry's earliest year, found with one sorted request, not at the subset's.
- At most 30 count requests are made per answer. If a chart would need more, or any group cannot be counted exactly, its counts stay estimates. Mixing exact and estimated bars would mislead. The response then says the figures describe the most recently started trials, and for a trend over start dates, that earlier years are undercounted.

## Mentions

Trials that mention a drug or condition only in descriptions are retrieved as their own tier (see [retrieval.md](retrieval.md)). `meta.mentions` lists them per cohort with their total, trial IDs, and quotes: the sentence that contains the term, from the title, summary, keywords, or intervention and arm descriptions. The term a trial mentions is not always the one asked about, since a Keytruda search matches text that says "pembrolizumab". Synonyms are learned from the named trials of the same cohort: a MeSH term most of them share is what the registry expanded the name to.
