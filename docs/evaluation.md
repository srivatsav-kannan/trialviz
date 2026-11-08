# Interpretation eval

This page describes how well each interpreter turns a question into the right plan, how that is measured, and how far the result can be trusted. The labeled questions are in `evals/cases.json`, the runner is `evals/run.py`, and the latest outcome of every case, with its full resolved plan, is in `evals/results/`.

```bash
uv run python -m evals.run --interpreter rules
uv run python -m evals.run --interpreter claude    # roughly 40 cents a run, against the spend cap
```

## What is measured

There are 54 labeled questions. Each one runs through the full resolver with the date fixed at 7 November 2026, so that relative dates such as "since last year" have one right answer, and a case passes only when the final plan or clarification is right. That is stricter than grading the interpreter's raw output, because a guess the resolver rejects or overrides counts against it. A case names the values that matter, the values that must stay empty, and the notes that must or must not appear. Three questions have two fair readings, and those cases accept either.

The set starts with the nine example questions from the assignment's appendix and then concentrates on what tends to break an interpreter: a question that contradicts the caller's fields, references such as "this drug", relative and exclusive dates, place names that collide (New Jersey, or the country of Georgia), negations, constraints no filter can express, comparisons defined by year ranges, a question in Spanish, an instruction planted inside the question, and the less common analyses such as flows, trial lists and three-measure scatter plots.

## Results

| Interpreter | Overall | Appendix | Hard cases | Entity typing | Comparison | Newer kinds | Clarification |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude (`claude-opus-5-5`, low effort) | 54/54 | 9/9 | 14/14 | 6/6 | 5/5 | 5/5 | 4/4 |
| Rule-based | 15/54 | 1/9 | 1/14 | 1/6 | 0/5 | 0/5 | 2/4 |

The rule-based interpreter fails mostly for one reason: it cannot tell a drug from a disease, so it files every name it finds as a free-text keyword, and any case that expects a drug or condition filter fails. What it does read, such as years, phases, statuses and the shape of the analysis, it reads reliably, which is what a fallback needs. Its results are deterministic, so rerunning the eval with `--interpreter rules` after a change to the rules shows exactly which cases moved.

## How the score developed

The first 36 cases all passed, which meant the set was too easy. Reading the outputs also showed something wrong underneath: constraints the plan cannot express, such as "more than 500 participants", were dropped without a word, so the service answered a broader question than the one asked. The interpreter now lists what it cannot express, and the answer reports each one as a note.

Thirteen harder cases came next, and the set passed 49 of 49 on three consecutive runs. A check for the opposite mistake followed, because the model had started flagging phrases like "vaccine trials" as inexpressible when a keyword expresses them perfectly well. Two cases that forbid that note both failed, and the cause was a contradiction in the prompt, which one fix resolved. Work on retrieval later showed that separate keywords widen a search, since values in one filter are alternatives, so the prompt now asks for terms that must all appear to go in a single keyword.

The last five cases arrived with the flow, trial-list, grouped-histogram and three-measure analyses. One older case asked for lung cancer trials grouped by principal investigator and expected a request for clarification, because investigators were not yet something the service could group by. Once they were added, that case was changed to expect a distribution by investigator.

## Caveats

The questions were written by the same person who wrote the prompt, in the same session, which favors the prompt; a held-out set written by someone else would be a fairer test. Fifty-four questions also cannot reveal a small error rate, since a perfect score here is consistent with a few percent of real questions being misread. Scoring is partial, so a filter nobody asked for goes unnoticed unless a case forbids it, and the model is not deterministic, so agreement between runs shows stability on this set without guaranteeing it.
