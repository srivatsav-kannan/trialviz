# Query resolution

This page explains how a question and a set of optional structured fields become one plan, and what the service does when the two disagree or when something is missing. It matters to anyone calling the API with a mix of fields and free text, and to anyone reading the `provenance` and `notes` in a response.

## Two ways to say the same thing

A caller can describe a request in the question (for example, "phase 3 trials of Keytruda since 2018") or in structured fields (for example, `"phases": ["PHASE3"]`). Some callers will use only the question, some only fields, and most a mix of the two. The service supports all of these by letting both inputs fill the same plan, and it avoids assigning either input a fixed set of slots, since that would mean ignoring a perfectly clear question because a field supposedly belongs to the structured side.

## One plan, filled slot by slot

A slot is one field of the plan, named by its path, such as `filters.phases`, `analysis.group_by` or `presentation.top_n`. Each slot takes its value from exactly one source, in a strict order of priority:

| Priority | Source | Meaning |
| --- | --- | --- |
| 1 | `user` | Set explicitly in the request, and always wins |
| 2 | `inferred` | Read from the question by the interpreter |
| 3 | `derived` | Implied by other slots, as when `analysis.granularity` alone implies a time series |
| 4 | `default` | A documented default, such as yearly granularity |

The response reports the source of every slot in `provenance`, so that a frontend can show, for example, "Phase 3 (from your filter)" next to "since 2018 (from your question)".

### A worked example

```json
{
  "query": "How has the number of trials for this drug changed per year since 2015?",
  "drug_name": "Pembrolizumab"
}
```

| Slot | Value | Source |
| --- | --- | --- |
| `filters.interventions` | Pembrolizumab | `user`, through the `drug_name` shorthand |
| `filters.start_year_from` | 2015 | `inferred` |
| `analysis.kind` | time_series | `inferred` |
| `analysis.time_field` | start_date | `default` |
| `analysis.granularity` | year | `inferred` from "per year" |
| `presentation.citations_per_datum` | 3 | `default` |

The interpreter used `drug_name` to understand "this drug", but the value that ends up in the plan comes from the field itself.

## How the merge works

The merge is code. The interpreter sees the caller's fields as context and is told to report what the question says even when it contradicts a field, and after it answers, the resolver applies the order of priority itself, so nothing depends on the interpreter respecting the caller.

A conflict becomes a note. For example, if the question says Phase 3 and the request sets Phase 2, the plan uses Phase 2 and the response says "The question suggests Phase 3 for filters.phases, but the request sets Phase 2, so the request's value was used." The service overrides with a note instead of stopping to ask, because a caller who sets a field has stated what they want. Two values that agree in a different spelling or order ("Phase III, 2" against "PHASE2, PHASE3") do not count as a conflict, since both sides are compared after parsing, ignoring order and case.

Values from the interpreter are validated exactly like values from the caller, and the only difference is what happens when one fails. A bad caller value is a 422, because the caller can fix it, while a bad interpreter value is dropped with a note, so that one wrong guess does not cost the rest of a good reading.

Some slots are only valid together: a range of start years cannot run backwards, a grouping cannot equal its series, and the axes of a scatter must all differ. These pair rules are defined once in `query/consistency.py` and applied both at the request boundary and during resolution. When a pair breaks during resolution, the inferred half is dropped, and a pair made only of caller values never reaches that point because the request boundary has already refused it.

## Settling the analysis

The analysis kind is settled last. It comes from the caller when set and from the question otherwise, and when the question's kind contradicts parameters the caller set (for example, the caller sets `group_by` and the question reads as a time series), the response asks for clarification instead of guessing. When nobody sets a kind, one is derived only if the parameters point to exactly one kind: `group_by` alone implies a distribution, because a comparison would also need cohorts, while `series_by` alone implies nothing, since three kinds accept it as an option. Inferred parameters that do not belong to the final kind are dropped with a note.

Missing pieces become clarifications. A distribution with nothing to group by, a comparison with a single cohort, or a question that points at "this drug" without naming one returns a `needs_clarification` response that names the slots that would resolve it.

What the plan cannot express is reported as well. When the question asks for something that no slot can hold, such as an enrollment threshold, an exclusion, or two drugs that must be given together, the interpreter lists it and the response notes that the results are not limited by it. A chart that the service does not draw ("as a spaghetti chart") is caught the same way and reported as a chart request, with the recommended chart shown instead. Quietly answering a broader question than the one asked is the failure this whole design is meant to prevent, and the [evaluation](evaluation.md) describes how this rule was found.

## Presentation slots

The question can also fill the `presentation` section under the same rules, so that, for example, "show it as a map" sets `presentation.chart_type` with the source `inferred`, and "the top 10 sponsors" sets `top_n`. A chart type is a preference: the visualizer uses it whenever that chart can draw the data and otherwise recommends another with a note explaining why, as described in [visualization.md](visualization.md). Choosing the chart and computing every number remain the work of code.

## Turning inference off

Setting `"inference": "off"` skips the interpreter entirely. The plan then comes from fields, derivation and defaults alone, which makes the request deterministic and free, and the response notes that the question was not read.

With inference on, the question is read even when the caller's fields look complete, because fields that settle the analysis say nothing about filters that the question might add (for example, "phase 3 trials by country"), and skipping the interpreter on that basis would drop those filters without a trace.
