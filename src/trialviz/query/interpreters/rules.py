"""A deterministic interpreter built from patterns.

It exists for three reasons: the service works without an API key, tests get an
interpreter whose output never varies, and the evaluation gets a baseline to measure the
language model against.

It reads phases, statuses, year expressions, countries, study and intervention types, and
the cue phrases that signal an analysis ("per year", "distributed across", "vs",
"network"). It cannot tell a drug from a disease. Names it cannot place become `keywords`,
which ClinicalTrials.gov matches anywhere in a record. That is broader than a typed
filter, but it is never wrong about what kind of entity a name is.

Patterns run in a fixed order, and each one marks the text it matched as used, so later
patterns cannot reread it. Whatever is left at the end, minus filler words, is treated as
the names the question is about.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from trialviz.query.interpreters.base import (
    Interpretation,
    InterpreterIssue,
    IssueKind,
)
from trialviz.query.request import QueryRequest
from trialviz.vocabulary import (
    AnalysisKind,
    Dimension,
    InterventionType,
    Measure,
    OverallStatus,
    SortOrder,
    SponsorClass,
    StudyType,
    TimeField,
    TimeGranularity,
    scan_countries,
)
from trialviz.vocabulary._parsing import fold_preserving_length
from trialviz.vocabulary.trials import parse_phase


class RuleBasedInterpreter:
    name = "rules"

    async def interpret(self, request: QueryRequest, today: date) -> Interpretation:
        return interpret_question(request, today)


def interpret_question(request: QueryRequest, today: date) -> Interpretation:
    scanner = _Scanner(request.query)
    reading = _Reading()

    issue = _read_references(scanner, request)
    _read_years(scanner, reading, today)
    _read_time_field(scanner, reading)
    _read_phases(scanner, reading)
    _read_statuses(scanner, reading)
    _read_trial_types(scanner, reading)
    _read_countries(scanner, reading)
    _read_analysis(scanner, reading)
    _read_names(scanner, reading, request)

    return Interpretation(
        interpreter=RuleBasedInterpreter.name, proposal=reading.proposal(), issue=issue
    )


@dataclass
class _Reading:
    """What the patterns found, accumulated section by section."""

    filters: dict[str, object] = field(default_factory=dict)
    analysis: dict[str, object] = field(default_factory=dict)
    presentation: dict[str, object] = field(default_factory=dict)
    comparing: bool = False

    def proposal(self) -> dict[str, dict[str, object]]:
        return {
            "filters": self.filters,
            "analysis": self.analysis,
            "presentation": self.presentation,
        }


class _Scanner:
    """Matches patterns against a folded copy of the question and marks used text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.folded = fold_preserving_length(text)
        self._used = [False] * len(text)

    def take(self, pattern: re.Pattern[str]) -> list[re.Match[str]]:
        """Every match in unused text, marked as used."""
        matches = [
            m for m in pattern.finditer(self.folded) if not self._overlaps(m.start(), m.end())
        ]
        for match in matches:
            self.mark(match.start(), match.end())
        return matches

    def has(self, pattern: re.Pattern[str]) -> bool:
        """Whether the pattern matches unused text, without marking it."""
        return any(not self._overlaps(m.start(), m.end()) for m in pattern.finditer(self.folded))

    def mark(self, start: int, end: int) -> None:
        self._used[start:end] = [True] * (end - start)

    def is_free(self, start: int, end: int) -> bool:
        return not self._overlaps(start, end)

    def leftover_phrases(self) -> list[str]:
        """Runs of unused, non-filler words, in their original spelling."""
        phrases: list[str] = []
        start = end = None
        for token in _TOKEN.finditer(self.folded):
            gap = self.folded[end : token.start()] if end is not None else ""
            breaks = (
                self._overlaps(token.start(), token.end())
                or token.group() in _FILLER_WORDS
                or bool(_PHRASE_BREAK.search(gap))
                or (end is not None and self._overlaps(end, token.start()))
            )
            if breaks and start is not None and end is not None:
                phrases.append(self.text[start:end])
                start = end = None
            if not (self._overlaps(token.start(), token.end()) or token.group() in _FILLER_WORDS):
                start = token.start() if start is None else start
                end = token.end()
        if start is not None and end is not None:
            phrases.append(self.text[start:end])
        return [phrase for phrase in phrases if len(phrase) > 1 and not phrase.isdigit()]

    def _overlaps(self, start: int, end: int) -> bool:
        return any(self._used[start:end])


