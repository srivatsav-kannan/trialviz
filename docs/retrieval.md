# Retrieval

This page explains how a resolved plan becomes requests to the ClinicalTrials.gov API and what comes back. It is for anyone who needs to know exactly which trials an answer counts and why, or who wants to reproduce a result by hand from the request URLs in `meta.registry_requests`. Retrieval is deterministic, so the same plan always produces the same requests, and no language model is involved.

## A note on the registry's query language

ClinicalTrials.gov searches with a query language called Essie. Two parts of it appear throughout this page. `AREA[Field]value` restricts a search to one field of the trial record, so `AREA[Phase]PHASE3` matches trials whose phase field holds `PHASE3`. MeSH (Medical Subject Headings) is the National Library of Medicine's controlled vocabulary, and the registry tags each trial's conditions and interventions with MeSH terms, which is how a search for "Keytruda" can find a trial that only says "pembrolizumab".

## From plan filters to API parameters

Each plan filter maps to one API parameter by a fixed rule in `retrieval/translate.py`:

| Plan filter | API parameter | Example |
| --- | --- | --- |
| `conditions` | `query.cond`, in the naming fields | `(AREA[Condition](melanoma) OR AREA[ConditionMeshTerm](melanoma) OR AREA[Keyword](melanoma))` |
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

Synonyms still expand inside the naming fields ("heart attack" finds myocardial infarction trials), and the restriction guarantees that every included trial has a field naming the drug or condition, which its citation needs. The cost is the trials that mention a drug only in their descriptions, between 3 and 15 percent of matches depending on the term, and the next section explains how those are kept.

Sponsors use `query.lead`, because `query.spons` also matches collaborators (6,110 trials for Pfizer against 3,901 with Pfizer as lead sponsor) and the plan's `sponsors` filter means the lead sponsor.

Countries are quoted and kept byte for byte, including the curly apostrophe in "Côte d’Ivoire" and a trailing space the registry stores in one country name.

## Named trials and mentions

Restricting names to the naming fields would silently lose the trials that mention a drug or condition only in descriptions, so retrieval fetches them as a second tier. For every cohort that filters on a drug or condition, a second query asks for trials that match the registry's default search and do not name the term in a naming field (`mention_query`, which is the default search AND NOT the naming-field search). The two queries split the default search exactly, with no overlap and nothing left out: on the live API, 2,053 named and 309 mention-only Keytruda trials since 2018 add up to the 2,362 the default search finds, and 247 plus 34 add up to the 281 Keytruda melanoma trials.

A sample of the 309 mentions shows why they cannot simply be counted. In some the drug is given under a generic intervention label ("Patients will receive pembrolizumab infusion every 3 weeks"), in others it is one permitted option among a class ("Commonly used agents include pembrolizumab and toripalimab"), and in others it is context, as in an observational study of patients "treated by pembrolizumab and chemotherapy". Telling these apart reliably takes judgment, which rules handle badly and which a language model would bring into the step that decides what gets counted, the one step this design keeps the model out of.

So `retrieval.name_matching` decides, and nothing is guessed. With `named`, the default, mentions are fetched and reported with their records but not counted. With `mentioned`, they are counted as well, sharing the cohort's record cap, and they stay labeled as mentions. Mention records also carry the text they were matched on (the official title, brief summary, keywords, and intervention and arm descriptions), so each mention can be quoted and cited, and in the recorded workflow all 100 fetched Keytruda mentions contained the drug in that text.

## Making names safe to search

Names come from people and from a language model, and the API executes any query syntax it finds inside them. During testing, `AREA[Phase]PHASE3` placed in a keyword ran as a phase filter and matched 50,185 trials, a stray `"` failed the request with a 400, and even with the brackets removed, a bare uppercase `AREA`, `RANGE` or `MISSING` still failed with a 400.

`retrieval/essie.py` therefore reduces each name to plain words, stripping syntax characters and lowercasing the text. Lowercasing disarms every operator, because the operators are uppercase keywords and matching ignores case. Quoting each name would also have been safe, but it turns a name into an exact phrase and loses matches ("CAR-T" finds 2,963 trials unquoted and 2,629 quoted).

## How many requests a plan becomes

The number of requests follows fixed rules in `retrieval/retriever.py`, decided by the plan's shape and the size of the result.

1. A comparison runs one query per cohort, each requiring the shared filters and that cohort's own, and any other analysis runs one query.
2. A cohort that filters on a drug or condition also runs the mention-only query described above.
3. Each query starts with a count-only request (`pageSize=1`, `fields=NCTId`), and the count decides what to fetch.
4. A `count` needs records only to cite them, so it fetches `citations_per_datum` records, while every other analysis fetches all matches. Only the fields the analysis reads are requested, plus the fields that show why each record matched its filters (its countries for a country filter, its drug names for a drug filter), which keeps a 10,000-trial fetch to a few megabytes while every included trial carries its own evidence.
5. When more trials match than `RETRIEVAL_MAX_RECORDS` (10,000 by default), the 10,000 most recently started are fetched, sorted by `StartDate:desc`, and the cohort is marked truncated with its exact total kept. The API returns results in no stable order unless asked, so without an explicit sort a partial fetch would differ from one run to the next.

As an example, "compare phases for Keytruda vs Opdivo since 2018" became two cohorts of 2,053 and 1,265 named trials, each fetched completely along with its mentions, while "How are breast cancer trials distributed across status?" matched more than 16,000 trials, so 10,000 were fetched and the per-status counts were taken from the registry directly, as [aggregation.md](aggregation.md#exact-counts-for-large-result-sets) describes.

## The HTTP client

`retrieval/client.py` handles the conversation with the API.

Requests are spaced to `CTGOV_REQUESTS_PER_MINUTE` (50 by default), because the API starts answering 429 after roughly ten requests in quick succession. A 429, a server error or a dropped connection is retried with backoff of 2, 4 and 8 seconds, for four attempts in all, while a 400 means the query itself is wrong and fails at once with the API's own message, and other client errors are not retried either.

Pages follow `nextPageToken`, repeating the first page's parameters as the API requires, with only the page size shrinking for the last page.

Responses are cached on disk under a key that includes the registry's `dataTimestamp`, the data version that `/version` reports and that changes with each daily refresh. A cached response therefore always matches the registry's current data, a refresh simply makes every old key miss, and failures are never cached.

## What retrieval returns

A `Retrieval` holds the registry's data version, the fields requested, the record limit, the name-matching setting and one `CohortRecords` per cohort. Each cohort has a `named` record set and, when it filters on a drug or condition, a `mentions` record set, and each set holds its query, its exact total, the raw study records, the sort used if any, and the URL of every request made. Results are computed from the cohort's `studies` and `total`, which are the named trials plus the mentions when those are counted. Every later stage cites trials from these raw records, so each value in an answer can be traced to a field in a real API response.

## Limitations

The first run of a large query is slow, since 10,000 records take 10 page requests at the polite rate, about 12 to 15 seconds, and repeats are served from the cache.

When a result set is capped, values computed from the records themselves (measures such as enrollment, or free-text groups such as sponsors) describe the fetched subset, and the response says so. Group counts on dimensions the registry can filter by are taken from the registry exactly, as [aggregation.md](aggregation.md#exact-counts-for-large-result-sets) explains.

Sanitizing removes syntax characters from names, so a name whose meaning depends on one of them (rare in drug and disease names) is searched without it.
