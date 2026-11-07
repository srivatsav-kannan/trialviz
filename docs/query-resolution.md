# Query resolution

How a question and a set of optional structured fields become one plan.

## The problem

A caller can describe what they want in two ways: in the question ("phase 3 trials of Keytruda since 2018") or in structured fields (`"phases": ["PHASE3"]`). Some callers will use only the question, some only fields, most a mix. The design should support all of these without assigning either input a fixed set of slots, which would mean refusing a perfectly clear question because a field "belongs" to the structured side.

## The model

Both inputs fill the **same plan**, slot by slot. A slot is one field of the plan, named by its path: `filters.phases`, `analysis.group_by`, `presentation.top_n`. Each slot gets its value from exactly one source, in strict priority order:

| Priority | Source | Meaning |
| --- | --- | --- |
| 1 | `user` | Set explicitly in the request. Always wins. |
| 2 | `inferred` | Read from the question by the interpreter. |
| 3 | `derived` | Implied by other slots: `analysis.granularity` alone implies a time series. |
| 4 | `default` | A documented default, such as yearly granularity. |

The response includes the source of every slot (`provenance`), so a frontend can show "Phase 3 (from your filter)" next to "since 2018 (from your question)".

### Worked example

```json
{
  "query": "How has the number of trials for this drug changed per year since 2015?",
  "drug_name": "Pembrolizumab"
}
```

| Slot | Value | Source |
| --- | --- | --- |
| `filters.interventions` | Pembrolizumab | `user` (via the `drug_name` shorthand) |
| `filters.start_year_from` | 2015 | `inferred` |
| `analysis.kind` | time_series | `inferred` |
| `analysis.time_field` | start_date | `default` |
| `analysis.granularity` | year | `inferred` ("per year") |
| `presentation.citations_per_datum` | 3 | `default` |

The interpreter used `drug_name` to understand "this drug", but the value in the plan comes from the field itself.

## Rules

**The merge is code.** The interpreter sees the caller's fields as context, and it is told to report what the question says even when that contradicts a field. It is never trusted to respect the fields. After it answers, the resolver applies the priority order itself.

**Conflicts become notes.** If the question says Phase 3 and the request sets Phase 2, the plan uses Phase 2 and the response says: "The question suggests Phase 3 for filters.phases, but the request sets Phase 2, so the request's value was used." Overriding with a note is better than stopping to ask, because the caller stated the field on purpose. Agreement in a different spelling or order ("Phase III, 2" against "PHASE2, PHASE3") is not a conflict. Both sides are compared after parsing, ignoring order and case.

**Interpreter values are validated like caller values.** Every proposed value goes through the same field parser as caller input. The difference is what happens on failure. A bad caller value is a 422, because the caller can fix it. A bad interpreter value is dropped with a note, because one wrong guess should not cost the rest of a good reading.

**Pair rules apply across sources.** Some slots are only valid together: a start-year range cannot run backwards, and a grouping cannot equal its series. These rules are defined once (`query/consistency.py`) and applied both at the request boundary and during resolution. When a pair breaks during resolution, the inferred half is dropped. A pair made only of caller values cannot break there, because the request boundary already refused it.

**The analysis kind is settled last.** The kind is taken from the caller if set, otherwise from the question. If the question's kind contradicts the caller's parameters (the caller set `group_by` and the question reads as a time series), the response asks rather than guessing. If nobody set a kind, it is derived only when the parameters point to exactly one kind. `group_by` alone implies a distribution, because a comparison would also need cohorts. `series_by` alone implies nothing, since three kinds use it optionally. Inferred parameters that do not belong to the final kind are dropped with a note.

**Missing pieces become clarifications.** A distribution with nothing to group by, a comparison with one cohort, or a question that points at "this drug" without naming one produces a `needs_clarification` response naming the slots that would resolve it.

**What the plan cannot express is reported.** If the question asks for something no slot can hold (an enrollment threshold, an exclusion, two drugs required together), the interpreter lists it, and the response notes that the results are not limited by it. The alternative, quietly answering a broader question, is the failure this design exists to prevent.

## Turning inference off

`"inference": "off"` skips the interpreter entirely. The plan comes from fields, derivation and defaults alone, which makes the request deterministic and free. The response notes that the question was not read.

The question is still read whenever inference is on, even if the caller's fields look complete. Fields that settle the analysis say nothing about filters the question might add ("phase 3 trials by country"), so skipping the interpreter on that basis would drop them silently.

## Looking ahead

The same pattern will extend to later stages. When a model helps choose presentation (chart type, title, sort), it will fill the `presentation` section under the same priority rules, and code will reject choices that do not fit the data, such as a line chart over categories with no order. Numbers will still come only from aggregation code.