def _pattern(regex: str) -> re.Pattern[str]:
    return re.compile(regex, re.VERBOSE)


# --- References: "this drug" with nothing to point at -----------------------------------

_REFERENCE = _pattern(
    r"\b(?:this|that|these|those|the\ same|said|the\ given|the\ specified)\s+"
    r"(?P<noun>drugs?|medications?|treatments?|therap(?:y|ies)|interventions?"
    r"|conditions?|diseases?|indications?|sponsors?|compan(?:y|ies)|countr(?:y|ies))\b"
)

_REFERENCE_TARGETS = {
    "drug": ("interventions", "drug_name"),
    "medication": ("interventions", "drug_name"),
    "treatment": ("interventions", "drug_name"),
    "therap": ("interventions", "drug_name"),
    "intervention": ("interventions", "drug_name"),
    "condition": ("conditions", "condition"),
    "disease": ("conditions", "condition"),
    "indication": ("conditions", "condition"),
    "sponsor": ("sponsors", "sponsor"),
    "compan": ("sponsors", "sponsor"),
    "countr": ("countries", "country"),
}


def _read_references(scanner: _Scanner, request: QueryRequest) -> InterpreterIssue | None:
    for match in scanner.take(_REFERENCE):
        noun = match.group("noun")
        target, shorthand = next(
            t for stem, t in _REFERENCE_TARGETS.items() if noun.startswith(stem)
        )
        if getattr(request.filters, target) is None:
            phrase = scanner.text[match.start() : match.end()]
            return InterpreterIssue(
                kind=IssueKind.AMBIGUOUS_REFERENCE,
                message=f'The question refers to "{phrase}" without naming it. Name it in the '
                f"question, or set filters.{target} (or {shorthand}).",
            )
    return None


# --- Years ------------------------------------------------------------------------------

_NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20,
}  # fmt: skip
_YEAR = r"(19\d{2}|20\d{2})"

_YEAR_RANGE = _pattern(
    rf"\b(?:between\ {_YEAR}\ and\ {_YEAR}|from\ {_YEAR}\ (?:to|through|until|till)\ {_YEAR}"
    rf"|{_YEAR}\s*(?:-|\u2013|to|through)\s*{_YEAR})\b"
)
_YEAR_FROM = _pattern(
    rf"\b(?P<cue>since|from|starting(?:\ in)?|beginning(?:\ in)?|after|later\ than)\ {_YEAR}\b"
)
_YEAR_TO = _pattern(rf"\b(?P<cue>before|prior\ to|earlier\ than|until|through|up\ to)\ {_YEAR}\b")
_YEAR_IN = _pattern(rf"\bin\ {_YEAR}\b")
_RECENT_YEARS = _pattern(
    r"\b(?:in\ )?(?:the\ )?(?:last|past|previous)\ (?:(?P<count>\d{1,2}|"
    + "|".join(_NUMBER_WORDS)
    + r")\ years|(?P<decade>decade))\b"
)


def _read_years(scanner: _Scanner, reading: _Reading, today: date) -> None:
    """Read year bounds. "Since 2015" includes 2015, "after 2015" starts at 2016, and
    "the last N years" means the N calendar years ending with the current one."""
    for match in scanner.take(_YEAR_RANGE):
        low, high = sorted(int(year) for year in match.groups() if year)
        reading.filters.update(start_year_from=low, start_year_to=high)
    for match in scanner.take(_RECENT_YEARS):
        count = match.group("count")
        span = 10 if match.group("decade") else int(_NUMBER_WORDS.get(count, count))
        reading.filters.update(start_year_from=today.year - span + 1, start_year_to=today.year)
    for match in scanner.take(_YEAR_FROM):
        year = int(match.group(2))
        exclusive = match.group("cue") in {"after", "later than"}
        reading.filters["start_year_from"] = year + 1 if exclusive else year
    for match in scanner.take(_YEAR_TO):
        year = int(match.group(2))
        exclusive = match.group("cue") in {"before", "prior to", "earlier than"}
        reading.filters["start_year_to"] = year - 1 if exclusive else year
    for match in scanner.take(_YEAR_IN):
        reading.filters.update(
            start_year_from=int(match.group(1)), start_year_to=int(match.group(1))
        )


# --- Time field: must run before statuses so "completed per year" is not a status --------

_COMPLETION_TIME = _pattern(
    r"\b(?:completed|finished|ended|completing|completion)\s+(?:per|each|every|by|in\ each)\s+"
    r"(?:year|month)\b|\bcompletion\ (?:dates?|years?)\b"
)


