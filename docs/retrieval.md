# Retrieval

This page explains how a resolved plan becomes requests to the ClinicalTrials.gov API and what comes back. It is for anyone who needs to know exactly which trials an answer counts and why, or who wants to reproduce a result by hand from the request URLs in `meta.registry_requests`. Retrieval is deterministic, so the same plan always produces the same requests, and no language model is involved.

## A note on the registry's query language

ClinicalTrials.gov searches with a query language called Essie. Two parts of it appear throughout this page. `AREA[Field]value` restricts a search to one field of the trial record, so `AREA[Phase]PHASE3` matches trials whose phase field holds `PHASE3`. MeSH (Medical Subject Headings) is the National Library of Medicine's controlled vocabulary, and the registry tags each trial's conditions and interventions with MeSH terms, which is how a search for "Keytruda" can find a trial that only says "pembrolizumab".

## From plan filters to API parameters

Each plan filter maps to one API parameter by a fixed rule in `retrieval/translate.py`:

| Plan filter | API parameter | Example |
| --- | --- | --- |
| `conditions` | `query.cond`, in the naming fields, plus the MeSH terms under the condition | `(AREA[Condition]"lung cancer" OR AREA[ConditionMeshTerm]"lung cancer" OR AREA[Keyword]"lung cancer") OR (AREA[ConditionMeshTerm]"Carcinoma, Non-Small-Cell Lung")` |
| `interventions` | `query.intr`, in the naming fields | `(AREA[InterventionName](keytruda) OR AREA[InterventionOtherName](keytruda) OR AREA[InterventionMeshTerm](keytruda))` |
| `sponsors` | `query.lead` | `(pfizer)` |
| `keywords` | `query.term` | `(car-t non-hodgkin lymphoma)` |
| `phases` | `filter.advanced` | `AREA[Phase](PHASE2 OR PHASE3)` |
| `statuses` | `filter.advanced` | `AREA[OverallStatus](RECRUITING)` |
| `sponsor_classes` | `filter.advanced` | `AREA[LeadSponsorClass](INDUSTRY)` |
| `intervention_types` | `filter.advanced` | `AREA[InterventionType](DEVICE)` |
| `study_types` | `filter.advanced` | `AREA[StudyType](OBSERVATIONAL)` |
| `countries` | `filter.advanced` | `AREA[LocationCountry]("United States" OR "Canada")` |
| `start_year_from`, `start_year_to` | `filter.advanced` | `AREA[StartDate]RANGE[2018-01-01, MAX]` |

All `filter.advanced` clauses are parenthesized and joined with AND, and parameters are kept in sorted order, so two equal plans give byte-identical URLs. That property is what makes the URLs usable both as cache keys and as an exact record of how the data was fetched.

Each of the following choices was checked against the live API.

Names go through the search parameters (`query.cond`, `query.intr`), because the registry expands synonyms there, so "Keytruda", "MK-3475" and "pembrolizumab" all find the same trials.

Drug and condition names must appear in the fields that name a trial's interventions or conditions. The registry's default search also matches descriptions, and recording real workflows showed that one in ten "Keytruda" trials listed pembrolizumab only as one permitted option in an arm description, while a search for "melanoma" matched a trial titled "Non-melanoma". The measurements that settled it:

| Search | Matches | Records that name the term in a naming field |
| --- | --- | --- |
| "keytruda", default search | 2,362 | 180 of 200 |
| "keytruda", naming fields | about 1,990 | 200 of 200 |
| "melanoma" (recruiting), default search | 483 | 193 of 200 |
| "melanoma" (recruiting), naming fields | 471 | 199 of 200 |

Synonyms still expand inside the naming fields, phrases included (`AREA[ConditionMeshTerm]"heart attack"` finds exactly the 2,737 trials tagged "Myocardial Infarction"), and the restriction guarantees that every included trial has a field naming the drug or condition, which its citation needs. The cost is the trials that mention a drug only in their descriptions, between 3 and 15 percent of matches depending on the term, and the next section explains how those are kept.

A name of several words is matched in the naming fields as a phrase. Without quotes, Essie matches the words anywhere across all of a trial's conditions and keywords, so "type 1 diabetes" matched a trial whose condition was "Type 2 Diabetes Mellitus" and whose keyword was "phase 1". As loose words the type 1 and type 2 searches shared 641 trials, and as phrases they share 506, which are trials that study both. A single word such as "melanoma" or "car-t" is searched as before.

## Conditions and the MeSH tree

