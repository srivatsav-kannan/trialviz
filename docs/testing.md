# Testing

This document describes the test suite, what each part protects against, and what the tests have found so far. Most of the suite consists of workflows that run a real question through the whole pipeline and check the answer against what it should be, supported by a handful of guard tests for failures a workflow cannot reach and three live tests against the registry itself.

```bash
uv run pytest                                    # offline, about 4 seconds
uv run pytest --run-live                         # adds the live registry tests
TRIALVIZ_RECORD=1 uv run pytest tests/workflows  # re-records against Claude and the registry
```

The offline run collects 35 cases from 32 test functions (the spend-cap failure test is parametrized four ways), and `--run-live` adds 3 more. The interpretation eval is separate from the suite, since a language model's output is a measurement to report and cannot be a pass or fail gate; it is described in [evaluation.md](evaluation.md).

## Question workflows

The workflows in `tests/workflows/` were each recorded once against the real services, capturing Claude's reading of the question and every registry response, and they replay offline from those cassettes afterwards. Replay is strict. A registry request that was never recorded fails the test and names the request, so any change in what the pipeline sends to the registry is noticed. Each cassette also stores a fingerprint of the Claude prompt and output schema, and a cassette recorded under a different prompt fails with a request to re-record, because replaying the old answer would test a reading the current prompt might not produce.

The assertions are about the answer. Every record that comes back must satisfy the plan, which means the drug is named in an intervention field, the start date falls in range, and the status, phase and country are right, and this tests the query translation against how the registry actually behaves. Every workflow also runs one shared check on the final answer, which confirms that following each citation's JSON path through the raw record it cites gives exactly its excerpt, that every data point lists each of its trials once with a count that agrees, and that the recommended chart and each alternative carry a Vega-Lite spec exactly when Vega-Lite can draw that chart.

The table lists each question workflow and what would have to go wrong for it to fail.

| Workflow | Fails when |
| --- | --- |
| The assignment example | "This drug" becomes a second search, a stray filter creeps in, the capped subset is not the latest trials, the drug search returns trials that do not test the drug, mention-only trials overlap named ones or cannot be cited, or the yearly counts are not exact |
| Counting mentions | Counted mentions are double counted, break the exact total, or exceed the cohort's record cap |
| Keytruda vs Opdivo since 2018 | The year is applied per cohort instead of shared, a cohort's records do not involve its drug, or the chart is not grouped bars with phases on the axis |
| A field contradicting the question | The question's phase reaches the registry in place of the caller's, a count downloads more records than it cites, or a cited trial lacks a citation for each filter or has a drug citation that names another drug |
| Countries with the most recruiting melanoma trials | Non-recruiting or non-melanoma trials come back, the ranking is unsorted, or the map alternative cannot join countries by ISO code |
| A small network question | A complete fetch misses or repeats a trial, a sponsor links to a sponsor, or the 3D layout is flat |
| Enrollment against duration | Non-completed or off-topic trials come back, or records lack the enrollment and dates a scatter plot needs |
| Hostile caller text | Injected syntax or a stray quote reaches the registry, or the cleaned query stops finding trials |
| Claude down | The rule-based fallback does not carry the question through, or two names widen the search when they should narrow it |
| Enrollment by phase | A box's statistics are out of order, or the histogram alternative covers different trials than the boxes |
| A three-stage flow | Trials appear or vanish between stages, links skip a stage, lung cancer leaves out trials filed only under narrower MeSH terms, a mention-only trial is filed under one of those terms alone, or observational studies are labeled "Not reported" |
| Type 1 vs type 2 diabetes in 2024 | A cohort holds a trial that names only the other type, or reported mentions fetch more than the 30 records needed to quote them |
| Keytruda vs Opdivo totals in 2024 | A cohort fetches more than the records it cites, a bar differs from the registry's total, a subset note appears, or a registry request is missing from the answer |
| A trial timeline | Trials are out of start order, or some do not match the question |
| Breast cancer by status, capped | The exact registry counts do not sum to the registry's total |
| Drug combinations | A link does not come from a shared treatment arm |
| Three measures | There is no 3D scatter, or no flat bubble alternative |
| The HTTP endpoint | `/v1/query` returns anything other than exactly the pipeline's answer |

## Failure workflows

These replay the same recordings under bad conditions.