def _read_time_field(scanner: _Scanner, reading: _Reading) -> None:
    for match in scanner.take(_COMPLETION_TIME):
        reading.analysis["time_field"] = TimeField.COMPLETION_DATE
        if "month" in match.group():
            reading.analysis["granularity"] = TimeGranularity.MONTH
        reading.analysis["kind"] = AnalysisKind.TIME_SERIES


# --- Phases -----------------------------------------------------------------------------

_NUMERAL = r"(?:[0-4]|iv|iii|ii|i)(?![\w])"
_PHASES = _pattern(
    rf"\b(?:early\ phase\ (?:1|i)(?![\w])|phases?\s*{_NUMERAL}"
    rf"(?:\s*(?:/|-|,|and|or|&)\s*(?:phase\s*)?{_NUMERAL})*)"
)
_PHASE_NUMERAL = re.compile(rf"(?<![\w])(?:early\ phase\ )?{_NUMERAL}", re.VERBOSE)


def _read_phases(scanner: _Scanner, reading: _Reading) -> None:
    phases = []
    for match in scanner.take(_PHASES):
        text = match.group()
        if text.startswith("early"):
            phases.append("EARLY_PHASE1")
            continue
        phases.extend(parse_phase(f"phase {n}").value for n in _PHASE_NUMERAL.findall(text[5:]))
    if phases:
        reading.filters["phases"] = list(dict.fromkeys(phases))


# --- Statuses, trial types, sponsor classes ---------------------------------------------

_ONGOING = (
    OverallStatus.NOT_YET_RECRUITING,
    OverallStatus.RECRUITING,
    OverallStatus.ENROLLING_BY_INVITATION,
    OverallStatus.ACTIVE_NOT_RECRUITING,
)

# Longer phrases first, so "not yet recruiting" is consumed before "recruiting" could be.
_STATUS_PHRASES: list[tuple[str, tuple[OverallStatus, ...]]] = [
    (r"not\ yet\ recruiting", (OverallStatus.NOT_YET_RECRUITING,)),
    (r"active,?\ not\ recruiting", (OverallStatus.ACTIVE_NOT_RECRUITING,)),
    (r"enrolling\ by\ invitation", (OverallStatus.ENROLLING_BY_INVITATION,)),
    # A negation names no single status, so it is consumed without producing one.
    (r"(?:not|no\ longer)\ recruiting", ()),
    (r"(?:currently\ )?recruiting", (OverallStatus.RECRUITING,)),
    # "Ongoing" covers four statuses. "Active" only counts directly before "trials", since
    # "the most active sponsors" is a grouping, not a status.
    (r"ongoing|active(?=\s+(?:trials?|studies))", _ONGOING),
    (r"completed", (OverallStatus.COMPLETED,)),
    (r"terminated", (OverallStatus.TERMINATED,)),
    (r"withdrawn", (OverallStatus.WITHDRAWN,)),
    (r"suspended", (OverallStatus.SUSPENDED,)),
]


def _read_statuses(scanner: _Scanner, reading: _Reading) -> None:
    statuses = [
        status
        for phrase, phrase_statuses in _STATUS_PHRASES
        if scanner.take(_pattern(rf"\b{phrase}\b"))
        for status in phrase_statuses
    ]
    if statuses:
        reading.filters["statuses"] = list(dict.fromkeys(statuses))


_STUDY_TYPES = [
    (r"observational", StudyType.OBSERVATIONAL),
    (r"interventional", StudyType.INTERVENTIONAL),
    (r"expanded\ access", StudyType.EXPANDED_ACCESS),
]
_INTERVENTION_TYPES = [
    (r"device", InterventionType.DEVICE),
    (r"behaviou?ral", InterventionType.BEHAVIORAL),
    (r"radiation|radiotherapy", InterventionType.RADIATION),
    (r"dietary\ supplement", InterventionType.DIETARY_SUPPLEMENT),
    (r"surgical|procedural", InterventionType.PROCEDURE),
    (r"gene\ therapy|genetic", InterventionType.GENETIC),
    (r"diagnostic", InterventionType.DIAGNOSTIC_TEST),
]
_INDUSTRY_SPONSORED = _pattern(
    r"\bindustry[-\s](?:sponsored|funded|backed)\b|\b(?:sponsored|funded)\ by\ industry\b"
)
_NIH_FUNDED = _pattern(
    r"\bnih[-\s](?:sponsored|funded)\b|\b(?:sponsored|funded)\ by\ (?:the\ )?nih\b"
)


