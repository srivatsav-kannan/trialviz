# Testing

This page describes the test suite, what each part protects against, and how correctness was checked beyond it. Most of the suite consists of workflows that run a real question through the whole pipeline and check the answer against what it should be. A few guard tests cover failures that a workflow cannot reach, and three live tests check the registry itself.

```bash
uv run pytest                                    # offline, about 10 seconds once cached
uv run pytest --run-live                         # adds the live registry tests
TRIALVIZ_RECORD=1 uv run pytest tests/workflows  # re-records against Claude and the registry
```

The offline run collects 39 cases from 38 test functions, since the answer-store test runs on both stores, and `--run-live` adds 3 more. The answer-store tests start a throwaway Postgres from the `pgserver` package under Python 3.12, which uv downloads on the first run, and they are skipped, with the reason shown, where it cannot start. A test belongs here only when it checks something that reading the code would not settle: behavior across several stages, concurrency, real registry data, saved files drifting from the code, or a bug that actually happened. The interpretation eval is kept apart from the suite, because a language model's output is a measurement to report, which a suite that passes or fails on every run would flatten; it is described in [evaluation.md](evaluation.md).

## Question workflows

Each workflow in `tests/workflows/` was recorded once against the real services, capturing Claude's reading of the question and every registry response, and replays offline from that recording afterwards. Replay is strict: a registry request that was never recorded fails the test and names the request, so any change in what the pipeline sends is noticed. A recording also stores a fingerprint of Claude's prompt, and replaying it under a different prompt fails with a request to re-record, since the old reading might not be what the current prompt produces.

The assertions are about the answer. Every record that comes back must satisfy the plan: the drug is named in an intervention field, the start date falls in range, and the status, phase and country are right, which tests the query translation against how the registry really behaves. Every workflow also runs one shared check on the final answer. It follows each citation into the raw record that it cites and confirms that the excerpt is exactly there. It also checks that every data point lists each of its trials once with a count that agrees, and that each chart carries a Vega-Lite spec exactly when Vega-Lite can draw it.

| Workflow | Fails when |
| --- | --- |
| The assignment example | "This drug" becomes a second search, a stray filter creeps in, the capped subset is not the latest trials, the drug search returns trials that do not test the drug, mention-only trials overlap named ones, or the yearly counts are not exact |
| Counting mentions | Counted mentions are double counted, break the exact total, or exceed the record cap |
| Keytruda vs Opdivo since 2018 | The year is applied to one cohort instead of both, a cohort's records do not involve its drug, or the chart is not grouped bars with phases on the axis |
| A field contradicting the question | The question's phase reaches the registry instead of the caller's, a count downloads more records than it cites, or a cited trial is missing a citation for one of the filters or cites the wrong drug |
| Countries with the most recruiting melanoma trials | Non-recruiting or non-melanoma trials come back, the ranking is unsorted, or the map cannot match countries to its shapes |
| A network of sponsors and drugs | The fetch repeats a trial or stops anywhere but the record cap, a sponsor links to a sponsor, or the 3D layout is flat |
| Sponsors and investigators | The network does not link lead sponsors to principal investigators, or a link is not cited by both the investigator and the sponsor of the trials it counts |
| Enrollment against duration | Off-topic trials come back, or records lack the enrollment and dates a scatter plot needs |
| Hostile caller text | Injected syntax or a stray quote reaches the registry, or the cleaned query stops finding trials |
| Claude down | The rule-based fallback does not carry the question through, or two names widen the search when they should narrow it |
| Enrollment by phase | A box's statistics are out of order, or the histogram covers different trials than the boxes |
| A three-stage flow | Trials appear or vanish between stages, lung cancer leaves out trials filed only under narrower MeSH terms, or observational studies are labeled "Not reported" |
| Type 1 vs type 2 diabetes in 2024 | A cohort holds a trial that names only the other type |
| Keytruda vs Opdivo totals in 2024 | A cohort fetches more records than it cites, a bar differs from the registry's total, or a registry request is missing from the answer |
| A trial timeline | Trials are out of start order, or some do not match the question |
| Breast cancer by status, capped | The exact registry counts do not add up to the registry's total |
| Drug combinations | A link does not come from drugs given in the same treatment arm |
| The HTTP endpoints | `/v1/query` returns anything other than exactly the pipeline's answer with its `answer_id`, a repeated question spends anything or reaches the registry, the saved answer does not reopen identically, its CSV has the wrong rows, or the stream does not pass through every stage in order and end with the same answer and a full progress count |

## Failure workflows

These replay the same recordings under bad conditions.

| Workflow | Checks |
| --- | --- |
| Throttling and server errors mid-run | Retries leave the result identical to a clean run |
| An outage | Exhausted retries raise an error and never return a partial answer |
| The cache | A repeat run makes no registry requests, and a registry refresh invalidates every cached response |
| A long-running server | The client notices a new registry data version after its 15-minute check and drops responses cached under the old one |
| A lying interpreter | Contradictions, invented values and inverted ranges change nothing in what is sent to the registry, and each one is reported |