| Workflow | Checks |
| --- | --- |
| Throttling and server errors mid-run | Retries leave the result identical to a clean run, with the expected backoff |
| An outage | Exhausted retries raise an error and never return a partial answer |
| The cache | A repeat run makes no registry requests, and a registry data refresh invalidates every cached response |
| A lying interpreter | Contradictions, invented values and inverted ranges change nothing in what is sent to the registry, and each one is reported |
| The spend cap reached | Questions still resolve through the fallback, Claude is never called, and nothing is charged |
| A long-running server | One client keeps the registry's data version past its 15-minute check, refetches within a version, or keeps responses cached under the old version |

## Guard tests

Five files in `tests/` cover failures that the workflows cannot reach. `test_resolver_properties.py` checks six guarantees of the resolver over 1,500 generated pairings of caller fields with interpreter proposals: it always returns a plan or a clarification, caller fields reach the plan unchanged, the plan's slots and provenance keys match, inferred values come only from the proposal, every contradiction is reported, and the same input always gives the same output. `test_input_hazards.py` sends hostile requests through HTTP, each of which must be refused with a pointer to the exact problem, checks that unknown paths, wrong methods, missing samples and unexpected failures all return the one error shape, and checks the vocabulary traps where a plausible parse would query the wrong trials ("N/A" split like "1/2", "Korea", and a country name the registry stores with a trailing space). `test_spend_cap.py` checks the cap under concurrent calls, confirms that failed calls are charged according to whether they could have been billed, that an unexpected error reaches the fallback instead of failing the request, and that every failure releases its reservation. `test_scale_guards.py` builds a grid of 3,000 synthetic trials by lead sponsor and country and checks that the answer keeps only the largest rows and columns, honors `top_n`, says so in a note, and stays under 2 MB. `test_rules_eval_snapshot.py` runs all 54 eval questions through the deterministic path (the rule-based interpreter and the resolver) and fails if any outcome changes in either direction.

## Live tests

The three tests in `tests/live/` run only with `--run-live`. They confirm that today's registry still has the search behavior the translator relies on: synonyms expand inside the naming fields, alternatives widen a search and cohort layers narrow it, named trials plus mention-only trials add up exactly to the default search, every filter type is accepted together, and names cannot inject query syntax. Recordings cannot prove any of this, since they only show what the registry returned once.

## Checking the suite with deliberate bugs

To check that the suite catches real mistakes, 32 bugs were introduced one at a time across every stage: the resolver, the translator, the mention tier, the client, the retriever, the rule-based interpreter, the spend cap, the vocabulary, aggregation, exact counts, citations, chart recommendation, layouts and row ordering. The current suite catches all 32. Two earlier misses each exposed a weak test, and both were fixed. One was a spend-cap test that checked a fresh ledger instead of the one in use, which let a ledger that never released its reservations pass. The other was the missing check that each data point lists a trial only once, which would have caught network nodes counting a trial once per treatment arm.

## What independent review found

The deliberate bugs only test the suite against mistakes its author thought to make. Two independent reviews of the finished service, one reading the code and one checking the saved answers against the live registry with its own queries, found bugs the suite had missed. Every saved count reproduced exactly, but grouping ten thousand trials by sponsor and country returned 90,000 rows with `top_n` ignored, a comparison of totals fetched 40,000 records for two numbers and then called them a subset, a name made only of punctuation silently dropped its filter, a count cited "Carboplatin" for a Keytruda filter, a long-running server never noticed the registry's refresh, an unexpected SDK error leaked part of the spend cap and skipped the fallback, and errors outside validation used FastAPI's default shape. The registry audit found the semantic ones: lung cancer trials filed only under "Carcinoma, Non-Small-Cell Lung" reported as mere mentions, "type 1 diabetes" matched as loose words, biopsies and scans drawn as drugs, either/or arms counted as combinations, and observational studies labeled "Not reported". Each is fixed, and each has a workflow assertion or a guard test above that fails without the fix.

## What the tests found

Recording the workflows showed that the registry's default drug search matches description text, and one in ten "Keytruda" trials only listed pembrolizumab as one permitted option in an arm description. Drug and condition names now have to appear in the fields that name a trial's interventions or conditions ([retrieval.md](retrieval.md)).

The live injection test showed that a bare uppercase `AREA` still broke a request after brackets were stripped, so names are now lowercased as well. Real records mix start dates such as "2026-12" and "2026-12-01", so dates are compared at month precision.

The exact-count workflow found per-status counts summing to 32 short of the registry's total, because statuses too rare to appear in the fetched subset were never counted, and every known value is now counted. The box plot workflow caught the default quartile method putting a quartile outside its own whisker for small groups, along with a rule that disqualified box plots whenever any single group was tiny. An earlier and much larger suite caught "N/A" being split like "1/2" and cohorts that differed only by letter case.
