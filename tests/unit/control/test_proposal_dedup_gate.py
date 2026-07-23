"""Tests for the deterministic ProposalDedupGate (#6878).

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
    GateDedupUnavailable,
    GateSuspectedDuplicate,
    OpenIssueCorpus,
    ProposalIntent,
    RejectCandidate,
    classify_proposal,
)

_THRESHOLD = 0.72
_EXECUTE = DedupAuthority(create_issue_execute=True, post_comment_execute=True)
_MATCH = OpenIssueRef(1234, "Stabilize CI runner disconnects", "runner drops mid build")
_GRANT = DuplicateTargetGrant.of({1234})
_ALL_AUTHORITIES = [DedupAuthority(c, p) for c in (True, False) for p in (True, False)]


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
    grant: DuplicateTargetGrant = _GRANT,
    authority: DedupAuthority = _EXECUTE,
):
    return classify_proposal(intent, corpus, grant, authority, threshold=_THRESHOLD)


class TestCorpusStateContract:
    def test_contradictory_corpus_cannot_be_constructed(self) -> None:
        # A non-READY corpus that lists issues is an unrepresentable state.
        with pytest.raises(ValueError, match="must carry no issues"):
            OpenIssueCorpus(CorpusState.UNAVAILABLE, (_MATCH,))
        with pytest.raises(ValueError, match="must carry no issues"):
            OpenIssueCorpus(CorpusState.DISABLED, (_MATCH,))


class TestDisabled:
    def test_files_new_ignoring_any_citation(self) -> None:
        # The feature is intentionally off — no dedup attempted, citations ignored.
        assert isinstance(_classify(_intent(), OpenIssueCorpus.disabled()), FileNew)
        assert isinstance(
            _classify(_intent(duplicate_of=1234), OpenIssueCorpus.disabled()), FileNew
        )


class TestUnavailableFailsClosed:
    def test_gates_dedup_unavailable_with_or_without_citation(self) -> None:
        # Facts were expected but missing -> fail closed, never file unchecked.
        for intent in (_intent(), _intent(duplicate_of=1234)):
            out = _classify(intent, OpenIssueCorpus.unavailable())
            assert isinstance(out, GateDedupUnavailable) and out.reason

    def test_never_files_new_under_any_authority(self) -> None:
        for authority in _ALL_AUTHORITIES:
            out = classify_proposal(
                _intent(duplicate_of=7),
                OpenIssueCorpus.unavailable(),
                _GRANT,
                authority,
                threshold=_THRESHOLD,
            )
            assert isinstance(out, GateDedupUnavailable)


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
        assert out.score is None  # a citation gate carries no lexical score

    def test_verified_but_out_of_grant_is_gated_not_rejected(self) -> None:
        # A known open issue outside the comment grant is valid duplicate evidence:
        # gate it (writes nothing), never file as novel and never reject.
        out = _classify(
            _intent(duplicate_of=1234), _ready(), grant=DuplicateTargetGrant.none()
        )
        assert isinstance(out, GateSuspectedDuplicate) and out.issue_number == 1234

    def test_missing_closed_or_pr_number_is_rejected(self) -> None:
        # The corpus holds only OPEN ISSUES, so a closed issue, PR number, or a
        # missing number is simply absent -> not a known open issue.
        out = _classify(_intent(duplicate_of=999), _ready())
        assert isinstance(out, RejectCandidate) and out.issue_number == 999


class TestLexicalBackstop:
    def test_strong_match_gates_with_score_regardless_of_grant(self) -> None:
        # B2: the grant governs writes, not whether evidence may gate — so the
        # backstop fires even with no redirect grant (batch/failure flavors).
        out = _classify(_intent(), _ready(), grant=DuplicateTargetGrant.none())
        assert isinstance(out, GateSuspectedDuplicate)
        assert out.issue_number == 1234
        assert out.score is not None and out.score >= _THRESHOLD

    def test_no_match_files_new(self) -> None:
        out = _classify(_intent(), _ready(OpenIssueRef(42, "Unrelated widget gadget", "")))
        assert isinstance(out, FileNew)


class TestGrantFromLaunchAuthority:
    """#6878 B3: the redirect grant is the session's EXISTING immutable comment
    scope (``allowed_targets``), never a board-wide capability from flavor."""

    def _grant(self, authority) -> DuplicateTargetGrant:
        return DuplicateTargetGrant.of(authority.allowed_targets())

    def test_health_review_grants_only_its_anchor_not_the_board(self) -> None:
        from issue_orchestrator.domain.tech_lead_session import (
            TechLeadLaunchAuthority,
            TechLeadSessionFlavor,
        )

        grant = self._grant(
            TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.HEALTH_REVIEW, anchor_issue_number=99
            )
        )
        assert grant.permits(99)  # its tracking issue only
        assert not grant.permits(1234)  # an arbitrary backlog issue -> gate, not comment

    def test_failure_investigation_grants_only_its_focus(self) -> None:
        from issue_orchestrator.domain.tech_lead_session import (
            TechLeadLaunchAuthority,
            TechLeadSessionFlavor,
        )

        grant = self._grant(
            TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                anchor_issue_number=99,
                focus_issue_number=7,
            )
        )
        assert grant.permits(7)
        assert not grant.permits(1234) and not grant.permits(99)

    def test_batch_grants_manifest_prs_and_anchor(self) -> None:
        from issue_orchestrator.domain.tech_lead_session import (
            TechLeadLaunchAuthority,
            TechLeadSessionFlavor,
        )

        grant = self._grant(
            TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.BATCH_REVIEW,
                anchor_issue_number=99,
                manifest_pr_numbers=(10, 20),
            )
        )
        assert grant.permits(10) and grant.permits(20) and grant.permits(99)
        assert not grant.permits(1234)


class TestInvariants:
    """Property tests over the full matrix — the states the gate makes illegal."""

    def _matrix(self):
        for grant in (_GRANT, DuplicateTargetGrant.none()):
            for corpus in (
                _ready(),
                OpenIssueCorpus.disabled(),
                OpenIssueCorpus.unavailable(),
            ):
                for dup in (None, 1234, 999):
                    for authority in _ALL_AUTHORITIES:
                        yield _intent(duplicate_of=dup), corpus, grant, authority

    def test_comment_existing_only_when_verified_granted_execute(self) -> None:
        for intent, corpus, grant, authority in self._matrix():
            out = classify_proposal(intent, corpus, grant, authority, threshold=_THRESHOLD)
            if isinstance(out, CommentExisting):
                assert corpus.state is CorpusState.READY
                assert out.issue_number in corpus.numbers
                assert grant.permits(out.issue_number)
                assert authority.may_comment_now

    def test_lexical_gate_always_keeps_its_score(self) -> None:
        # A lexical match (no citation) must never lose its score.
        for corpus in (_ready(),):
            for grant in (_GRANT, DuplicateTargetGrant.none()):
                out = classify_proposal(
                    _intent(), corpus, grant, _EXECUTE, threshold=_THRESHOLD
                )
                if isinstance(out, GateSuspectedDuplicate):
                    assert out.score is not None and out.reason

    def test_unavailable_corpus_is_always_dedup_unavailable(self) -> None:
        for intent, corpus, grant, authority in self._matrix():
            out = classify_proposal(intent, corpus, grant, authority, threshold=_THRESHOLD)
            if corpus.state is CorpusState.UNAVAILABLE:
                assert isinstance(out, GateDedupUnavailable)

    def test_reject_never_becomes_a_comment(self) -> None:
        for intent, corpus, grant, authority in self._matrix():
            out = classify_proposal(intent, corpus, grant, authority, threshold=_THRESHOLD)
            if intent.duplicate_of == 999 and corpus.state is CorpusState.READY:
                assert isinstance(out, RejectCandidate)

    def test_identical_inputs_are_deterministic(self) -> None:
        for intent, corpus, grant, authority in self._matrix():
            a = classify_proposal(intent, corpus, grant, authority, threshold=_THRESHOLD)
            b = classify_proposal(intent, corpus, grant, authority, threshold=_THRESHOLD)
            assert a == b