MeSH is a tree, and a trial tagged "Carcinoma, Non-Small-Cell Lung" is a lung cancer trial even when no field of it says "lung cancer". The registry's default search follows the tree, but its `ConditionAncestor` field is refused in every search parameter (`query.cond`, `query.term` and `filter.advanced` all answer 400), so the naming-field search cannot. An independent audit of the saved answers found the cost: at least 57 of the 115 trials reported as mere mentions in the lung cancer sample named a lung cancer through their MeSH term.

The registry does return each trial's MeSH ancestors, so retrieval finds the terms under a condition itself (`Retriever._descendants`). It first reads 200 trials that name the condition and takes as its concept the MeSH node that covers at least three quarters of them, counting ancestors, and is most often a trial's own term. For "lung cancer" that is "Lung Neoplasms", which sits over 176 of the 200, while broader nodes such as "Lung Diseases" cover as many but are rarely a trial's own term. It then reads up to 2,000 of the cohort's near misses, the trials the default search finds and the naming search does not, and keeps every MeSH term that is a near-miss trial's only condition term while the concept is among its ancestors. Trials with several terms are ignored, because their ancestors are pooled and a trial listing "Adenocarcinoma of Lung" with plain "Adenocarcinoma" would otherwise make every adenocarcinoma a lung cancer. The terms found are added to the condition's search as exact `ConditionMeshTerm` alternatives, and a note names them. For lung cancer trials started in 2016 they are "Adenocarcinoma of Lung", "Bronchial Neoplasms", "Carcinoma, Bronchogenic", "Carcinoma, Non-Small-Cell Lung" and "Mesothelioma, Malignant", and the cohort grows from 516 trials to 565.

## Sponsors and countries

Sponsors use `query.lead`, because `query.spons` also matches collaborators (6,110 trials for Pfizer against 3,901 with Pfizer as lead sponsor) and the plan's `sponsors` filter means the lead sponsor.

Countries are quoted and kept byte for byte, including the curly apostrophe in "Côte d’Ivoire" and a trailing space the registry stores in one country name.

## Named trials and mentions

Restricting names to the naming fields would silently lose the trials that mention a drug or condition only in descriptions, so retrieval fetches them as a second tier. For every cohort that filters on a drug or condition, a second query asks for trials that match the registry's default search and do not name the term in a naming field (`mention_query`, which is the default search AND NOT the naming-field search). The two queries split the default search exactly, with no overlap and nothing left out: on the live API, 2,053 named and 309 mention-only Keytruda trials since 2018 add up to the 2,362 the default search finds, and 247 plus 34 add up to the 281 Keytruda melanoma trials.

A sample of the 309 mentions shows why they cannot simply be counted. In some the drug is given under a generic intervention label ("Patients will receive pembrolizumab infusion every 3 weeks"), in others it is one permitted option among a class ("Commonly used agents include pembrolizumab and toripalimab"), and in others it is context, as in an observational study of patients "treated by pembrolizumab and chemotherapy". Telling these apart reliably takes judgment, which rules handle badly and which a language model would bring into the step that decides what gets counted, the one step this design keeps the model out of.

So `retrieval.name_matching` decides, and nothing is guessed. With `named`, the default, mentions are counted exactly and reported with up to 30 records, enough to quote, but not counted in the chart. With `mentioned`, they are counted as well, every record is fetched within the cohort's record cap, and they stay labeled as mentions. Mention records also carry the text they were matched on (the official title, brief summary, keywords, and intervention and arm descriptions), so each mention can be quoted and cited, and in the recorded workflow all 100 fetched Keytruda mentions contained the drug in that text.

## Making names safe to search

Names come from people and from a language model, and the API executes any query syntax it finds inside them. During testing, `AREA[Phase]PHASE3` placed in a keyword ran as a phase filter and matched 50,185 trials, a stray `"` failed the request with a 400, and even with the brackets removed, a bare uppercase `AREA`, `RANGE` or `MISSING` still failed with a 400.

`retrieval/essie.py` therefore reduces each name to plain words, stripping syntax characters and lowercasing the text. Lowercasing disarms every operator, because the operators are uppercase keywords and matching ignores case. Possessives are dropped as well, since MeSH drops them ("Alzheimer Disease") and "alzheimer's disease" found 2,177 completed trials where "alzheimer disease" found 2,196. In the naming fields a parenthetical is dropped too, because it is an abbreviation or alias ("Crohn's disease (CD)") that would make the phrase match nothing. The quotes added around a phrase are the service's own, never the caller's, so they cannot be used to inject syntax. A name that reduces to no words at all, such as "???" or "(CD)", is refused with a 422 when the request is validated, by the same function retrieval uses, since an empty search would otherwise drop its filter and match the whole registry.

## How many requests a plan becomes

The number of requests follows fixed rules in `retrieval/retriever.py`, decided by the plan's shape and the size of the result.

