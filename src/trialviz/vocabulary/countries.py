"""Country names as ClinicalTrials.gov records them, plus the aliases people actually type.

`COUNTRIES` is the full set of distinct `LocationCountry` values in the registry, taken from
`/api/v2/stats/field/values?fields=LocationCountry` on 2026-11-07. Values are kept byte for
byte, including source quirks such as the trailing space in "Bonaire, Saint Eustatius and
Saba ", because API filters match the stored string.
"""

from __future__ import annotations

import re

from trialviz.vocabulary._parsing import closest_matches, comparison_key, fold_preserving_length

COUNTRIES: tuple[str, ...] = (
    "Afghanistan",
    "Aland Islands",
    "Albania",
    "Algeria",
    "American Samoa",
    "Andorra",
    "Angola",
    "Anguilla",
    "Antarctica",
    "Antigua and Barbuda",
    "Argentina",
    "Armenia",
    "Aruba",
    "Australia",
    "Austria",
    "Azerbaijan",
    "Bahrain",
    "Bangladesh",
    "Barbados",
    "Belarus",
    "Belgium",
    "Belize",
    "Benin",
    "Bermuda",
    "Bhutan",
    "Bolivia",
    "Bonaire, Saint Eustatius and Saba ",
    "Bosnia and Herzegovina",
    "Botswana",
    "Brazil",
    "Brunei",
    "Bulgaria",
    "Burkina Faso",
    "Burma",
    "Burundi",
    "Cabo Verde",
    "Cambodia",
    "Cameroon",
    "Canada",
    "Cayman Islands",
    "Central African Republic",
    "Chad",
    "Chile",
    "China",
    "Christmas Island",
    "Colombia",
    "Comoros",
    "Costa Rica",
    "Croatia",
    "Cuba",
    "Curacao",
    "Cyprus",
    "Czechia",
    "Côte d’Ivoire",
    "Democratic Republic of the Congo",
    "Denmark",
    "Djibouti",
    "Dominica",
    "Dominican Republic",
    "Ecuador",
    "Egypt",
    "El Salvador",
    "Equatorial Guinea",
    "Eritrea",
    "Estonia",
    "Eswatini",
    "Ethiopia",
    "Faroe Islands",
    "Federal Republic of Yugoslavia",
    "Fiji",
    "Finland",
    "France",
    "French Guiana",
    "French Polynesia",
    "French Southern and Antarctic Lands",
    "Gabon",
    "Georgia",
    "Germany",
    "Ghana",
    "Gibraltar",
    "Greece",
    "Greenland",
    "Grenada",
    "Guadeloupe",
    "Guam",
    "Guatemala",
    "Guinea",
    "Guinea-Bissau",
    "Guyana",
    "Haiti",
    "Holy See",
    "Honduras",
    "Hong Kong",
    "Hungary",
    "Iceland",
    "India",
    "Indonesia",
    "Iran",
    "Iraq",
    "Ireland",
    "Israel",
    "Italy",
    "Jamaica",
    "Japan",
    "Jersey",
    "Jordan",
    "Kazakhstan",
    "Kenya",
    "Kiribati",
    "Kosovo",
    "Kuwait",
    "Kyrgyzstan",
    "Laos",
    "Latvia",
    "Lebanon",
    "Lesotho",
    "Liberia",
    "Libya",
    "Liechtenstein",
    "Lithuania",
    "Luxembourg",
    "Macau",
    "Madagascar",
    "Malawi",
    "Malaysia",
    "Maldives",
    "Mali",
    "Malta",
    "Martinique",
    "Mauritania",
    "Mauritius",
    "Mayotte",
    "Mexico",
    "Micronesia",
    "Moldova",
    "Monaco",
    "Mongolia",
    "Montenegro",
    "Montserrat",
    "Morocco",
    "Mozambique",
    "Namibia",
    "Nepal",
    "Netherlands",
    "Netherlands Antilles",
    "New Caledonia",
    "New Zealand",
    "Nicaragua",
    "Niger",
    "Nigeria",
    "Niue",
    "North Korea",
    "North Macedonia",
    "Northern Mariana Islands",
    "Norway",
    "Oman",
    "Pakistan",
    "Palestinian Territories",
    "Panama",
    "Papua New Guinea",
    "Paraguay",
    "Peru",
    "Philippines",
    "Poland",
    "Portugal",
    "Puerto Rico",
    "Qatar",
    "Republic of the Congo",
    "Reunion",
    "Romania",
    "Russia",
    "Rwanda",
    "Saint Kitts and Nevis",
    "Saint Lucia",
    "Saint Martin",
    "Saint Vincent and the Grenadines",
    "Samoa",
    "San Marino",
    "Saudi Arabia",
    "Senegal",
    "Serbia",
    "Serbia and Montenegro",
    "Seychelles",
    "Sierra Leone",
    "Singapore",
    "Slovakia",
    "Slovenia",
    "Solomon Islands",
    "Somalia",
    "South Africa",
    "South Korea",
    "South Sudan",
    "Spain",
    "Sri Lanka",
    "Sudan",
    "Suriname",
    "Sweden",
    "Switzerland",
    "Syria",
    "Taiwan",
    "Tajikistan",
    "Tanzania",
    "Thailand",
    "The Bahamas",
    "The Gambia",
    "Timor-Leste",
    "Togo",
    "Trinidad and Tobago",
    "Tunisia",
    "Turkey (Türkiye)",
    "Turkmenistan",
    "Uganda",
    "Ukraine",
    "United Arab Emirates",
    "United Kingdom",
    "United States",
    "United States Minor Outlying Islands",
    "Uruguay",
    "Uzbekistan",
    "Vanuatu",
    "Venezuela",
    "Vietnam",
    "Virgin Islands",
    "Yemen",
    "Zambia",
    "Zimbabwe",
)

