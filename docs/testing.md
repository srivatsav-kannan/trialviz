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

`tests/api/` covers the HTTP contract: both outcome shapes and the single error envelope.

### 2. Live tests (`tests/live/`, `--run-live`)

A handful of real Claude calls for things only the model can do: typing brand names as interventions, resolving "this drug" from a field, recognizing an unanswerable question. They cost about three cents each and are skipped by default.

### 3. Evaluation (`evals/interpretation/`)

The labeled-question eval measures how well each interpreter reads questions, through the full resolver. It is a measurement, not a pass/fail gate, because a language model is not deterministic. See its [README](../evals/interpretation/README.md).

### 4. Workflow tests (planned)

Once retrieval, aggregation and visualization exist, workflow tests will run complete requests through every stage against recorded ClinicalTrials.gov responses. They will check the end result, including that every citation points at a real value in a real record.

## Checking the tests themselves

A suite that cannot fail proves nothing. While building the resolver suite, three deliberate bugs were introduced one at a time: letting inferred values win the merge, skipping pair-rule enforcement, and keeping parameters that do not fit the analysis kind. Both the hand-written tests and the property test caught each one.

## Running

```bash
uv run pytest                     # everything offline
uv run pytest tests/query -q      # one section
uv run pytest --run-live          # include real API calls
uv run ruff check src tests && uv run mypy src
```