def _read_trial_types(scanner: _Scanner, reading: _Reading) -> None:
    study_types = [kind for word, kind in _STUDY_TYPES if scanner.take(_pattern(rf"\b{word}\b"))]
    if study_types:
        reading.filters["study_types"] = study_types

    trial_noun = r"(?:\s+(?:therapy|therapies|interventions?))?\s+(?:trials?|studies)\b"
    intervention_types = [
        kind
        for word, kind in _INTERVENTION_TYPES
        if scanner.take(_pattern(rf"\b(?:{word}){trial_noun}"))
    ]
    if intervention_types:
        reading.filters["intervention_types"] = intervention_types

    sponsor_classes = []
    if scanner.take(_INDUSTRY_SPONSORED):
        sponsor_classes.append(SponsorClass.INDUSTRY)
    if scanner.take(_NIH_FUNDED):
        sponsor_classes.append(SponsorClass.NIH)
    if sponsor_classes:
        reading.filters["sponsor_classes"] = sponsor_classes


def _read_countries(scanner: _Scanner, reading: _Reading) -> None:
    countries = []
    for start, end, name in scan_countries(scanner.text):
        if scanner.is_free(start, end):
            scanner.mark(start, end)
            countries.append(name)
    if countries:
        reading.filters["countries"] = list(dict.fromkeys(countries))


# --- Analysis: kind, dimensions, measures -----------------------------------------------

# Longer phrases come first so "sponsor class" is read before "sponsor".
_DIMENSION_WORDS: list[tuple[str, Dimension]] = [
    (
        r"sponsor\ (?:class(?:es)?|categor(?:y|ies)|types?)|types?\ of\ sponsors?|funder\ types?",
        Dimension.SPONSOR_CLASS,
    ),
    (
        r"intervention\ types?|types?\ of\ interventions?|modalit(?:y|ies)",
        Dimension.INTERVENTION_TYPE,
    ),
    (r"study\ types?|types?\ of\ stud(?:y|ies)", Dimension.STUDY_TYPE),
    (r"(?:trial\ )?phases?", Dimension.PHASE),
    (r"(?:recruitment\ |overall\ )?status(?:es)?", Dimension.STATUS),
    (
        r"(?:lead\ )?sponsors?|compan(?:y|ies)|funders?|pharma(?:ceutical)?\ companies",
        Dimension.LEAD_SPONSOR,
    ),
    (r"drugs?|interventions?|treatments?|therap(?:y|ies)|medications?", Dimension.INTERVENTION),
    (r"conditions?|diseases?|indications?", Dimension.CONDITION),
    (r"countr(?:y|ies)|nations?|geograph(?:y|ies)", Dimension.COUNTRY),
    (r"start\ years?", Dimension.START_YEAR),
]
_DIMENSION = "(?P<dimension>" + "|".join(words for words, _ in _DIMENSION_WORDS) + r")\b"

# Phrasings that ask for results grouped by a dimension. Each is its own pattern because a
# regex cannot reuse the `dimension` group name across alternatives.
_GROUPINGS = [
    _pattern(
        rf"\b(?:by|across|per|among|between)\s+(?:the\s+|their\s+|different\s+|each\s+)*{_DIMENSION}"
    ),
    _pattern(rf"\b(?:which|what)\s+(?:are\s+the\s+)?(?:\w+\s+)?{_DIMENSION}"),
    _pattern(rf"\b(?:most|least)\s+(?:common|frequent|popular|active|prolific)\s+{_DIMENSION}"),
    _pattern(rf"\btop\s+(?:\d+\s+)?{_DIMENSION}"),
    _pattern(rf"\b(?:compare|comparing)\s+(?:the\s+)?{_DIMENSION}"),
    _pattern(rf"\b{_DIMENSION}\s+(?:distribution|breakdown|split|mix)\b"),
    _pattern(rf"\b(?:breakdown|distribution)\s+(?:of\s+\w+\s+)?(?:by|across|of)\s+{_DIMENSION}"),
]