## Guard tests

| File | What it guards |
| --- | --- |
| `test_resolver_properties.py` | Six properties of the resolver over 1,500 generated pairings of caller fields with interpreter proposals, among them that caller fields always reach the plan unchanged, every contradiction is reported, and the same input always gives the same output |
| `test_input_hazards.py` | Hostile requests are refused with a pointer to the exact problem, every kind of failure returns the one error shape, and plausible misreadings are refused (for example, "N/A" read like "1/2", or "Korea") |
| `test_api_contract.py` | What a frontend built from the docs alone relies on, over every saved answer: each saved file still matches the response the code produces today and is served unchanged, and each chart's rows validate against the row model the OpenAPI document publishes for it, with unique ids that the Vega-Lite values carry. The rows are built as dictionaries and the models declared apart, so this is what keeps them in step |
| `test_answer_store.py` | On both stores, the second against a real throwaway Postgres: a saved answer comes back exactly from a new process, only the newest answers are kept while their requests outlive them, and a store that cannot be reached or written is a miss, never an error |
| `test_abuse_guards.py` | Over HTTP: each client's question quota with its RateLimit headers, that a forged `X-Forwarded-For` entry earns no fresh quota, the ceiling across all clients (without spending the refused client's quota), the cap on answers in progress under real concurrency, oversized bodies refused whether or not their length is declared, and an agent calling the MCP tools sharing the same client's question quota with the HTTP API and being refused with the same reason |
| `test_counting_guards.py` | The two errors the second recount of the saved answers found: a comparison counts every cell and every known sponsor class exactly, including ones its fetched subset never contained, and a bracketed brand ("Pembrolizumab (KEYTRUDA®)") or a biological left out by the type filter no longer drops a drug or misnames another |

## Live tests

The three tests in `tests/live/` run only with `--run-live`. They confirm that today's registry still behaves the way the translator assumes: synonyms expand inside the naming fields, alternatives widen a search while cohort layers narrow it, named and mention-only trials add up exactly to the default search, every filter type is accepted together, and a name cannot inject query syntax. Recordings cannot show any of this, since they only capture what the registry returned once.

## Checking beyond the suite

The service was checked in two more ways once its first five saved answers existed. Each of them was recomputed independently against the live registry, with separate code that shared nothing with the service, and every count reproduced exactly, along with 77 sampled citations. The same check asked whether the answers meant what a person would expect, and the code was reviewed separately for behavior under large questions and failures. That work led to several of the behaviors described in these docs: conditions now include the MeSH terms beneath them, names of several words are matched as phrases, procedures are kept out of drug networks, observational studies have their own phase group, large grids are capped, and every failure returns the same error shape.

The API was also checked from the outside, by a developer who had never seen the project. A separate agent was given only the README, these docs and `/openapi.json`, kept away from the source and the demo pages, and asked to build a frontend on its own origin. It built one that draws all 22 chart types, every alternative in every saved answer, clarifications, errors and rate limiting. It could only do so after putting a forwarding server in front of the API, because the API sent no CORS headers and a browser refused every response. It also found dates drawn a day or a year early west of UTC, a box plot axis labeled only to 200 while whiskers reached 1,377, a box plot's table showing histogram bins, a flat scatter carrying a depth channel, clarifications that did not name the missing field, and titles reading "breast Cancer". Beyond those, a client had to discover many details by experiment, from encoding channel names to which row a Vega-Lite mark belongs to. Each was fixed or documented under [Drawing an answer](api.md#drawing-an-answer), and `test_api_contract.py` now holds the response contract that it relied on.

When the samples grew from five to fourteen, the nine new ones were recounted the same way, by separate code against the live registry, along with 27 more citations. Seven reproduced exactly. Two did not. Breast cancer's individual and unknown sponsor classes were never counted by the registry, because a comparison only counted the cells that its fetched subset happened to contain. The melanoma drug network missed trials that name a combination as one intervention or put a brand in brackets. Both were fixed, the affected answers were regenerated, and `test_counting_guards.py` now holds both cases.

## What recording found

Recording the workflows showed that the registry's default drug search also matches description text, and that about one in ten trials that matched "Keytruda" mentioned pembrolizumab only in an arm description, as a permitted option. Drug and condition names now have to appear in the fields that name a trial's interventions or conditions ([retrieval.md](retrieval.md)). The live injection test showed that a bare uppercase `AREA` still broke a request after brackets were stripped, so names are lowercased as well. The exact-count workflow found per-status counts 32 short of the registry's total, because statuses too rare to appear in the fetched subset were never counted, and every known status is now counted. The box plot workflow caught the default quartile method placing a quartile outside its own whisker for small groups.
