"""Tests for the deterministic ProposalDedupGate (#6878 B1-B3/A1).

The gate owns the whole dedup decision; these tests pin the typed outcome for
every combination of corpus state, candidate, grant, and authority, plus the
invariants that make the dangerous states unrepresentable.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.control.proposal_dedup import OpenIssueRef
from issue_orchestrator.control.proposal_dedup_gate import (
    CommentExisting,
    CorpusState,
    DedupAuthority,
    DuplicateTargetGrant,
    FileNew,
    GateSuspectedDuplicate,
    OpenIssueCorpus,
    ProposalIntent,
    RejectCandidate,
    classify_proposal,
)

_THRESHOLD = 0.72
_EXECUTE = DedupAuthority(create_issue_execute=True, post_comment_execute=True)
_MATCH = OpenIssueRef(1234, "Stabilize CI runner disconnects", "runner drops mid build")
_ALL_AUTHORITIES = [
    DedupAuthority(c, p) for c in (True, False) for p in (True, False)
]


def _intent(
    duplicate_of: int | None = None,
    title: str = "Stabilize CI runner disconnects",
    body: str = "runner drops mid build",
) -> ProposalIntent:
    return ProposalIntent(title=title, body=body, duplicate_of=duplicate_of)


def _ready(*issues: OpenIssueRef) -> OpenIssueCorpus:
    return OpenIssueCorpus.ready(issues or (_MATCH,))


def _classify(
    intent: ProposalIntent,
    corpus: OpenIssueCorpus,
    grant: DuplicateTargetGrant = DuplicateTargetGrant.whole_board(),
    authority: DedupAuthority = _EXECUTE,
):
    return classify_proposal(intent, corpus, grant, authority, threshold=_THRESHOLD)


class TestCorpusUnavailable:
    def test_citation_is_gated_never_commented_or_filed(self) -> None:
        out = _classify(_intent(duplicate_of=1234), OpenIssueCorpus.unavailable())
        assert isinstance(out, GateSuspectedDuplicate)
        assert out.issue_number == 1234 and out.score is None and out.reason

    def test_no_citation_files_new(self) -> None:
        assert isinstance(_classify(_intent(), OpenIssueCorpus.unavailable()), FileNew)

    def test_never_files_new_for_a_citation(self) -> None:
        # Core invariant: unavailable corpus + citation is NEVER "no dup -> file".
        for authority in _ALL_AUTHORITIES:
            out = classify_proposal(
                _intent(duplicate_of=7),
                OpenIssueCorpus.unavailable(),
                DuplicateTargetGrant.whole_board(),
                authority,
                threshold=_THRESHOLD,
            )
            assert not isinstance(out, FileNew)


class TestAgentCitation:
    def test_verified_granted_execute_comments(self) -> None:
        out = _classify(_intent(duplicate_of=1234), _ready())
        assert isinstance(out, CommentExisting) and out.issue_number == 1234

    @pytest.mark.parametrize(
        "authority",
        [
            DedupAuthority(create_issue_execute=False, post_comment_execute=True),
            DedupAuthority(create_issue_execute=True, post_comment_execute=False),
            DedupAuthority(create_issue_execute=False, post_comment_execute=False),
        ],
    )
    def test_any_propose_posture_gates_instead_of_commenting(self, authority) -> None:
        out = _classify(_intent(duplicate_of=1234), _ready(), authority=authority)
        assert isinstance(out, GateSuspectedDuplicate) and out.issue_number == 1234

    def test_missing_closed_or_pr_number_is_rejected(self) -> None:
        # The corpus holds only OPEN ISSUES, so a closed issue, a PR number, or a
        # missing number is simply absent -> not a known open issue.
        out = _classify(_intent(duplicate_of=999), _ready())
        assert isinstance(out, RejectCandidate) and out.issue_number == 999

    def test_out_of_grant_is_rejected(self) -> None:
        out = _classify(
            _intent(duplicate_of=1234), _ready(), grant=DuplicateTargetGrant.none()
        )
        assert isinstance(out, RejectCandidate) and out.issue_number == 1234


class TestLexicalBackstop:
    def test_strong_match_within_grant_gates_with_score(self) -> None:
        out = _classify(_intent(), _ready())
        assert isinstance(out, GateSuspectedDuplicate)
        assert out.issue_number == 1234
        assert out.score is not None and out.score >= _THRESHOLD

    def test_no_match_files_new(self) -> None:
        out = _classify(_intent(), _ready(OpenIssueRef(42, "Unrelated widget gadget", "")))
        assert isinstance(out, FileNew)

    def test_match_outside_grant_files_new(self) -> None:
        # A lexical match with no board-wide grant can't be acted on -> file novel.
        out = _classify(_intent(), _ready(), grant=DuplicateTargetGrant.none())
        assert isinstance(out, FileNew)


class TestInvariants:
    """Property tests over the full matrix — the states the gate makes illegal."""

    def _matrix(self):
        for grant in (DuplicateTargetGrant.whole_board(), DuplicateTargetGrant.none()):
            for corpus in (_ready(), OpenIssueCorpus.unavailable()):
                for dup in (None, 1234, 999):
                    for authority in _ALL_AUTHORITIES:
                        yield _intent(duplicate_of=dup), corpus, grant, authority

    def test_comment_existing_only_targets_verified_granted_execute(self) -> None:
        for intent, corpus, grant, authority in self._matrix():
            out = classify_proposal(intent, corpus, grant, authority, threshold=_THRESHOLD)
            if isinstance(out, CommentExisting):
                assert corpus.state is CorpusState.READY
                assert out.issue_number in corpus.numbers
                assert grant.permits(out.issue_number, corpus)
                assert authority.may_comment_now

    def test_every_suspected_gate_carries_candidate_and_reason(self) -> None:
        for intent, corpus, grant, authority in self._matrix():
            out = classify_proposal(intent, corpus, grant, authority, threshold=_THRESHOLD)
            if isinstance(out, GateSuspectedDuplicate):
                assert out.issue_number > 0 and out.reason
                # A lexical gate always keeps its score; a citation gate has none.

    def test_reject_never_becomes_a_comment(self) -> None:
        for intent, corpus, grant, authority in self._matrix():
            out = classify_proposal(intent, corpus, grant, authority, threshold=_THRESHOLD)
            # A cited number outside the trusted/granted set is never CommentExisting.
            if intent.duplicate_of == 999 and corpus.state is CorpusState.READY:
                assert isinstance(out, RejectCandidate)

    def test_identical_inputs_are_deterministic(self) -> None:
        for intent, corpus, grant, authority in self._matrix():
            a = classify_proposal(intent, corpus, grant, authority, threshold=_THRESHOLD)
            b = classify_proposal(intent, corpus, grant, authority, threshold=_THRESHOLD)
            assert a == b


class TestFlavorGrant:
    """#6878 B1: the duplicate-target grant is derived from launch authority —
    only a health review, which walks the whole board, earns a board-wide grant."""

    def test_health_review_gets_board_wide_grant(self) -> None:
        from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

        assert DuplicateTargetGrant.for_flavor(
            TechLeadSessionFlavor.HEALTH_REVIEW
        ).board_wide is True

    def test_batch_and_failure_get_no_grant(self) -> None:
        from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

        for flavor in (
            TechLeadSessionFlavor.BATCH_REVIEW,
            TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        ):
            assert DuplicateTargetGrant.for_flavor(flavor).board_wide is False
