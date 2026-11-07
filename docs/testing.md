# Testing

## Principles

Tests exist to catch failures that could actually happen. Most cases here are adversarial: a caller with a typo, an interpreter that contradicts a field, invents a phase or proposes a range that runs backwards, a country name that is also a US state, a number inside a disease name that looks like a phase. Easy cases are kept few, so a passing suite means something.

## Layers

### 1. Section suites (`tests/<section>/`)

Each stage is tested on its own. For the query stage (`tests/query/`):

| File | What it pins down |
| --- | --- |
| `test_vocabulary.py` | Loose spellings parse to registry values. Ambiguous names ("Korea", "active") fail loudly instead of being guessed. Registry quirks such as a trailing space in a country name survive. |
| `test_request.py` | The closed schema: unknown fields, empty lists, implausible years, parameters that do not fit their kind, degenerate comparisons. The assignment's example request works verbatim. |
| `test_resolver.py` | The merge, driven by a scripted interpreter that is wrong in specific ways. Caller fields win, invalid guesses are dropped one by one, pair rules hold across sources, and kinds are derived only when the parameters single one out. |
| `test_resolver_properties.py` | Six guarantees checked over 1,500 generated pairings of caller fields and proposals, drawn from pools that mix valid values, near misses and garbage. |
| `test_rules_interpreter.py` | Phrasings that trap naive pattern matching: "after 2015" (exclusive), "type 2 diabetes" (not a phase), "not yet recruiting" (not also "recruiting"), "New Jersey" (not Jersey). |
| `test_claude_interpreter.py` | The Claude wrapper with the API faked: the exact request sent, slot conversion, the spend cap, and every failure mode turning into a fallback. Drift guards keep the output schema in step with the request models. |
| `test_budget.py` | The spend ledger under concurrency and across restarts. |

For the retrieval stage (`tests/retrieval/`), a fake registry served through `httpx.MockTransport` pages like the real API and fails on command:

| File | What it pins down |
| --- | --- |
| `test_translate.py` | The exact parameters each filter becomes, injection-proof names, cohort layering, determinism, and which fields each analysis requests. |
| `test_client.py` | Paging that repeats the first page's query, retries with backoff for 429, 5xx and dropped connections, immediate failure on a 400, request spacing, and a cache that misses when the registry's data version changes. |
| `test_retriever.py` | The fetch rules: one query per cohort with shared filters, counts fetching only what they cite, no fetch for zero matches, and a sorted, flagged subset above the cap. |

`tests/api/` covers the HTTP contract: both outcome shapes and the single error envelope.

### 2. Live tests (`tests/live/`, `--run-live`)

A handful of real Claude calls for things only the model can do: typing brand names as interventions, resolving "this drug" from a field, recognizing an unanswerable question. They cost about three cents each and are skipped by default.

The registry tests confirm the API reads the translator's parameters the way the translator assumes: synonyms expand, every filter type is accepted together, alternatives widen and cohort layers narrow, a full fetch returns every match exactly once, and injected syntax is searched as words. That last test found a real hole. The offline suite stripped brackets from names, but the registry still rejected a bare uppercase `AREA`. Names are now lowercased, which disarms every operator keyword.

### 3. Evaluation (`evals/interpretation/`)

The labeled-question eval measures how well each interpreter reads questions, through the full resolver. It is a measurement, not a pass/fail gate, because a language model is not deterministic. See its [README](../evals/interpretation/README.md).

### 4. Workflow tests (planned)

Once retrieval, aggregation and visualization exist, workflow tests will run complete requests through every stage against recorded ClinicalTrials.gov responses. They will check the end result, including that every citation points at a real value in a real record.

## Checking the tests themselves

A suite that cannot fail proves nothing. While building the resolver suite, three deliberate bugs were introduced one at a time: letting inferred values win the merge, skipping pair-rule enforcement, and keeping parameters that do not fit the analysis kind. Both the hand-written tests and the property test caught each one. The retrieval suite caught all five of its deliberate bugs: dropping the sort on a truncated fetch, dropping the shared filters from a cohort query, retrying a 400, leaving the data version out of the cache key, and skipping name sanitizing.

## Running

```bash
uv run pytest                     # everything offline
uv run pytest tests/query -q      # one section
uv run pytest --run-live          # include real API calls
uv run ruff check src tests && uv run mypy src
```
