# Architecture

This page describes how a request moves through the service, where the language model sits, and how the code is laid out. Read it first if you want to change the service or judge its design, since the other docs each zoom in on one stage.

## The pipeline

```
QueryRequest ──► Interpreter ──► Resolver ──► Plan ──► Retriever ──► Aggregator ──► Visualizer ──► Answer
                 (proposes)      (decides)             (fetches)      (groups, cites)  (recommends)
```

Every stage hands the next one a typed object that is validated where it is created, so a stage can rely on what it receives without checking it again.

| Stage | Takes | Produces | How far it is trusted |
| --- | --- | --- | --- |
| Request boundary | The JSON body | `QueryRequest` | Caller input held to a closed schema, where anything invalid is a 422 |
| Interpreter | `QueryRequest` | `Interpretation`, a proposal | Untrusted: it is shaped like a partial plan but is never typed as one |
| Resolver | The request and the proposal | `ResolvedQuery` or `ClarificationNeeded` | Produces a complete `Plan` with the source of every value, or says what is missing |
| Retriever | `Plan` | `Retrieval`: raw records per cohort, exact totals, and every request URL | Deterministic, following a fixed translation table and fetch rules |
| Aggregator | `Plan` and `Retrieval` | A dataset in one of nine shapes, with every datum cited, plus notes | Deterministic, with exact registry counts when only part of a result set was fetched |
| Visualizer | The dataset | A recommended chart, its alternatives, their specs and Vega-Lite | Deterministic rules, under which a requested chart wins whenever it can draw the data |

The stages are described in [query-resolution.md](query-resolution.md), [retrieval.md](retrieval.md), [aggregation.md](aggregation.md) and [visualization.md](visualization.md).

## Where the language model sits

The model handles the one step that needs language understanding, which is deciding that "Keytruda vs Opdivo since 2018" means two cohorts filtered by intervention with a lower bound on the start year. Everything that can be computed is computed by code, because a model that writes numbers can write wrong ones convincingly, and a reader has no way to tell.

Three properties keep that step honest. Claude answers with slot assignments such as `{"slot": "filters.phases", "values": ["PHASE3"]}`, enforced by structured output, and the list of slot names is generated from the request models so the model can only propose slots the system has. Every proposed value is then parsed by the same validators that parse caller input, since structured output guarantees the shape of an answer and says nothing about its meaning, which is how a proposed "PHASE5" or "Korea" gets dropped with a note. Finally, the model sees the caller's fields so that it can resolve phrases like "this drug", but the merge that decides each final value is code that ignores whatever the model did with those fields.

The slot-assignment format came from a failure. The first version asked Claude for one JSON object with a nullable field per slot, and the API rejected it, because two dozen nullable fields repeated inside each comparison cohort compiled into a grammar over the size limit. The list of assignments is smaller, and it has the side benefit that the model writes only what the question actually settles.

## Interchangeable interpreters

`Interpreter` is a protocol with a single method, and two implementations exist. `ClaudeInterpreter` reads the question with Claude under a hard spend cap, and `RuleBasedInterpreter` reads it with pattern matching, which is what runs without an API key, as the fallback, and as the baseline in the [evaluation](evaluation.md).

`FallbackInterpreter` chains the two. When Claude cannot answer (no key, the spend cap, a refusal, an outage or unreadable output), the rule-based interpreter takes over and the response carries a note naming the reason. The resolver cannot tell which interpreter produced a proposal, and the tests depend on that property, since every resolver test drives it with a scripted interpreter.

## Package layout

```
src/trialviz/
  vocabulary/        canonical values and their parsers (phases, statuses, countries, dimensions)
  query/
    fields.py        reusable annotated field types
    consistency.py   rules that relate two slots, and when two values mean the same thing
    filters.py       Filters and Cohort
    plan.py          the resolved plan and the analysis spec for each kind
    request.py       the public request and the table of plan sections
    resolution.py    what the resolver returns: provenance, notes and clarifications
    resolver.py      the merge
    interpreters/    the protocol, the fallback chain, the rules and Claude
  retrieval/
    essie.py         building registry query expressions and making names safe to search
    translate.py     mapping plan filters to API parameters and choosing which fields to request
    client.py        HTTP, with paging, retries, the rate limit and a version-keyed cache
    retriever.py     the fetch rules for cohorts, counts, record limits and truncation
  aggregation/
    evidence.py      reading record fields with citations, and following citation paths back
    extract.py       what each dimension and measure means for a single trial
    datasets.py      the nine dataset shapes
    build.py         building a dataset from a plan and its records
    exact.py         exact registry counts for result sets that were only partly fetched
    mentions.py      quoting where mention-only trials mention their drug or condition
  visualization/
    recommend.py     the chart catalog, fit rules, scoring and the recommendation
    spec.py          rows, encodings and the visualization spec models
    vega.py          Vega-Lite output
    layout.py        deterministic 2D and 3D network layout
    titles.py        titles and subtitles written from the plan
    geo.py           ISO codes for the registry's country names
  answer.py          the final answer model and its assembly
  pipeline.py        the sequence of stages
  llm/budget.py      the spend ledger and price table
  settings.py        configuration
  api.py             HTTP routes, error handling, and the pages at /, /samples and /docs
  openapi.py         the API reference's overview, request and response examples, and error models
  ui/                the demo UI (one page for both routes, a stylesheet, a JavaScript module)
                     and the API reference page
```

## Extending the service

Most extensions touch one table and are picked up everywhere else automatically.

A new filter is a field on `Filters`, which request validation, the plan, the resolver and the Claude schema all read. It also needs a line in `SLOT_GUIDE` in the Claude interpreter (the module refuses to import until the guide and the slot list agree) and a mapping in `retrieval/translate.py`.

A new grouping dimension is a member of `Dimension`, with its label and whether a trial can hold several values of it, plus the record fields it reads in `DIMENSION_FIELDS` in `retrieval/translate.py`. Every analysis that takes a dimension then accepts it.

A new analysis kind is a spec class in `plan.py` and an entry in `ANALYSIS_SPECS`, the table that kind derivation, parameter checks and missing-parameter clarifications all read, followed by a dataset builder in `aggregation/build.py`.

A new chart is a member of `ChartType`, a `Candidate` in `visualization/recommend.py` with its fit rule, score and reason, and its encoding in `visualization/spec.py`, after which it is recommended or offered whenever its data shape appears.
