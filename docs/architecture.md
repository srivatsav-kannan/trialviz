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

A question arrives through `POST /v1/query`, its streaming twin or the MCP tools, and all three take the same path. Before the pipeline runs, the API looks the request up among saved answers, keyed by a hash of the request and the registry's data version, and returns a saved answer without running anything ([api.md](api.md#saved-answers-and-csv)). While it runs, each stage reports its progress through a context variable that the streaming endpoint listens to, so no stage takes an extra argument for it. The stages are described in [query-resolution.md](query-resolution.md), [retrieval.md](retrieval.md), [aggregation.md](aggregation.md) and [visualization.md](visualization.md).

## Where the language model sits

The model handles the one step that needs language understanding, which is deciding, for example, that "Keytruda vs Opdivo since 2018" means two cohorts filtered by intervention with a lower bound on the start year. Everything that can be computed is computed by code, because a model that writes numbers can write wrong ones convincingly, and a reader has no way to tell.

Three things keep that step honest. Claude answers in a fixed format, a list of slot assignments such as `{"slot": "filters.phases", "values": ["PHASE3"]}`, and the slot names it may use are generated from the request models, so that it can only propose slots that the system has. Every value it proposes is then checked by the same validators that check caller input, because a fixed format guarantees the shape of an answer but nothing about its meaning. That is how a proposed value such as "PHASE5" or "Korea" is dropped with a note. And although the model sees the caller's fields, so that it can resolve phrases such as "this drug", the code that decides each final value ignores whatever the model did with them.

The format came from a failure. The first version asked Claude for one object with an optional field for every slot, and the API rejected the schema as too large, because the fields were repeated inside every comparison cohort. A list of assignments is far smaller, and the model writes only what the question actually settles.

## Interchangeable interpreters

`Interpreter` is a protocol with a single method, and two implementations exist. `ClaudeInterpreter` reads the question with Claude under a hard spend cap, and `RuleBasedInterpreter` reads it with pattern matching, which is what runs without an API key, as the fallback, and as the baseline in the [evaluation](evaluation.md).

`FallbackInterpreter` chains the two. When Claude cannot answer, for lack of a key, the spend cap, an outage, unreadable output or any unexpected error, the rule-based interpreter takes over and the answer carries a note saying why. Each Claude call reserves its worst-case cost before it is made and releases the reservation however the call ends, so a failure can neither cross the cap nor leave part of it held. The resolver cannot tell which interpreter produced a proposal, which is also what lets its tests drive it with a scripted one.

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
    references.py    questions that point at something they never name ("this drug")
    interpreters/    the protocol, the fallback chain, the rules and Claude
  retrieval/
    essie.py         building registry query expressions and making names safe to search
    translate.py     mapping plan filters to API parameters and choosing which fields to request
    client.py        HTTP, with paging, retries, the rate limit and a cache kept per data version
    retriever.py     the fetch rules for cohorts, counts, record limits, truncation and the
                     MeSH terms under a condition
  aggregation/
    evidence.py      reading record fields with citations, and following citation paths back
    extract.py       what each dimension and measure means for a single trial, and how
                     drug names are tied to MeSH terms
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
    contract.py      the row model of each chart type, published in the OpenAPI document
    geo.py           ISO codes for the registry's country names
  answer.py          the final answer model and its assembly
  pipeline.py        the sequence of stages, the answer's request log and its registry deadline
  progress.py        progress reports for the streaming endpoint, through a context variable
  store.py           saved answers keyed by request and data version, in files or Postgres
  export.py          an answer's rows as CSV
  mcp_server.py      the same answers as MCP tools for AI agents, at /mcp
  llm/budget.py      the spend caps: a file ledger for local runs, a Postgres one when deployed
  limits.py          rate limits, the cap on answers in progress, body size, security headers
  settings.py        configuration
  service.py         what every way in shares: limits, answering, saved answers and status
  api.py             HTTP routes on top of the service, the stream, error handling, and the
                     pages at /, /samples and /docs
  openapi.py         the API reference's overview, request and response examples, and error models
  ui/                the demo UI (one page for both routes, a stylesheet, and a JavaScript
                     module that streams progress, shares links and downloads CSV and JSON),
                     and the API reference page
```

## Extending the service

Most extensions touch one table and are picked up everywhere else automatically.

A new filter is a field on `Filters`, which request validation, the plan, the resolver and the Claude schema all read. It also needs a line in `SLOT_GUIDE` in the Claude interpreter (the module refuses to import until the guide and the slot list agree) and a mapping in `retrieval/translate.py`.

A new grouping dimension is a member of `Dimension`, with its label and whether a trial can hold several values of it, an extractor in `aggregation/extract.py` that reads its values from a trial record, and the record fields to fetch in `DIMENSION_FIELDS` in `retrieval/translate.py`. Every analysis that takes a dimension then accepts it, as investigators and sites showed when they were added.

A new analysis kind is a spec class in `plan.py` and an entry in `ANALYSIS_SPECS`, the table that kind derivation, parameter checks and missing-parameter clarifications all read, followed by a dataset builder in `aggregation/build.py`.

A new chart is a member of `ChartType`, a `Candidate` in `visualization/recommend.py` with its fit rule, score and reason, its encoding in `visualization/spec.py`, and its row model in `visualization/contract.py`, after which it is recommended or offered whenever its data shape appears.
