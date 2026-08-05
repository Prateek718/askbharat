"""Reciprocal rank fusion, and the query tokeniser feeding lexical search.

Both are pure functions and both encode a decision that was made from measured
failures, so they are worth pinning:

- Fusion combines two rankers whose scores are on incompatible scales
  (`ts_rank` vs cosine distance). Position is the only comparable signal.
- The tokeniser exists because `plainto_tsquery` ANDs every term, which made
  every natural-sentence question return zero results.
"""
from __future__ import annotations

from askbharat.web.chat import _any_term_query, reciprocal_rank_fusion


class TestFusion:
    def test_agreement_between_rankers_wins(self):
        """Ranked well by both beats ranked first by one."""
        lexical = ["a", "shared", "b"]
        semantic = ["c", "shared", "d"]
        assert reciprocal_rank_fusion([lexical, semantic])[0] == "shared"

    def test_single_ranker_still_orders_correctly(self):
        assert reciprocal_rank_fusion([["a", "b", "c"]]) == ["a", "b", "c"]

    def test_empty_ranker_is_ignored_not_fatal(self):
        """Semantic search returns [] when the model or vectors are missing."""
        assert reciprocal_rank_fusion([["a", "b"], []]) == ["a", "b"]

    def test_all_empty_gives_empty(self):
        assert reciprocal_rank_fusion([[], []]) == []

    def test_every_candidate_survives_fusion(self):
        out = reciprocal_rank_fusion([["a", "b"], ["c", "d"]])
        assert set(out) == {"a", "b", "c", "d"}

    def test_damping_stops_rank_one_from_dominating(self):
        """With k=60, second place in both lists beats first in one."""
        out = reciprocal_rank_fusion([["solo", "both"], ["x", "both"]])
        assert out[0] == "both"

    def test_small_k_lets_rank_one_dominate(self):
        """Sanity check that k is doing the work, not the data."""
        out = reciprocal_rank_fusion([["solo", "both"], ["x", "both"]], k=0)
        assert out[0] == "solo"


class TestQueryTokeniser:
    def test_conversational_noise_is_dropped(self):
        terms = _any_term_query("I am a widow in Punjab, is there a pension for me?")
        assert set(terms.split(" | ")) == {"widow", "punjab", "pension"}

    def test_short_words_are_dropped(self):
        assert "in" not in _any_term_query("pension in up").split(" | ")

    def test_terms_are_or_ed_not_and_ed(self):
        """The whole point: ANDing returned zero rows for real questions."""
        assert " | " in _any_term_query("farmer bihar acres")

    def test_duplicates_collapse_keeping_order(self):
        assert _any_term_query("pension pension widow") == "pension | widow"

    def test_punctuation_cannot_inject_tsquery_operators(self):
        """Tokens are letters only, so the joined string is always safe."""
        out = _any_term_query("pension & widow | (scholarship) !x:*")
        assert set("&()!:*") .isdisjoint(out)

    def test_devanagari_yields_no_lexical_terms(self):
        """Hindi produces nothing here, and that is correct.

        Devanagari matras are combining marks, which are not `isalnum()`, so
        the letters-only pattern shatters each word into single-consonant
        fragments that the length filter then drops. Widening the pattern would
        not help: 0 of 4,810 catalogue titles contain Devanagari, so there is
        nothing for a Hindi lexeme to match. Hindi is served by the semantic
        retriever, which is precisely why the assistant needed one.
        """
        assert _any_term_query("मुझे छात्रवृत्ति चाहिए") == ""

    def test_a_query_of_pure_noise_yields_nothing(self):
        assert _any_term_query("what can I apply for?") == ""

    def test_empty_and_none_are_safe(self):
        assert _any_term_query("") == ""
        assert _any_term_query(None) == ""
