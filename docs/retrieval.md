# Retrieval

This page explains how a resolved plan becomes requests to the ClinicalTrials.gov API, which trials an answer therefore counts, and why. It is also the guide for reproducing a result by hand from the request URLs listed in `meta.registry_requests`. Retrieval is deterministic, so the same plan always produces the same requests, and no language model is involved.

## The registry's query language

ClinicalTrials.gov searches with a query language called Essie, and two of its ideas run through this page. `AREA[Field]value` restricts a search to one field of the trial record, so that, for example, `AREA[Phase]PHASE3` matches trials whose phase is Phase 3. MeSH (Medical Subject Headings) is the National Library of Medicine's controlled vocabulary, and the registry tags each trial's conditions and interventions with MeSH terms, which is how a search for "Keytruda" can find a trial that only says "pembrolizumab".

## From plan filters to API parameters

Each plan filter maps to one API parameter by a fixed rule in `retrieval/translate.py`.

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

Parameters are always written in the same order, so two equal plans produce identical URLs, which is what lets a URL serve both as a cache key and as an exact record of how the data was fetched. Every choice below was checked against the live API.

## Matching drug and condition names

Names go through the search parameters, because the registry expands synonyms there, so that "Keytruda", "MK-3475" and "pembrolizumab", for example, all find the same trials. They must also appear in the fields that name a trial's interventions or conditions. The registry's default search reads descriptions as well. Recordings of real questions showed that about one in ten trials that matched "Keytruda" mentioned pembrolizumab only in an arm description, as one permitted option among several, and a search for "melanoma" matched, for example, a trial titled "Non-melanoma".

| Search | Matches | Records that name the term in a naming field |
| --- | --- | --- |
| "keytruda" (started since 2018), default search | 2,362 | 180 of 200 |
| "keytruda" (started since 2018), naming fields | 2,053 | 200 of 200 |
| "melanoma" (recruiting), default search | 483 | 193 of 200 |
| "melanoma" (recruiting), naming fields | 471 | 199 of 200 |

Synonyms still expand inside the naming fields, and the restriction guarantees that every included trial has a field naming the drug or condition, which its citation needs. The cost is the trials that mention a name only in their descriptions, 3 to 15 percent of matches depending on the term, and a later section explains how those are kept.

A name of several words is matched as a phrase. Unquoted, the words can match anywhere across a trial's separate conditions and keywords, so that, for example, "type 1 diabetes" matched a trial whose condition was "Type 2 Diabetes Mellitus" and whose keyword was "phase 1". As loose words the type 1 and type 2 searches shared 641 trials; as phrases they share 506, and each of those names both types.

## Conditions and the MeSH tree

MeSH is a tree, and a trial tagged "Carcinoma, Non-Small-Cell Lung" is a lung cancer trial even when none of its own fields says "lung cancer". The registry's default search follows the tree, but it refuses any search on a trial's MeSH ancestors, so a search restricted to the naming fields misses such trials. An independent check of the saved answers found at least 57 lung cancer trials being reported as mere mentions for this reason.

The registry does return each trial's MeSH ancestors, so retrieval finds the narrower terms itself, in two small steps. It reads 200 trials that name the condition and takes as the condition's concept the MeSH term that sits above at least three quarters of them and is most often used directly. For "lung cancer" that is "Lung Neoplasms", above 176 of the 200; broader terms such as "Lung Diseases" sit above as many trials but are rarely used directly. It then reads up to 2,000 of the cohort's near misses, the trials the default search finds and the naming search does not, and keeps each MeSH term that is a trial's only condition term and has the concept among its ancestors. Trials with several terms are skipped, because the registry pools their ancestors, so a trial listing "Adenocarcinoma of Lung" next to plain "Adenocarcinoma" would otherwise make every adenocarcinoma a lung cancer.

The terms found are added to the condition's search, and a note in the answer names them. For lung cancer trials started in 2016 they are "Adenocarcinoma of Lung", "Bronchial Neoplasms", "Carcinoma, Bronchogenic", "Carcinoma, Non-Small-Cell Lung" and "Mesothelioma, Malignant", and the cohort grows from 516 trials to 565.

## Sponsors and countries

Sponsors use `query.lead`, because `query.spons` also matches collaborators (6,110 trials for Pfizer against 3,901 with Pfizer as lead sponsor), and the plan's sponsor filter means the lead sponsor. Countries are kept exactly as the registry spells them, including the curly apostrophe in "Côte d’Ivoire" and a trailing space it stores in one country name.

## Named trials and mentions

Restricting names to the naming fields would silently lose the trials that mention a drug or condition only in their descriptions, so retrieval fetches them as a second tier. For every cohort that filters on a drug or condition, a second query asks for the trials that the default search finds and the naming search leaves out. The two queries split the default search exactly: on the live API, 2,053 named and 309 mention-only Keytruda trials since 2018 add up to the 2,362 the default search finds.

