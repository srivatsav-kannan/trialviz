# Interpretation eval

How well each interpreter turns a question into the right plan.

## What it measures

`cases.json` holds 49 labeled questions. Each runs through the full resolver, so a case passes only when the final plan (or clarification) is right, not merely the interpreter's raw guess. Expectations are partial on purpose. A case names the slots that matter, the slots that must stay empty, the notes that must or must not appear, and where a question has two fair readings, both alternatives (`any_of`).

The questions cover the assignment's appendix, then lean hard on the cases that break interpreters: conflicts between the question and the caller's fields, references like "this drug", relative and exclusive dates ("since last year", "after 2019"), place names that collide ("New Jersey", the country of Georgia), negations, constraints no filter can express, comparisons defined by year ranges, a question in Spanish, and an instruction planted in the question.

```bash
uv run python -m evals.interpretation.run --interpreter rules
uv run python -m evals.interpretation.run --interpreter claude    # about $0.37 per run
```

Per-case outcomes, including the full resolved plan, are written to `results/<interpreter>.json`.

## Results

| Interpreter | Overall | Appendix | Hard tranche | Entity typing | Comparison |
| --- | --- | --- | --- | --- | --- |
| Claude (`claude-opus-5-5`, low effort) | 49/49 | 9/9 | 13/13 | 6/6 | 5/5 |
| Rule-based | 16/49 | 1/9 | 1/13 | 1/6 | 0/5 |

The rule-based interpreter fails mostly by design. It files every name it finds as a free-text keyword rather than guessing whether it is a drug or a disease, so any case that expects a typed filter fails. What it does read (years, phases, statuses, analysis cues) it reads reliably, which is what a fallback needs.

## How the numbers got here

The history matters more than the final score:

1. **First 36 cases: 36/36.** Reading the outputs showed the set was too easy for the model, and it exposed a real gap. Constraints the plan cannot express ("more than 500 participants") were dropped silently, so a broader question got answered.
2. **The gap became a feature.** The interpreter now lists constraints it cannot express, and the resolver reports each one as a `constraint_not_applied` note.
3. **13 harder cases: 49/49** on three consecutive runs.
4. **A precision check: 47/49.** The model was now over-reporting, flagging "vaccine trials" as inexpressible when a keyword expresses it fine. Two cases were given `forbidden_notes`, and both failed. The cause was a contradiction in the prompt: it restricted keywords to names whose type was unclear, leaving topic words nowhere to go.
5. **One prompt fix: 49/49** on each of the three runs since.

## Caveats

- The questions were written by the same author as the prompt, during the same session. That favors the prompt. A held-out set written by someone else would be a fairer test.
- 49 questions cannot show a small error rate. A perfect score here is consistent with a few percent of real questions being misread.
- Scoring is partial. A case passes if the slots it names are right, so an extra, unasked-for filter can go unnoticed unless a case forbids it.
- The model is not deterministic. Three identical runs agreeing is evidence of stability on this set, not a guarantee.
