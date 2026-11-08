# Interpretation eval

This document describes how well each interpreter turns a question into the right plan, how that is measured, and how far the result can be trusted. The eval lives in `evals/`, with the labeled questions in `cases.json`, the runner in `run.py`, and the latest per-case outcomes in `results/`.

```bash
uv run python -m evals.run --interpreter rules
uv run python -m evals.run --interpreter claude    # roughly 40 cents a run, against the spend cap
```

## What is measured

`cases.json` holds 54 labeled questions. Each one runs through the full resolver with the date fixed at 2026-11-07, so relative dates have a single right answer, and a case passes only when the final plan or clarification is right, which is stricter than grading the interpreter's raw guess. A case names the slots that matter, the slots that must stay empty, any values a slot must not contain, and the notes that must or must not appear. Three questions have two fair readings, and for those the case lists both and passes if either one holds.

The questions start with the nine examples in the assignment's appendix and then concentrate on what tends to break an interpreter: conflicts between the question and the caller's fields, references such as "this drug", relative and exclusive dates ("since last year", "after 2019"), place names that collide ("New Jersey", the country of Georgia), negations, constraints no filter can express, comparisons defined by year ranges, a question in Spanish, an instruction planted inside the question, and the newer analysis kinds (flows, trial lists, grouped histograms and three-measure scatters). `results/<interpreter>.json` records every case's outcome along with the full resolved plan.

## Results

| Interpreter | Overall | Appendix | Hard cases | Entity typing | Comparison | Newer kinds | Clarification |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude (`claude-opus-5-5`, low effort) | 54/54 | 9/9 | 14/14 | 6/6 | 5/5 | 5/5 | 5/5 |
| Rule-based | 16/54 | 1/9 | 1/14 | 1/6 | 0/5 | 0/5 | 3/5 |

Most of the rule-based interpreter's failures come from one limitation: it cannot tell a drug from a disease, so it files every name it finds as a free-text keyword, and every case that expects a typed filter fails. What it does read (years, phases, statuses and analysis cues) it reads reliably, which is what a fallback needs. Its outcomes are also deterministic, so `tests/test_rules_eval_snapshot.py` reruns them on every test run and fails if any case changes.

## How the score developed

The first 36 cases all passed, and reading the outputs showed two things: the set was too easy for the model, and constraints the plan cannot express ("more than 500 participants") were being dropped silently, so a broader question got answered without saying so. The interpreter now lists the constraints it cannot express, and the resolver reports each one as a `constraint_not_applied` note.

Thirteen harder cases were added next, and the set passed 49 of 49 on three consecutive runs. A precision check followed, because the model had started over-reporting, flagging "vaccine trials" as inexpressible when a keyword expresses it fine. Two cases that forbid that note were added and both failed, and the cause turned out to be a contradiction in the prompt, which restricted keywords to names of unclear type and left topic words nowhere to go. After one prompt fix the set passed 49 of 49 on each of the next three runs.

Retrieval work then showed that separate keywords widen a search ("CAR-T" or "lymphoma"), because values in one filter are alternatives, so one prompt sentence now says that terms which must all appear go in a single keyword, and the eval was rerun to check for regressions. The last five cases were added with the flow, trial list, grouped histogram and three-measure analyses, bringing the set to its current 54.

## Caveats

The questions were written by the author of the prompt during the same session, which favors the prompt, and a held-out set written by someone else would be a fairer test. Fifty-four questions also cannot reveal a small error rate, since a perfect score here is consistent with a few percent of real questions being misread. Scoring is partial as well, so an extra filter that nobody asked for can go unnoticed unless a case forbids it. Finally, the model is not deterministic, and agreement between repeated runs is evidence of stability on this set without being a guarantee.