A sample of those 309 shows why they cannot simply be counted. In some the drug is given under a generic label ("Patients will receive pembrolizumab infusion every 3 weeks"), in some it is one permitted option among several ("Commonly used agents include pembrolizumab and toripalimab"), and in others it is background, as in an observational study of patients "treated by pembrolizumab and chemotherapy". Telling these apart takes judgment that rules handle badly and that a language model would bring into the one step this design keeps it out of, deciding what gets counted.

So `retrieval.name_matching` decides, and nothing is guessed. Under `named`, the default, the number of mention-only trials is reported exactly and up to 30 of their records are fetched, which is enough to quote, but they are left out of the chart. Under `mentioned` they are counted too, every record is fetched within the cohort's record cap, and they stay labeled as mentions. Their records include the text they matched on, so each one can be quoted and cited.

## Making names safe to search

Names come from people and from a language model, and the API executes any query syntax it finds inside them. During testing, `AREA[Phase]PHASE3` typed as a keyword ran as a phase filter and matched 50,185 trials, and a stray quote or a bare uppercase `AREA` failed the request.

Each name is therefore reduced to plain lowercase words before it is searched. Lowercasing disarms every operator, because the operators are uppercase keywords and matching ignores case. Possessives are dropped, since MeSH drops them (for example, "Alzheimer Disease"), and "alzheimer's disease" found 19 fewer completed trials than "alzheimer disease". Inside the naming fields a parenthetical is dropped too, because an abbreviation such as "Crohn's disease (CD)" would make the phrase match nothing. A name that reduces to no words at all, such as "???" or "(CD)", is refused when the request is validated, since an empty search would drop its filter and match the whole registry.

## How many requests a plan becomes

The number of requests follows fixed rules in `retrieval/retriever.py`.

1. A comparison runs one query per cohort, each requiring the shared filters and the cohort's own, and any other analysis runs one query.
2. A cohort that filters on a drug or condition also runs the mention-only query, and one that filters on a condition first reads the two small samples that find the MeSH terms under it.
3. Each query starts with a request for its count, which decides what to fetch.
4. A count, and a comparison of cohort totals without a breakdown, are answered by the registry's counts, so they fetch only the few records they cite. Reported mentions fetch at most 30 records. Every other analysis fetches all matches, requesting only the fields it reads plus the fields that show why each trial matched, which keeps a 10,000-trial fetch to a few megabytes.
5. When more trials match than the record cap (10,000 by default), the most recently started are fetched, in a fixed order so the subset is the same on every run, and the cohort keeps its exact total. The cohorts of a comparison share the cap, each fetching at most the larger of 1,000 and the cap divided by the number of cohorts.

Every request made for an answer is listed in `meta.registry_requests`, including the MeSH lookups and the exact counts made later, so an answer can be reproduced by hand. The registry work for one answer has a deadline, 180 seconds by default, after which the request stops with a 504 and a suggestion to narrow the question.

As an example, "compare phases for Keytruda vs Opdivo since 2018" became two cohorts of 2,053 and 1,265 trials, each fetched in full. "How are breast cancer trials distributed across status?" matched more than 16,000 trials, so the most recent 10,000 were fetched and the per-status counts were taken from the registry directly ([aggregation.md](aggregation.md#exact-counts-for-large-result-sets)).

## The HTTP client

Requests are spaced to 50 per minute by default, because the API starts refusing requests after roughly ten in quick succession. A refusal, a server error or a dropped connection is retried after 2, 4 and 8 seconds, four attempts in all. A malformed query fails at once with the API's own message, since retrying would not help.

Responses are cached on disk, in one folder per registry data version. The registry refreshes once a day, and the server checks the version every 15 minutes, starting a new folder and removing the older ones when it changes. Answers therefore come from current data within 15 minutes of a refresh, and the cache never holds more than one version. Failures are never cached.

## Limitations

The first run of a large question is slow: 10,000 records take 10 page requests at the polite rate, about 12 to 15 seconds, and repeats come from the cache.

When a result set is capped, values computed from the records themselves, such as enrollment or free-text groups like sponsors, describe the fetched subset, and the answer says so. Group counts on dimensions the registry can filter by come from the registry exactly ([aggregation.md](aggregation.md#exact-counts-for-large-result-sets)).

Narrower MeSH terms are learned from samples of near misses, so a term that never appears alone on a near-miss trial is not added, and a condition with no clear concept in its sample gets no added terms.

A trial that lists a drug only among an intervention's other names, as one of several alternatives, still names it in a naming field and matches. That is about 1.4 percent of the pembrolizumab trials.
