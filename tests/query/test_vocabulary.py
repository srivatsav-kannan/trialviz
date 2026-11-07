"""Parsing loose spellings into the registry's canonical values.

The interesting failures here are the quiet ones: a parser that maps "Korea" to one of two
countries, or reads "active" as one status when people mean four, produces a confident
wrong answer downstream. Those cases must fail loudly.
"""

from __future__ import annotations

import pytest

from trialviz.vocabulary import (
    COUNTRIES,
    InterventionType,
    OverallStatus,
    Phase,
    StudyType,
    parse_country,
    parse_phases,
    scan_countries,
)
from trialviz.vocabulary.analysis import Dimension, parse_dimension
from trialviz.vocabulary.trials import parse_intervention_type, parse_status, parse_study_type


class TestPhases:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (["Phase I/II"], [Phase.PHASE1, Phase.PHASE2]),
            (["phase ii/iii", "4"], [Phase.PHASE2, Phase.PHASE3, Phase.PHASE4]),
            (["1/2/3"], [Phase.PHASE1, Phase.PHASE2, Phase.PHASE3]),
            (["Early Phase 1", "Phase 0"], [Phase.EARLY_PHASE1]),
            (["N/A"], [Phase.NA]),
            (["PHASE3", "phase 3", "III", "Phase-3"], [Phase.PHASE3]),
            (["4", "1"], [Phase.PHASE1, Phase.PHASE4]),
        ],
    )
    def test_spellings_collapse_to_canonical_phases_in_phase_order(
        self, raw: list[str], expected: list[Phase]
    ) -> None:
        assert parse_phases(raw) == expected

    @pytest.mark.parametrize("raw", [["phase 5"], ["phase"], [""], ["1/5"], ["V"], [3]])
    def test_invalid_phases_are_rejected(self, raw: list[object]) -> None:
        with pytest.raises(ValueError):
            parse_phases(raw)

    def test_rejection_names_the_accepted_values(self) -> None:
        with pytest.raises(ValueError, match="Accepted values: EARLY_PHASE1, PHASE1"):
            parse_phases(["phase 5"])


class TestStatuses:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Active, not recruiting", OverallStatus.ACTIVE_NOT_RECRUITING),
            ("not-yet-recruiting", OverallStatus.NOT_YET_RECRUITING),
            ("ENROLLING_BY_INVITATION", OverallStatus.ENROLLING_BY_INVITATION),
        ],
    )
    def test_spellings(self, raw: str, expected: OverallStatus) -> None:
        assert parse_status(raw) == expected

    @pytest.mark.parametrize("raw", ["active", "ongoing", "open"])
    def test_words_that_mean_several_statuses_are_not_guessed(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_status(raw)


class TestCountries:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("USA", "United States"),
            ("u.s.a.", "United States"),
            ("United States of America", "United States"),
            ("England", "United Kingdom"),
            ("Türkiye", "Turkey (Türkiye)"),
            ("turkey", "Turkey (Türkiye)"),
            ("cote d'ivoire", "Côte d’Ivoire"),
            ("Myanmar", "Burma"),
            ("czech republic", "Czechia"),
        ],
    )
    def test_aliases_resolve_to_registry_spelling(self, raw: str, expected: str) -> None:
        assert parse_country(raw) == expected
        assert expected in COUNTRIES

    def test_registry_quirks_are_preserved_byte_for_byte(self) -> None:
        # The registry stores this name with a trailing space; filters must match it.
        assert parse_country("Bonaire, Saint Eustatius and Saba") == (
            "Bonaire, Saint Eustatius and Saba "
        )

    @pytest.mark.parametrize(
        ("raw", "candidates"),
        [
            ("Korea", ("South Korea", "North Korea")),
            ("Congo", ("Democratic Republic of the Congo", "Republic of the Congo")),
        ],
    )
    def test_ambiguous_names_fail_with_every_candidate(
        self, raw: str, candidates: tuple[str, ...]
    ) -> None:
        with pytest.raises(ValueError) as error:
            parse_country(raw)
        assert all(candidate in str(error.value) for candidate in candidates)

    def test_typos_get_a_suggestion(self) -> None:
        with pytest.raises(ValueError, match="Did you mean Germany"):
            parse_country("Germny")

    def test_every_registry_country_parses_to_itself(self) -> None:
        assert all(parse_country(name) == name for name in COUNTRIES)


class TestCountryScanning:
    def scan(self, text: str) -> list[str]:
        return [name for _, _, name in scan_countries(text)]

    def test_place_names_that_contain_a_country_are_not_misread(self) -> None:
        assert self.scan("sites in New Jersey and New South Wales") == []
        assert self.scan("Papua New Guinea") == ["Papua New Guinea"]
        assert self.scan("the Dominican Republic") == ["Dominican Republic"]

    def test_nigeria_and_niger_are_told_apart(self) -> None:
        assert self.scan("trials in Nigeria and Niger") == ["Nigeria", "Niger"]

    def test_names_that_collide_with_english_are_left_to_the_model(self) -> None:
        assert self.scan("Georgia, Jordan and Chad") == []
        assert self.scan("show us trials") == []

    def test_offsets_slice_the_original_spelling(self) -> None:
        text = "Recruiting in CÔTE D’IVOIRE and the U.S."
        spans = scan_countries(text)
        assert [text[start:end] for start, end, _ in spans] == ["CÔTE D’IVOIRE", "U.S"]


class TestOtherEnums:
    @pytest.mark.parametrize("member", list(InterventionType))
    def test_intervention_type_labels_parse_back(self, member: InterventionType) -> None:
        assert parse_intervention_type(member.label) == member

    @pytest.mark.parametrize("member", list(StudyType))
    def test_study_type_labels_parse_back(self, member: StudyType) -> None:
        assert parse_study_type(member.label) == member

    @pytest.mark.parametrize("member", list(OverallStatus))
    def test_status_labels_parse_back(self, member: OverallStatus) -> None:
        assert parse_status(member.label) == member

    def test_plurals_and_aliases(self) -> None:
        assert parse_intervention_type("devices") is InterventionType.DEVICE
        assert parse_dimension("sponsor") is Dimension.LEAD_SPONSOR
        assert parse_dimension("drugs") is Dimension.INTERVENTION

    def test_only_multi_valued_dimensions_can_link_to_themselves(self) -> None:
        multi = {dimension for dimension in Dimension if dimension.multi_valued}
        assert multi == {
            Dimension.INTERVENTION,
            Dimension.INTERVENTION_TYPE,
            Dimension.CONDITION,
            Dimension.COUNTRY,
        }
