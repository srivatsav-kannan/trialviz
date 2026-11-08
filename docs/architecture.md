# Architecture

## The pipeline

```
QueryRequest ──► Interpreter ──► Resolver ──► Plan ──► Retriever ──► Aggregator ──► Visualizer ──► Answer
                 (proposes)      (decides)             (fetches)      (groups, cites)  (recommends)
```

Each stage hands the next a typed object, and each object is validated where it is created:

| Stage | Input | Output | Trust |
| --- | --- | --- | --- |
| Request boundary | JSON body | `QueryRequest` | Caller input, held to a closed schema. Invalid input is a 422. |
| Interpreter | `QueryRequest` | `Interpretation` (a proposal) | Untrusted. Shaped like a partial plan, but never typed as one. |
| Resolver | request + proposal | `ResolvedQuery` or `ClarificationNeeded` | Produces a complete `Plan` with provenance, or says what is missing. |
| Retriever | `Plan` | `Retrieval`: raw records per cohort, exact totals, request URLs | Deterministic. Fixed translation table and fetch rules. |
| Aggregator | `Plan` + `Retrieval` | a dataset in one of nine shapes, every datum cited, plus notes | Deterministic. Exact registry counts for capped result sets. |
| Visualizer | dataset | recommended chart, alternatives, specs, Vega-Lite | Deterministic rules. A requested chart wins when it fits. |

## Where the language model sits, and why only there

The language model is used for the one step that genuinely needs language understanding: deciding that "Keytruda vs Opdivo since 2018" means two cohorts filtered by intervention, with a start-year bound. It is kept away from everything that can be computed, because a model that writes numbers can write wrong ones convincingly.

Three things keep that step honest:

1. **A closed output shape.** Claude answers with slot assignments (`{"slot": "filters.phases", "values": ["PHASE3"]}`), enforced by structured output. The list of slot names is generated from the request models, so the model cannot propose a slot the system does not have.
2. **Validation after the model.** Structured output guarantees the shape, not the meaning. Every proposed value is parsed by the same field validators that parse caller input, so "PHASE5" or "Korea" is dropped with a note.
3. **Caller precedence enforced in code.** The model sees the caller's fields so it can resolve "this drug", but the merge that decides the final value ignores what the model did with them.

A first version gave Claude one JSON object with a nullable field per slot. The API rejected it: two dozen nullable fields, repeated inside each comparison cohort, compiled into a grammar over the size limit. The assignment-list format replaced it. It is smaller, and the model writes only what the question actually settles.

## Interpreters are interchangeable

`Interpreter` is a protocol with one method. Two implementations exist:

- `ClaudeInterpreter`: reads the question with Claude under a hard spend cap.
- `RuleBasedInterpreter`: pattern matching, used without an API key, as a fallback, and as the evaluation baseline.

`FallbackInterpreter` chains them. When Claude is unavailable (no key, spend cap, refusal, outage, unreadable output), the next interpreter answers and the response carries a note naming the reason. The resolver cannot tell which interpreter produced a proposal, and the test suite depends on that. Every resolver test drives it with a scripted interpreter.

## Package layout

```
src/trialviz/
  vocabulary/        canonical values and parsers (phases, statuses, countries, dimensions)
  query/
    fields.py        reusable annotated field types
    consistency.py   rules relating two slots, and value equivalence
    filters.py       Filters and Cohort
    plan.py          the resolved plan and the per-kind analysis specs
    request.py       the public request and the plan sections table
    resolution.py    resolver outputs: provenance, notes, clarifications
    resolver.py      the merge
    interpreters/    the protocol, the fallback chain, rules, Claude
  retrieval/
    essie.py         building query expressions, and making names safe to search
    translate.py     plan filters to API parameters, and which fields to request
    client.py        HTTP: paging, retries, rate limit, version-keyed cache
    retriever.py     the fetch rules: cohorts, counts, record limits, truncation
  aggregation/
    evidence.py      reading record fields with citations, and following citation paths
    extract.py       what each dimension and measure means for one trial
    datasets.py      the nine dataset shapes
    build.py         building a dataset from a plan and its records
    exact.py         exact registry counts for capped result sets
    mentions.py      quoting where mention-only trials mention their drug or condition
  visualization/
    recommend.py     the chart catalog, fit rules, scoring, and the recommendation
    spec.py          rows, encodings and the visualization spec models
    vega.py          Vega-Lite output
    layout.py        deterministic 2D and 3D network layout
    titles.py        titles and subtitles written from the plan
    geo.py           ISO codes for the registry's country names
  answer.py          the final answer model and its assembly
  pipeline.py        sequencing the stages
  llm/budget.py      the spend ledger and price table
  settings.py        configuration
  api.py             HTTP
```

## Extending it

- **A new filter**: add a field to `Filters`. Request validation, the plan, the resolver, and the Claude schema pick it up automatically. Add a line to `SLOT_GUIDE` in the Claude interpreter (a test fails until you do) and a mapping in `retrieval/translate.py`.
- **A new grouping dimension**: add it to `Dimension` with its label and whether it is multi-valued, and list the record fields it reads in `DIMENSION_FIELDS`. Every analysis that takes a dimension accepts it.
- **A new analysis kind**: add a spec class to `plan.py` and an entry in `ANALYSIS_SPECS`. Kind derivation, parameter checks and missing-parameter clarifications all read that table. Then add a dataset builder in `aggregation/build.py`.
- **A new chart**: add it to `ChartType`, add a `Candidate` with its fit rule, score and reason in `visualization/recommend.py`, and its encoding in `visualization/spec.py`. It is then recommended or offered whenever its data shape appears.
