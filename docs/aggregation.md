# Aggregation and citations

This page explains how fetched registry records become the numbers in a chart, and how each number stays traceable to the records behind it. Aggregation is plain deterministic code in `src/trialviz/aggregation/`, and no language model is involved at this stage.

## Grouping trials

Each dimension maps a trial to zero or more groups. The definitions live in `aggregation/extract.py`, the one place where the shape of a registry record meets the analysis vocabulary.

| Dimension | Groups | Notes |
| --- | --- | --- |
| phase | One per phase combination | A trial listed as Phase 1 and Phase 2 forms one "Phase 1/2" group, as the registry labels it. Observational and expanded-access studies have no phase by design and form "No phase (observational)". |
| status, sponsor class, study type | One per registry value | |
| lead sponsor | One per sponsor name | |
| intervention | One per drug | Drugs are merged under the MeSH term tied to them, as described below. Only interventions of the plan's intervention types count, and placebo, sham, vehicle and saline arms are left out. |
| intervention type, condition, country | One per value | A trial can belong to several groups. |
| investigator | One per overall official | The trial's principal investigators, study chairs and directors, by name, with any part after a comma (", MD") dropped. Each trial writes names its own way, so the same person can appear twice under two spellings. |
| site | One per facility | Facility with its city and country, since names such as "University Hospital" repeat across cities. |
| start year | One per year | Partial dates such as "2019-03" count as the first of the month. |

A trial with no value for a dimension goes to an explicit "Not reported" group, so totals add up and missing data stays visible.

Several dimensions let a trial sit in more than one group. A trial with sites in three countries counts once for each, so the country bars can add up to more than the number of trials, and the response says so in a note whenever that can happen. Charts that show parts of one whole, such as donuts and stacked bars, are offered only when every trial sits in exactly one group.

### Drugs

The registry tags a trial's interventions with MeSH terms, but does not say which term belongs to which intervention, so the pairing follows fixed rules. A term belongs to an intervention when the intervention's name, or one of its other names, is that term with nothing added except dose, unit, route or form words, as in "pembrolizumab 200 mg IV". A name in brackets counts as well, so "Pembrolizumab (KEYTRUDA®)" is pembrolizumab. A term also belongs to an intervention when exactly one intervention and one term in the trial are left unpaired, counting interventions of every type, so a term that belongs to a biological the plan leaves out is never handed to a drug. "Bone Marrow Aspiration and Biopsy" therefore stays separate from "Biopsy", and a combination product named "Bortezomib-bendamustine-melphalan" stays separate from "Melphalan".

An intervention whose names point to several MeSH terms is listing alternatives, as in "Sintilimab" with the other names "Pembrolizumab, Nivolumab", so it is paired with none of them. Brand and code names that one trial in the answer pairs with a MeSH term are applied to the rest, so a trial that says only "Opdivo" joins Nivolumab when another trial shows the pairing. An intervention that cannot be paired keeps its own name.

Many trials name a combination as one intervention, such as "BO-112 plus pembrolizumab" or "Lenvatinib, Pembrolizumab". When a name joins parts with words like "plus", "and", "with" or "+", and at least one part pairs with a MeSH term, each part becomes its own drug, and an arm that gives the intervention links them. A name that offers a choice, such as "nivolumab or pembrolizumab", is never split, since its drugs are not given together. An independent recount of the melanoma network found that before this split, about one in five trials that give pembrolizumab were missing from its node.

When the plan filters on intervention types, only interventions of those types become groups, so a question about drug trials does not draw their biopsies and scans as drugs.

## Dataset shapes

Each analysis kind produces one dataset shape, defined in `aggregation/datasets.py`, and charts are chosen per shape ([visualization.md](visualization.md)).

| Analysis | Dataset | What it holds |
| --- | --- | --- |
| count | Scalar | The registry's exact total |
| distribution | Categorical, or Matrix when there is a series | Groups with their trials |
| comparison | Categorical for totals, Matrix when broken down by a dimension | One row per cohort |
| time series | TimeSeries | Every period from the first to the last, so gaps show as zeros |
| histogram | Distribution | Values per group, shared bins, and box statistics |
| scatter | Points | One point per trial, with up to three measures |
| network | Graph | Nodes, weighted links, clusters, and 2D and 3D layouts |
| flow | Flow | Nodes per stage and the links between consecutive stages |
| trial list | Trials | One row per trial with its dates |

### Measures

Enrollment is the count the registry reports. Duration runs from the start date to the completion date, or to the primary completion date when that is all a record has, in months. Site count is the number of listed locations. Trials without a usable value are left out, and a note gives their number.

### Histogram bins

Bins have a round width, 1, 2 or 5 times a power of ten, chosen from the spread of the middle 98 percent of values, so a few very large trials do not flatten everything else. Values beyond that range fall into a last bin that keeps the normal width and is marked open-ended, so a single 30,000-patient trial does not stretch the axis.

Enrollment is usually so skewed that equal widths would put nearly every trial in the first bin. When the largest value is more than 20 times the median, the bin edges step through 0, 10, 20, 50, 100, 200 and so on, and the chart's axis uses a symmetric log scale, which behaves like a log scale for large values but can still show trials that enrolled no one.

### Box statistics

