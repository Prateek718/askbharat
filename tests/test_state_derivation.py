"""Reading a scheme's jurisdiction off the harvested page.

myScheme's search API has a `beneficiaryState` field and returns it null on
every one of the 4,810 rows, so the catalogue cannot say which state a scheme
belongs to. The detail page can: there is a chip beside the title carrying the
state, and central schemes simply do not have one.

That absence is load-bearing — it is the only thing distinguishing a central
scheme from a state one — so the cases below pin down exactly what counts as a
chip and what does not. The layouts are real, taken from the corpus.
"""
from __future__ import annotations

import pytest

from askbharat.scripts.backfill_states import (
    CENTRAL,
    STATES,
    canonicalise,
    state_from_page,
)


def page(title: str, *lines: str) -> dict:
    """A page whose text is the boilerplate every myScheme page carries,
    then the block under test."""
    body = ["Back", "Apply Now", "Check Eligibility", *lines, "Details", "Benefits"]
    return {"title": title, "text": "\n".join(body)}


class TestVocabulary:
    def test_covers_every_state_and_union_territory(self):
        # 28 states + 8 UTs. A short list here means a citizen whose state is
        # missing silently gets filed as a central scheme.
        assert len(STATES) == 36

    def test_names_are_unique(self):
        assert len(set(STATES)) == len(STATES)


class TestCanonicalise:
    def test_exact_name_passes_through(self):
        assert canonicalise("Kerala") == "Kerala"

    def test_narrow_no_break_space_is_folded(self):
        # The extraction picked this up off the rendered page for 17 Tamil Nadu
        # schemes; untreated it is a 37th "state" that no filter option matches.
        assert canonicalise("Tamil Nadu") == "Tamil Nadu"

    def test_ampersand_and_the_word_and_are_the_same(self):
        expected = "Dadra & Nagar Haveli and Daman & Diu"
        for variant in (
            "Dadra & Nagar Haveli and Daman & Diu",
            "Dadra and Nagar Haveli and Daman and Diu",
            "Dadra & Nagar Haveli & Daman & Diu",
            "The Dadra And Nagar Haveli And Daman And Diu",
        ):
            assert canonicalise(variant) == expected

    def test_historic_names_map_to_current_ones(self):
        assert canonicalise("Pondicherry") == "Puducherry"
        assert canonicalise("Orissa") == "Odisha"

    def test_case_and_whitespace_are_insensitive(self):
        assert canonicalise("  west   BENGAL ") == "West Bengal"

    @pytest.mark.parametrize("value", [None, "", "Ministry of Education", "Farmer"])
    def test_non_states_return_none(self, value):
        assert canonicalise(value) is None

    def test_a_state_named_inside_prose_is_not_a_match(self):
        # Substring matching would file this under Kerala. It is a title.
        assert canonicalise("Kerala State Nirmithi Kendra Housing Grant") is None


class TestStateFromPage:
    def test_chip_before_the_title(self):
        p = page("Mhaji Bus Scheme", "Goa", "Mhaji Bus Scheme", "Bus", "Transport")
        assert state_from_page(p) == "Goa"

    def test_chip_after_the_title(self):
        p = page(
            "Financial Assistance to Revive Goan Maand Culture",
            "Financial Assistance to Revive Goan Maand Culture",
            "Goa",
        )
        assert state_from_page(p) == "Goa"

    def test_no_chip_means_central(self):
        # Tags butt straight up against the title.
        p = page("Prime Minister's Research Fellowship",
                 "Prime Minister's Research Fellowship", "Fellowship", "Student")
        assert state_from_page(p) == CENTRAL

    def test_ministry_beside_the_title_is_not_a_state(self):
        p = page("NEC Merit Scholarship", "NEC Merit Scholarship",
                 "Ministry Of Development Of North Eastern Region")
        assert state_from_page(p) == CENTRAL

    def test_a_state_further_down_is_ignored(self):
        # The regression this guards: a wider window read tags several lines
        # below the title and filed three schemes under unrelated states. Here
        # the scheme is central and 'Goa' is a tag, not the chip.
        p = page("National Handloom Award", "National Handloom Award",
                 "Award", "Weaver", "Goa")
        assert state_from_page(p) == CENTRAL

    def test_untitled_page_is_unknown_not_central(self):
        assert state_from_page({"title": "", "text": "Goa\nsomething"}) is None

    def test_title_absent_from_body_is_unknown_not_central(self):
        # A layout this does not understand. Guessing CENTRAL here would file
        # unread schemes as nationwide, which is a wrong answer rather than no
        # answer.
        p = {"title": "Some Scheme", "text": "Back\nApply Now\nDetails"}
        assert state_from_page(p) is None
