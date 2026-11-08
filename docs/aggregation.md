# Aggregation and citations

This document explains how fetched registry records become the numbers in a chart, and how each of those numbers stays traceable to the records behind it. Aggregation is plain deterministic code in `src/trialviz/aggregation/`, and no language model is involved at this stage.

## Grouping trials

Each dimension maps a trial to zero or more groups. The definitions live in `aggregation/extract.py`, which is the one place where the shape of a registry record meets the analysis vocabulary.

| Dimension | Groups | Notes |
| --- | --- | --- |
| phase | One per phase combination | A trial listed as Phase 1 and Phase 2 forms a single "Phase 1/2" group, which is how the registry labels it, instead of counting once under each phase. |
| status, sponsor class, study type | One per registry value | |
| lead sponsor | One per sponsor name | |
| intervention | One per drug | Drugs are identified by MeSH term when the registry provides one, so "Keytruda", "MK-3475" and "pembrolizumab 200 mg" become one drug. Placebo, sham, vehicle and saline control arms are left out. |
| intervention type, condition, country | One per value | A trial can belong to several groups. |
| start year | One per year | Partial dates such as "2019-03" count as the first of the month. |

A trial with no value for a dimension goes to an explicit "Not reported" group, so totals add up and missing data stays visible in the chart.

Some dimensions let a trial sit in several groups at once. A trial with sites in three countries counts once for each, which means the country bars can add up to more than the number of trials, and the response says so in a note whenever that can happen. Charts that show parts of one whole (donuts, stacked bars and stacked areas) are offered only when every trial sits in exactly one group.

## Dataset shapes

Each analysis kind produces one dataset shape, defined in `aggregation/datasets.py`, and charts are chosen per shape (see [visualization.md](visualization.md)).

| Analysis | Dataset | What it holds |
| --- | --- | --- |
| count | Scalar | The registry's exact total |
| distribution | Categorical, or Matrix when there is a series | Groups with their trials |
| comparison | Categorical for totals, Matrix when broken down by a dimension | One row per cohort |
| time series | TimeSeries | Every period from the first to the last, so gaps show as zeros |
| histogram | Distribution | Values per group, shared bins, and box statistics |
| scatter | Points | One point per trial, with up to three measures |
| network | Graph | Nodes, weighted edges, clusters, and 2D and 3D layouts |
| flow | Flow | Nodes per stage and the links between consecutive stages |
| trial list | Trials | One row per trial with its dates |

### Measures

Enrollment is the count the registry reports. Duration runs from the start date to the completion date (or the primary completion date when that is all a record has), in months, and site count is the number of listed locations. Trials without a usable value for the measure are left out, and a note gives their number.

### Histogram bins

Bins have a round width (1, 2 or 5 times a power of ten) chosen by the Freedman-Diaconis rule on the 1st to 99th percentiles, which keeps a few very large trials from flattening everything else. Values beyond that range fall into the last bin, which keeps the normal width and is marked `open_ended`, so a single 30,000-patient trial does not stretch the axis.

Enrollment is usually so skewed that equal widths would put nearly every trial in the first bin. When the largest value is more than 20 times the median, the bin edges instead step through 0, 10, 20, 50, 100, 200 and so on, and the dataset is flagged `skewed`, which puts the chart's axis on a symmetric log scale. The first version used a plain log scale, and the box plot silently drew nothing, because a log axis has no place for trials that report an enrollment of zero.

### Box statistics

Quartiles use the inclusive method. The default exclusive method extrapolates past the data for small groups, and the box plot workflow caught it placing a quartile outside its own whisker. Groups with fewer than 5 values are still drawn and flagged as small samples.

### Networks

Two different dimensions, such as sponsors and drugs, link when they share a trial. A multi-valued dimension can also link to itself by co-occurrence, which for drugs means two drugs given in the same treatment arm, so a drug and the placebo comparator in another arm do not count as a combination. Each drug edge cites the arm that gives both drugs, and each sponsor-drug edge cites both the trial's sponsor and its drug.

The network keeps the 40 most frequent entities (or `presentation.top_n`) and the 300 strongest links, leaves out entities that end up with no links, and reports what was pruned in a note. Clusters come from label propagation run in a fixed order, so they are the same on every run.

### Flows

Flow stages must have one value per trial, since a trial with three countries would follow three paths at once. Every trial therefore follows exactly one path, and the number of trials is conserved at every stage, which a workflow test checks.

## Citations

Every data point carries `trial_count`, `nct_ids` (every contributing trial), and the citations for its first `citations_per_datum` trials. Each of those trials comes with every citation that places it in the data point, so a network link cites both of its ends and a grid cell cites both of its values. This citation comes from the Keytruda vs Opdivo example:

```json
{
  "nct_id": "NCT05204290",
  "field": "protocolSection.designModule.phases",
  "excerpt": "[\"EARLY_PHASE1\"]",
  "kind": "value",
  "url": "https://clinicaltrials.gov/study/NCT05204290"
}
```

`field` is a JSON path into the registry's response for that trial, and `excerpt` is the exact value found there, encoded as JSON when it is not a string. A citation of kind `quote` holds a passage from inside a longer text field instead. The cited field always explains why the trial belongs to that particular data point: its phases for a phase bar, `locations[3].country` for a country bar, the treatment arm for a drug combination, the start and completion dates for a duration. A count cites the field behind each active filter.

Every workflow test follows every citation in the answer back into the raw record it cites and checks that the excerpt is exactly there ([testing.md](testing.md)).

## Exact counts for large result sets

When more trials match than the record cap (10,000 by default), only a subset is fetched, and counts computed from that subset are estimates. For the dimensions the registry can filter on directly (phase, status, sponsor class, study type, intervention type, country and start year), `aggregation/exact.py` instead asks the registry to count each group, using the cohort's query plus one clause for the group.

The clause matches the group exactly as aggregation defines it. "Phase 2" becomes `AREA[Phase]PHASE2 AND NOT AREA[Phase](EARLY_PHASE1 OR PHASE1 OR PHASE3 OR PHASE4 OR NA)`, which excludes Phase 1/2 trials, and "Not reported" uses the registry's `MISSING` operator. For dimensions with a known set of values every value is counted, including those that never appear in the subset. The first version missed 32 breast cancer trials whose rare expanded-access statuses did not occur among the 10,000 most recent trials, and with every value counted the per-status figures now sum to exactly the registry's 16,344. A capped time series starts at the registry's earliest year, which one sorted request finds, so it does not begin at the subset's earliest year.

An answer makes at most 30 count requests. When a chart would need more, or when any of its groups cannot be counted exactly, all of its counts stay estimates, since a chart mixing exact and estimated bars would mislead. The response then says the figures describe the most recently started trials and, for a trend over start dates, that earlier years are undercounted.

## Mentions

Trials that mention a drug or condition only in their descriptions are retrieved as a separate tier ([retrieval.md](retrieval.md)). `meta.mentions` lists them per cohort with their total, their trial IDs and quotes, where each quote is the sentence containing the term, taken from the title, summary, keywords, or intervention and arm descriptions.

The term a trial mentions is not always the one that was asked about, since a search for Keytruda also matches text that says "pembrolizumab". The synonyms to quote are learned from the named trials of the same cohort: a MeSH term that most of them share is what the registry expanded the name to.
