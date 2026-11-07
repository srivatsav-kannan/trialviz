# Retrieval

How a plan becomes requests to ClinicalTrials.gov, and what comes back. Retrieval is deterministic. No model is involved, and the same plan always produces the same requests.

## Translation

Each plan filter maps to one API parameter by a fixed rule (`retrieval/translate.py`):

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

All `filter.advanced` clauses are parenthesized and joined with AND. Parameters are kept in sorted order, so equal plans give byte-identical URLs. That is what makes the URLs usable as cache keys and as a record of exactly how data was fetched.

Each choice below was checked against the live API:

- **Names use the search parameters**, because the registry expands synonyms there. "Keytruda", "MK-3475" and "pembrolizumab" all find the same trials.
- **Drug and condition names must appear in the fields that name them.** The default search also matches descriptions. Recording real workflows showed one in ten "Keytruda" trials only listed pembrolizumab as one permitted option in an arm description, and a "melanoma" search matched a "Non-melanoma" title. Measured on the live API:

  | Search | Matches | Records that name the term in a naming field |
  | --- | --- | --- |
  | "keytruda", default | 2,362 | 180 of 200 |
  | "keytruda", naming fields | about 1,990 | 200 of 200 |
  | "melanoma" (recruiting), default | 483 | 193 of 200 |
  | "melanoma" (recruiting), naming fields | 471 | 199 of 200 |

  Synonyms still expand inside the naming fields ("heart attack" finds myocardial infarction trials). The restriction also guarantees that every included trial has a field naming the drug or condition, which citations need. The cost is the trials that mention a drug only in descriptions, 3 to 15 percent of matches depending on the term.
- **Sponsors use `query.lead`.** `query.spons` also matches collaborators (6,110 trials for Pfizer against 3,901 as lead sponsor), and the plan's `sponsors` filter means the lead sponsor.
- **Countries are quoted and kept byte for byte**, including the curly apostrophe in "Côte d’Ivoire" and a trailing space the registry stores in one name.

## Making names safe

Names come from people and from a language model, and the API executes any query syntax inside them:

- `AREA[Phase]PHASE3` in a keyword ran as a phase filter and matched 50,185 trials.
- A stray `"` failed the request with a 400.
- Even with the brackets removed, a bare uppercase `AREA`, `RANGE` or `MISSING` failed with a 400.

Each name is reduced to plain words: syntax characters are stripped and the text is lowercased (`retrieval/essie.py`). Lowercasing disarms every operator, because the operators are uppercase keywords and matching ignores case. Quoting each name would also have been safe, but it turns a name into an exact phrase search and loses matches ("CAR-T" finds 2,963 trials unquoted and 2,629 quoted).

## How many requests

Fixed rules, from the plan's shape and the result size (`retrieval/retriever.py`):

1. **One query per cohort.** A comparison runs one query per cohort, each requiring the shared filters and that cohort's own. Anything else runs one query.
2. **Count first.** Each query starts with a count-only request (`pageSize=1`, `fields=NCTId`), which decides what to fetch.
3. **Fetch only what the analysis reads.** A `count` needs records only to cite them, so it fetches `citations_per_datum` records. Other analyses fetch every match. Only the fields the analysis reads are requested, plus the fields that show why each record matched its filters (its countries for a country filter, its drug names for a drug filter). That keeps a 10,000-trial fetch to a few megabytes, and every included trial carries its own evidence.
4. **Above the cap, a reproducible subset.** When more trials match than `RETRIEVAL_MAX_RECORDS` (default 10,000), the first 10,000 are fetched by most recent start date and the cohort is marked truncated, with the exact total kept. The API returns unsorted results by default, so without an explicit order a partial fetch would differ from run to run.

For example, "compare phases for Keytruda vs Opdivo since 2018" became two cohorts of 2,362 and 1,486 trials, each fetched completely (about 12 seconds the first time). "How are breast cancer trials distributed across phases?" matched 16,998 trials, so 10,000 were fetched and the cohort was flagged as truncated.

## The client

`retrieval/client.py` talks to the API:

- **Rate limit.** Requests are spaced to `CTGOV_REQUESTS_PER_MINUTE` (default 50). The API answers 429 after roughly ten requests in quick succession, so spacing is enforced rather than hoped for.
- **Retries.** A 429, a server error or a dropped connection is retried with backoff (2, 4 and 8 seconds, four attempts in all). A 400 means the query is wrong, so it fails at once with the API's own message. Other client errors are not retried.
- **Paging.** Pages follow `nextPageToken`, repeating the first page's parameters as the API requires. Only the page size shrinks for the last page.
- **Cache.** Responses are stored on disk under a key that includes the registry's `dataTimestamp` from `/version`. A cached response therefore always matches the current registry data. When the registry refreshes (daily), keys change and the cache misses. Failures are never cached.

## What retrieval returns

A `Retrieval` holds the registry data version, the fields requested, the record limit, and one `CohortRecords` per cohort: its label, query, exact total, the raw study records, the sort used (if any), and the URL of every request made. Later stages cite trials from these raw records, so every value they report can be traced to a field in a real API response.

## Limitations

- Above the cap, figures computed from records describe the fetched subset. The exact total is always known, and the next stage will report the difference. Exact per-group counts for large result sets (from count-only queries per group) belong to that stage, because they depend on how groups are defined.
- The first run of a large query is slow by design. 10,000 records take 10 page requests at the polite rate, about 12 to 15 seconds. Repeats are served from the cache.
- Sanitizing removes syntax characters from names, so a name whose meaning depends on one (rare in drug and disease names) is searched without it.