Quartiles use the inclusive method, because the default method extrapolates beyond the data for small groups and can place a quartile outside its own whisker. Groups with fewer than 5 values are still drawn and flagged as small samples.

### Networks

Two different dimensions, such as sponsors and drugs, link when they share a trial. A dimension that can hold several values per trial can also link to itself. For drugs, that means two drugs given in the same treatment arm, so a drug and the placebo in another arm are not a combination.

An arm is also not a combination when its wording says the drugs are alternatives: its label or description mentions a choice, "either", monotherapy or a single agent, or its short label reads "X or Y", describes switching, or names a dosing interval. Each drug link cites the arm that gives both drugs, and every other link cites the values at both of its ends. A drug's node counts every trial that gives it, including trials that list no arms, while its links come only from arms.

The network keeps the 40 most frequent entities, or `presentation.top_n`, and the 300 strongest links, then leaves out entities left with no links, so a network can show fewer than 40, and reports what was pruned in a note. Clusters come from label propagation run in a fixed order, so they are the same on every run.

### Grids

A distribution over two dimensions, or a comparison broken down by one, is a grid of trial counts. When neither dimension has a natural order, the grid keeps the 30 largest rows and the 12 largest columns by number of trials, or `presentation.top_n` of each, and a note says what was kept, so ten thousand trials grouped by sponsor and country stay a chart a person can read.

### Flows

Flow stages must have one value per trial, since a trial with three countries would follow three paths at once. Every trial therefore follows exactly one path, and the number of trials is the same at every stage.

## Citations

Every data point carries `trial_count`, `nct_ids` with every contributing trial, and citations for its first `citations_per_datum` trials. Each of those trials comes with every citation that places it in the data point, so a network link cites both of its ends. This citation is from the Keytruda vs Opdivo example:

```json
{
  "nct_id": "NCT05204290",
  "field": "protocolSection.designModule.phases",
  "excerpt": "[\"EARLY_PHASE1\"]",
  "kind": "value",
  "url": "https://clinicaltrials.gov/study/NCT05204290"
}
```

`field` is the path to the value in the registry's record for that trial, and `excerpt` is the exact value found there, written as JSON when it is not text. A citation of kind `quote` holds a passage from inside a longer text field instead. The cited field always explains why the trial belongs to that data point: its phases for a phase bar, the matching entry in its locations for a country bar, the treatment arm for a drug combination, and the start and completion dates for a duration.

A count cites every active filter for each trial it cites. A drug or condition is cited only where the trial's record actually names it, or names the MeSH term the registry expanded it to, which is the term at least half of the counted trials share. When neither appears, that filter is left uncited for the trial, because any other value would cite the wrong thing.

Every workflow test follows every citation in the answer back into the raw record and checks that the excerpt is exactly there ([testing.md](testing.md)).

## Exact counts for large result sets

When more trials match than the record cap, which is 10,000 by default, only a subset is fetched, and counts taken from it are estimates. For dimensions the registry can filter on directly (phase, status, sponsor class, study type, intervention type, country and start year), each group is instead counted by the registry, using the cohort's query plus one clause for the group. Free-text dimensions such as sponsors, drugs, conditions, investigators and sites cannot be counted this way, so their charts stay estimates over the subset.

The clause matches the group exactly as aggregation defines it. "Phase 2" becomes `AREA[Phase]PHASE2 AND NOT AREA[Phase](EARLY_PHASE1 OR PHASE1 OR PHASE3 OR PHASE4 OR NA)`, which leaves out Phase 1/2 trials, and a missing phase is split by study type into "Not reported" for interventional trials and "No phase (observational)" for the rest. Of the 3,391 breast cancer trials with no phase, 1 is interventional and 3,390 are not, and the two counts add up exactly.

Every known value of a dimension is counted, including values that never appear in the subset, so the 13 per-status counts for breast cancer add up to the registry's total of 16,344. A capped time series starts at the earliest year among all matching trials, found with one sorted request, so the subset's own range does not cut it short.

An answer makes at most 30 count requests. When a chart would need more, or when any of its groups cannot be counted exactly, all of its counts stay estimates, because a chart that mixed exact and estimated bars would mislead. The response then says the figures describe the most recently started trials, and for a trend over start dates, that earlier years are undercounted.

## Mentions

Trials that mention a drug or condition only in their descriptions are retrieved separately ([retrieval.md](retrieval.md)). For each cohort, `meta.mentions` gives their exact total, the IDs of the trials fetched (up to 30 when they are not counted, all of them when they are) and quotes, each being the sentence that contains the term, taken from the title, summary, keywords, or intervention and arm descriptions.

The term a trial mentions is not always the one that was asked about, since a search for Keytruda also matches text that says "pembrolizumab". The terms to quote are learned from the cohort's named trials: a MeSH term most of them share is what the registry expanded the name to.

## Notes on periods and phases

When a time series ends in the year the data was fetched or later, a note says that the current year is still in progress and that later years count trials planned to start then, so a drop at the end of a trend is not read as a decline. A phase filter follows the registry's meaning, so a "Phase 2" filter includes Phase 1/2 and Phase 2/3 trials even though the chart's "Phase 2" group does not, and a note says so whenever such a filter applies.