_TIME_CUE = _pattern(
    r"\b(?:per|each|every|by|a)\s+(?P<unit>year|month)\b|\bover\ (?:time|the\ (?:years|months))\b"
    r"|\btrends?\b|\btrending\b|\b(?P<adverb>annual(?:ly)?|yearly|monthly)\b"
    r"|\byear[-\s]over[-\s]year\b|\btimeline\b"
)
# "How has X changed" asks for a trend. Only the opening words are consumed, so the subject
# between them stays available as a name.
_CHANGE_CUE = _pattern(r"\bhow\ (?:has|have)\b(?=.*\bchang(?:e|ed)\b)")
_NETWORK_CUE = _pattern(
    r"\bnetworks?\b|\bco-?occur\w*|↔|<->|\bconnections?\ between\b|\bgraph\ of\b"
)
_COMBINATION_CUE = _pattern(r"\bcombinations?\b|\bcombined\ with\b|\bin\ combination\b")
_COMPARE_CUE = _pattern(r"\b(?:compare|comparing|comparison|compared)\b|\bvs\b\.?|\bversus\b")
_COMPARE_SEPARATOR = _pattern(r"\bvs\b\.?|\bversus\b|\bcompared\ (?:to|with)\b|\band\b|\bor\b")
_SCATTER_CUE = _pattern(r"\bvs\b|\bversus\b|\bagainst\b|\brelationship\b|\bcorrelat\w*|\bscatter\b")
_HISTOGRAM_CUE = _pattern(
    r"\bdistribution\b|\bhistogram\b|\bspread\b|\brange\b|\bsizes\b|\btypical\b"
    r"|\bhow\ (?:large|big|long)\b"
)
_COUNT_CUE = _pattern(r"\bhow\ many\b|\bnumber\ of\b|\bcount\b|\btotal\b")
_RANKING_CUE = _pattern(r"\b(?:most|top|leading|largest|biggest|highest)\b")
_TOP_N = _pattern(r"\btop\s+(?P<n>\d{1,3})\b")

_MEASURE_WORDS: list[tuple[str, Measure]] = [
    (
        r"number\ of\ (?:sites|locations)|site\ counts?|how\ many\ sites|sites\ per\ trial",
        Measure.SITE_COUNT,
    ),
    (r"enrol(?:l)?ments?|participants?|sample\ sizes?|patients\ enrolled", Measure.ENROLLMENT),
    (r"durations?|how\ long|length", Measure.DURATION_MONTHS),
]

_NODE_WORDS: list[tuple[str, Dimension]] = [
    (r"(?:lead\ )?sponsors?|compan(?:y|ies)|pharma\w*", Dimension.LEAD_SPONSOR),
    (
        r"drugs?|interventions?|treatments?|therap(?:y|ies)|medications?|compounds?",
        Dimension.INTERVENTION,
    ),
    (r"conditions?|diseases?|indications?", Dimension.CONDITION),
    (r"countr(?:y|ies)|nations?", Dimension.COUNTRY),
]


def _dimension_named(word: str) -> Dimension:
    return next(dim for words, dim in _DIMENSION_WORDS if re.fullmatch(words, word, re.VERBOSE))


def _read_analysis(scanner: _Scanner, reading: _Reading) -> None:
    """Pick the analysis kind from cue phrases, most specific first:
    network, scatter, histogram, comparison, time series, distribution, count."""
    # Read without consuming: "top 10 sponsors" is also the grouping phrase.
    if top := _TOP_N.search(scanner.folded):
        reading.presentation["top_n"] = int(top.group("n"))
    ranked = scanner.has(_RANKING_CUE)

    if scanner.take(_NETWORK_CUE):
        _read_network(scanner, reading)
        return

    measures = _take_in_order(scanner, _MEASURE_WORDS)
    if len(measures) >= 2 and scanner.take(_SCATTER_CUE):
        reading.analysis.update(kind=AnalysisKind.SCATTER, x=measures[0], y=measures[1])
        return
    if measures and scanner.take(_HISTOGRAM_CUE):
        reading.analysis.update(kind=AnalysisKind.HISTOGRAM, measure=measures[0])
        return

    # Checked before groupings, because "compare phases" is read as a grouping and
    # consumes the word "compare".
    comparing = scanner.has(_COMPARE_CUE)
    timed = _read_time_cue(scanner, reading)
    groupings = _read_groupings(scanner)

    if comparing:
        reading.comparing = True
        scanner.take(_COMPARE_CUE)
        scanner.take(_COMPARE_SEPARATOR)
        reading.analysis["kind"] = AnalysisKind.COMPARISON
        if groupings:
            reading.analysis["group_by"] = groupings[0]
        elif timed:
            reading.analysis["group_by"] = Dimension.START_YEAR
        reading.analysis.pop("granularity", None)
        return

    if timed:
        reading.analysis.setdefault("kind", AnalysisKind.TIME_SERIES)
        if groupings:
            reading.analysis["series_by"] = groupings[0]
        return

    if groupings:
        reading.analysis.update(kind=AnalysisKind.DISTRIBUTION, group_by=groupings[0])
        if len(groupings) > 1:
            reading.analysis["series_by"] = groupings[1]
        if ranked:
            reading.presentation["sort"] = SortOrder.VALUE_DESC
        return

    if scanner.take(_COUNT_CUE):
        reading.analysis["kind"] = AnalysisKind.COUNT