1. A comparison runs one query per cohort, each requiring the shared filters and that cohort's own, and any other analysis runs one query.
2. A cohort that filters on a drug or condition also runs the mention-only query described above, and a cohort that filters on a condition first reads the two small samples that find the MeSH terms under it.
3. Each query starts with a count-only request (`pageSize=1`, `fields=NCTId`), and the count decides what to fetch.
4. A `count`, and a comparison of cohort totals without `group_by`, are answered by the registry's counts and need records only to cite them, so they fetch `citations_per_datum` records per cohort and are not marked truncated. Reported mentions fetch at most 30 records, enough to quote. Every other analysis fetches all matches. Only the fields the analysis reads are requested, plus the fields that show why each record matched its filters (its countries for a country filter, its drug names for a drug filter), which keeps a 10,000-trial fetch to a few megabytes while every included trial carries its own evidence.
5. When more trials match than `RETRIEVAL_MAX_RECORDS` (10,000 by default), the most recently started are fetched, sorted by `StartDate:desc`, and the cohort is marked truncated with its exact total kept. The cohorts of a comparison share the cap, each fetching at most the larger of 1,000 and the cap divided by the number of cohorts, so six large cohorts cost about as much as one. The API returns results in no stable order unless asked, so without an explicit sort a partial fetch would differ from one run to the next.

Every request made for an answer, including the MeSH lookups and the exact counts that later stages make, is listed in `meta.registry_requests`, so an answer can be reproduced by hand. An answer also has a deadline on its registry work, `ANSWER_DEADLINE_SECONDS` (180 by default), after which it stops with a 504 and a suggestion to narrow the question.

As an example, "compare phases for Keytruda vs Opdivo since 2018" became two cohorts of 2,053 and 1,265 named trials, each fetched completely within its share of 5,000 records, with a few of its mentions, while "How are breast cancer trials distributed across status?" matched more than 16,000 trials, so 10,000 were fetched and the per-status counts were taken from the registry directly, as [aggregation.md](aggregation.md#exact-counts-for-large-result-sets) describes.

## The HTTP client

`retrieval/client.py` handles the conversation with the API.

Requests are spaced to `CTGOV_REQUESTS_PER_MINUTE` (50 by default), because the API starts answering 429 after roughly ten requests in quick succession. A 429, a server error or a dropped connection is retried with backoff of 2, 4 and 8 seconds, for four attempts in all, while a 400 means the query itself is wrong and fails at once with the API's own message, and other client errors are not retried either.

Pages follow `nextPageToken`, repeating the first page's parameters as the API requires, with only the page size shrinking for the last page.

Responses are cached on disk in one folder per registry data version, the `dataTimestamp` that `/version` reports and that changes with each daily refresh. One client lives as long as the server, so it asks for the version again every 15 minutes, and when the version changes it starts a new folder and removes the older ones. A long-running server therefore answers from the current data within 15 minutes of a refresh, the cache never grows past one version, and failures are never cached. Cache reads and writes run in a worker thread so they do not stall other requests.

## What retrieval returns

A `Retrieval` holds the registry's data version, the fields requested, the record limit, the name-matching setting and one `CohortRecords` per cohort. Each cohort has a `named` record set and, when it filters on a drug or condition, a `mentions` record set, along with the MeSH terms added to each condition and the requests made to find them, and each set holds its query, its exact total, the raw study records, the sort used if any, and the URL of every request made. Results are computed from the cohort's `studies` and `total`, which are the named trials plus the mentions when those are counted. Every later stage cites trials from these raw records, so each value in an answer can be traced to a field in a real API response.

## Limitations

The first run of a large query is slow, since 10,000 records take 10 page requests at the polite rate, about 12 to 15 seconds, and repeats are served from the cache.

When a result set is capped, values computed from the records themselves (measures such as enrollment, or free-text groups such as sponsors) describe the fetched subset, and the response says so. Group counts on dimensions the registry can filter by are taken from the registry exactly, as [aggregation.md](aggregation.md#exact-counts-for-large-result-sets) explains.

Sanitizing removes syntax characters from names, so a name whose meaning depends on one of them (rare in drug and disease names) is searched without it.

The MeSH terms under a condition are learned from samples of near misses, so a narrower term that never appears as the only condition term of a near-miss trial is not added, and a condition whose sample has no node covering three quarters of it gets no terms at all.

A trial that lists a drug only among one intervention's other names, as one of several alternatives ("Sintilimab", other names "Pembrolizumab, Nivolumab"), still names the drug in a naming field and matches. The audit measured this at about 1.4 percent of the pembrolizumab trials.