# Common alternative names mapped to the registry's spelling. The four UK nations map to
# "United Kingdom" because the registry does not record them separately.
_ALIASES: dict[str, str] = {
    "us": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "great britain": "United Kingdom",
    "britain": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "northern ireland": "United Kingdom",
    "south korea": "South Korea",
    "republic of korea": "South Korea",
    "korea, republic of": "South Korea",
    "dprk": "North Korea",
    "turkey": "Turkey (Türkiye)",
    "türkiye": "Turkey (Türkiye)",
    "russian federation": "Russia",
    "iran, islamic republic of": "Iran",
    "czech republic": "Czechia",
    "myanmar": "Burma",
    "ivory coast": "Côte d’Ivoire",
    "cote d'ivoire": "Côte d’Ivoire",
    "uae": "United Arab Emirates",
    "drc": "Democratic Republic of the Congo",
    "dr congo": "Democratic Republic of the Congo",
    "congo-kinshasa": "Democratic Republic of the Congo",
    "congo-brazzaville": "Republic of the Congo",
    "vatican": "Holy See",
    "vatican city": "Holy See",
    "macao": "Macau",
    "swaziland": "Eswatini",
    "macedonia": "North Macedonia",
    "palestine": "Palestinian Territories",
    "viet nam": "Vietnam",
    "lao pdr": "Laos",
    "holland": "Netherlands",
    "the netherlands": "Netherlands",
    "gambia": "The Gambia",
    "bahamas": "The Bahamas",
    "cape verde": "Cabo Verde",
    "east timor": "Timor-Leste",
    "prc": "China",
    "mainland china": "China",
}

# Names that refer to more than one registry country. Guessing would silently change the
# answer, so these fail with the candidates listed.
_AMBIGUOUS: dict[str, tuple[str, ...]] = {
    "korea": ("South Korea", "North Korea"),
    "congo": ("Democratic Republic of the Congo", "Republic of the Congo"),
}

_BY_KEY: dict[str, str] = {comparison_key(name): name for name in COUNTRIES} | {
    comparison_key(alias): name for alias, name in _ALIASES.items()
}
_AMBIGUOUS_BY_KEY = {comparison_key(name): options for name, options in _AMBIGUOUS.items()}


def parse_country(raw: object) -> str:
    """Return the registry's spelling for a country name or alias, or raise ValueError."""
    if not isinstance(raw, str):
        raise ValueError(f"country must be a string, got {type(raw).__name__}")
    key = comparison_key(raw)
    if key in _AMBIGUOUS_BY_KEY:
        options = " or ".join(_AMBIGUOUS_BY_KEY[key])
        raise ValueError(f"{raw!r} could mean {options}. Name the country exactly.")
    if key in _BY_KEY:
        return _BY_KEY[key]
    suggestions = list(dict.fromkeys(_BY_KEY[match] for match in closest_matches(key, _BY_KEY)))
    hint = f" Did you mean {' or '.join(suggestions)}?" if suggestions else ""
    raise ValueError(f"Unknown country {raw!r}.{hint}")


def scan_countries(text: str) -> list[tuple[int, int, str]]:
    """Find countries named in free text as (start, end, registry name), longest names first.

    Offsets index into `text`. Names that collide with ordinary English are skipped (see
    `_UNSAFE_FOR_SCANNING`) because a keyword scanner cannot tell which sense is meant;
    the language model interpreter handles those.
    """
    folded = fold_preserving_length(text)
    taken = [False] * len(text)
    found = []
    for pattern, name in _SCAN_PATTERNS:
        for match in pattern.finditer(folded):
            if any(taken[match.start() : match.end()]):
                continue
            taken[match.start() : match.end()] = [True] * (match.end() - match.start())
            found.append((match.start(), match.end(), name))
    return sorted(found)


# Each of these collides with ordinary English or a larger place name: Georgia and Jersey
# (US states, New Jersey), Chad and Jordan (first names), Guinea ("guinea pig"), "us" (the
# pronoun, an alias of United States) and Wales ("New South Wales").
_UNSAFE_FOR_SCANNING = frozenset(
    comparison_key(name)
    for name in ("Georgia", "Jersey", "Chad", "Jordan", "Guinea", "us", "Wales")
)

# One pattern per name, tolerant of any punctuation between words, longest names first so
# "Papua New Guinea" claims its words before "Guinea" could.
_SCAN_PATTERNS = [
    (re.compile(r"\b" + r"[\W_]+".join(map(re.escape, key.split())) + r"\b"), _BY_KEY[key])
    for key in sorted(_BY_KEY, key=len, reverse=True)
    if key not in _UNSAFE_FOR_SCANNING
]