def _read_groupings(scanner: _Scanner) -> list[Dimension]:
    """Grouping dimensions in the order the question mentions them."""
    matches = [match for pattern in _GROUPINGS for match in scanner.take(pattern)]
    matches.sort(key=lambda match: match.start())
    return list(dict.fromkeys(_dimension_named(m.group("dimension")) for m in matches))


def _read_time_cue(scanner: _Scanner, reading: _Reading) -> bool:
    matches = scanner.take(_TIME_CUE)
    changed = bool(scanner.take(_CHANGE_CUE))
    if not (matches or changed) and reading.analysis.get("kind") is not AnalysisKind.TIME_SERIES:
        return False
    if any(m.group("unit") == "month" or m.group("adverb") == "monthly" for m in matches):
        reading.analysis["granularity"] = TimeGranularity.MONTH
    return True


def _read_network(scanner: _Scanner, reading: _Reading) -> None:
    nodes = _take_in_order(scanner, _NODE_WORDS)
    if scanner.take(_COMBINATION_CUE) and not nodes:
        nodes = [Dimension.INTERVENTION]
    if not nodes:
        reading.analysis["kind"] = AnalysisKind.NETWORK
        return
    pair = (nodes[0], nodes[1]) if len(nodes) > 1 else (nodes[0], nodes[0])
    reading.analysis.update(kind=AnalysisKind.NETWORK, node_types=list(pair))


def _take_in_order[T](scanner: _Scanner, vocabulary: Iterable[tuple[str, T]]) -> list[T]:
    """Distinct values whose words appear in the question, in order of appearance."""
    found: list[tuple[int, T]] = []
    for words, value in vocabulary:
        found.extend((m.start(), value) for m in scanner.take(_pattern(rf"\b(?:{words})\b")))
    return list(dict.fromkeys(value for _, value in sorted(found, key=lambda item: item[0])))


# --- Names: whatever is left -----------------------------------------------------------

_TOKEN = re.compile(r"\w+(?:[-+/.']\w+)*")
_PHRASE_BREAK = re.compile(r"[,;:?!()\[\]]")


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Words that carry no name. Condition and therapy words ("disease", "therapy") are absent on
# purpose: they are often part of a name, as in "Crohn's disease" or "hormone therapy".
_FILLER_WORDS = _words(
    """
    a about across after against all also among an and any are as at be been being between
    both but by can could did do does done during each either every for from give had has
    have how i in into is it its me more most much my of on only or other our over per
    please show so some than that the their them then there these they this those through
    to too up us very was we were what when where which while who whom whose why will with
    within would you your
    trial trials study studies clinical registered register registry listed conducted
    running run ongoing current currently started start starting began begun new recent
    recently number count counts total amount many several various different
    involving involve involves testing tested test evaluating evaluate investigating using
    use used targeting target treating treat assess assessing compare comparing comparison
    chart graph plot visualize visualise display list breakdown distribution distributed
    trend trends change changed changes changing time year years month months annual
    common frequent frequently popular top highest largest biggest leading least
    patients people participants sponsored funded based located
    drug drugs intervention interventions medication medications
    """
)


def _read_names(scanner: _Scanner, reading: _Reading, request: QueryRequest) -> None:
    """Treat leftover phrases as names. Two or more while comparing become cohorts."""
    known = {
        name.casefold()
        for names in (
            request.filters.interventions,
            request.filters.conditions,
            request.filters.sponsors,
        )
        for name in names or ()
    }
    phrases = [p for p in scanner.leftover_phrases() if p.casefold() not in known]
    phrases = list(dict.fromkeys(phrases))
    if reading.comparing and len(phrases) >= 2:
        reading.analysis["cohorts"] = [
            {"label": phrase, "filters": {"keywords": [phrase]}} for phrase in phrases
        ]
    elif phrases:
        reading.filters["keywords"] = phrases
