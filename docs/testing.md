# Testing

Few tests, each one a failure that could actually happen. Most are workflows: a real question through the whole pipeline, checked against what the answer should be. There are no tests that restate a constant or check that a parameter was set.

```bash
uv run pytest                                    # offline, about 4 seconds
uv run pytest --run-live                         # adds real registry requests
TRIALVIZ_RECORD=1 uv run pytest tests/workflows  # re-record against Claude and the registry
uv run python -m evals.interpretation.run --interpreter claude
```

## Workflows (`tests/workflows/`)

Each question workflow is recorded once against the real services (Claude's reading of the question and the registry's responses) and replayed offline after that. The replay is strict. A request the pipeline makes that was never recorded fails the test by name, so any change to what is sent to the registry is caught. A cassette recorded under a different Claude prompt fails with a request to re-record, rather than passing on an answer the current prompt might not give.

The assertions are about the answer, not the code. Every record that comes back must satisfy the plan: the drug named in an intervention field, the start date in range, the right status, phase and country. This tests the query translation against how the registry really behaves.

| Workflow | What would have to go wrong for it to fail |
| --- | --- |
| The assignment example | "This drug" becomes a second search, a stray filter creeps in, the capped subset is not the latest trials, or the drug search returns trials that do not test the drug |
| Keytruda vs Opdivo since 2018 | The year is applied per cohort instead of shared, or a cohort's records do not involve its drug |
| A field contradicting the question | The question's phase reaches the registry instead of the caller's, or a count downloads more than it cites |
| Countries with the most recruiting melanoma trials | Non-recruiting or non-melanoma trials come back, or records lack the site countries the breakdown needs |
| A small network question | A complete fetch misses or repeats a trial, or the fields a network needs are not requested |
| Hostile caller text | Injected syntax or a stray quote reaches the registry, or the cleaned query stops finding trials |
| Claude down | The rule-based fallback does not carry the question through, or two names widen the search instead of narrowing it |

The failure workflows replay the same recordings under bad conditions:

| Workflow | Checks |
| --- | --- |
| Throttling and server errors mid-run | Retries leave the result identical to a clean run, with the expected backoff |
| An outage | Exhausted retries raise an error and never return a partial answer |
| The cache | A repeat run makes no registry requests, and a registry refresh invalidates every cached response |
| A lying interpreter | Contradictions, invented values and inverted ranges change nothing in what is sent to the registry, and each is reported |
| The spend cap reached | Questions still resolve through the fallback, Claude is never called, and nothing is charged |

## Guards (`tests/`)

Four tests guard failures the workflows cannot reach:

- `test_resolver_properties.py`: six guarantees of the resolver (caller fields win, complete provenance, every contradiction reported, determinism and more) over 1,500 generated combinations of caller fields and interpreter proposals.
- `test_input_hazards.py`: hostile requests through HTTP, each refused with a pointer to the exact problem, and the vocabulary traps where a plausible parse would query the wrong trials ("N/A" split like "1/2", "Korea", a trailing space the registry stores in a country name).
- `test_spend_cap.py`: the cap under concurrent calls, and failed calls charged by whether they could have been billed.
- `test_rules_eval_snapshot.py`: all 49 eval questions through the deterministic path (rule-based interpreter and resolver), failing if any outcome changes.

## Live (`tests/live/`, `--run-live`)

Two tests confirm today's registry still has the search behavior the translator relies on: synonyms expand inside the naming fields, alternatives widen and cohort layers narrow, every filter type is accepted together, and names cannot inject query syntax. Recordings cannot prove that, because they only show what the registry returned once.

## Evaluation (`evals/interpretation/`)

How well each interpreter reads 49 hard questions, scored through the full resolver. It is a measurement, not a gate, because a language model is not deterministic. See its [README](../evals/interpretation/README.md).

## Is the suite sharp?

Sixteen deliberate bugs were introduced one at a time, across the resolver, the translator, the client, the retriever, the rule-based interpreter, the spend cap and the vocabulary. The suite caught all sixteen. The first run caught fifteen. The miss was a spend-cap test that checked a fresh ledger instead of the one in use, so a ledger that never released reservations passed. That test was fixed.

## What testing found

The suite earns its keep by finding things:

- Recording the workflows showed the registry's default drug search matches description text. One in ten "Keytruda" trials only listed pembrolizumab as one permitted option in an arm description. Drug and condition names now have to appear in the fields that name a trial's interventions or conditions (see [retrieval.md](retrieval.md)).
- The live injection test showed that a bare uppercase `AREA` still broke a request after brackets were stripped. Names are now lowercased.
- Real records mix "2026-12" and "2026-12-01" start dates, so dates are compared at month precision.
- An earlier, larger suite caught "N/A" being split like "1/2" and cohorts that differed only by letter case.
