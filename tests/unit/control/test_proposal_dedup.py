"""Tests for lexical near-duplicate scoring of tech-lead proposals (#6878)."""

from __future__ import annotations

from issue_orchestrator.control.proposal_dedup import (
    DuplicateMatch,
    OpenIssueRef,
    find_duplicate,
    similarity,
)


class TestSimilarity:
    def test_identical_issue_scores_one(self):
        assert similarity("Fix flaky test", "It times out", "Fix flaky test", "It times out") == 1.0

    def test_disjoint_vocabulary_scores_zero(self):
        assert similarity("alpha beta", "gamma", "delta epsilon", "zeta") == 0.0

    def test_symmetric(self):
        a = similarity("reset retry loop", "scratch", "retry loop scratch reset", "")
        b = similarity("retry loop scratch reset", "", "reset retry loop", "scratch")
        assert a == b

    def test_reworded_same_vocabulary_is_a_strong_match(self):
        # The common accidental re-file: same words, shuffled.
        score = similarity(
            "Flaky sandbox OS boundary test times out under load",
            "The sandbox timing test is flaky under parallel load",
            "Sandbox OS boundary test is flaky and times out",
            "Fails under load when run in parallel",
        )
        assert score > 0.5

    def test_unrelated_issues_score_low(self):
        score = similarity(
            "Add retry cap to the publish gate",
            "Cap outstanding retries",
            "Render provider circuit status in the dashboard",
            "Show an outage banner",
        )
        assert score < 0.2

    def test_pure_paraphrase_is_missed_documented_lexical_limitation(self):
        # Same MEANING, disjoint words -> lexical scoring cannot see it. This is
        # WHY the agent owns the semantic call; the lexical layer only backstops.
        score = similarity(
            "Reset the retry loop from scratch", "",
            "Restart failed attempts cleanly", "",
        )
        assert score < 0.2

    def test_boilerplate_and_stopwords_do_not_manufacture_similarity(self):
        # Two unrelated proposals sharing only template scaffolding score ~0.
        tmpl = "## Problem\n\nWe should fix this. ## Acceptance criteria\n- [ ] done"
        score = similarity(
            "Widget alignment", tmpl + " widget alignment drifts",
            "Gadget latency", tmpl + " gadget latency spikes",
        )
        assert score < 0.2

    def test_shared_title_term_outweighs_shared_body_term(self):
        proposal_t, proposal_b = "widget", "gadget"
        via_title = similarity(proposal_t, proposal_b, "widget", "zzzz")
        via_body = similarity(proposal_t, proposal_b, "zzzz", "gadget")
        assert via_title > via_body


class TestFindDuplicate:
    def _corpus(self) -> list[OpenIssueRef]:
        return [
            OpenIssueRef(10, "Flaky sandbox OS boundary test times out", "under parallel load"),
            OpenIssueRef(20, "Add retry cap to publish gate", "cap outstanding retries"),
            OpenIssueRef(30, "Render provider circuit in dashboard", "outage banner"),
        ]

    def test_returns_best_match_above_threshold(self):
        match = find_duplicate(
            "Fix flaky sandbox OS boundary test that times out",
            "flaky under load",
            self._corpus(),
            threshold=0.3,
        )
        assert match is not None
        assert match.number == 10
        assert match.score >= 0.3

    def test_returns_none_when_nothing_clears_threshold(self):
        assert (
            find_duplicate("Totally novel unrelated concept xyzzy", "plugh", self._corpus(), threshold=0.3)
            is None
        )

    def test_threshold_is_respected(self):
        # A weak-but-nonzero overlap is suppressed by a high threshold.
        assert find_duplicate("retry", "", self._corpus(), threshold=0.99) is None

    def test_empty_corpus_returns_none(self):
        assert find_duplicate("anything", "", [], threshold=0.1) is None

    def test_empty_proposal_never_matches(self):
        # No substantive terms -> not comparable -> never a spurious dedup.
        assert find_duplicate("", "", self._corpus(), threshold=0.0) is None

    def test_ties_break_to_lowest_issue_number(self):
        corpus = [
            OpenIssueRef(50, "alpha beta gamma", ""),
            OpenIssueRef(40, "alpha beta gamma", ""),
        ]
        match = find_duplicate("alpha beta gamma", "", corpus, threshold=0.5)
        assert match == DuplicateMatch(number=40, score=1.0)
