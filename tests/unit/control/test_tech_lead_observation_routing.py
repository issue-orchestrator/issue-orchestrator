"""Where a duplicate-suspected create_issue observation lands (#6989)."""

import pytest

from issue_orchestrator.control.proposal_dedup_gate import (
    CommentExisting,
    FileNew,
    GateDedupUnavailable,
    GateSuspectedDuplicate,
    GateUnverifiedDuplicate,
    RejectCandidate,
)
from issue_orchestrator.control.tech_lead_observation_routing import (
    accrual_for,
    accrual_signature,
    observation_of,
)
from issue_orchestrator.domain.tech_lead_artifacts import ProposedTechLeadAction


def _proposal(**overrides) -> ProposedTechLeadAction:
    action = ProposedTechLeadAction(
        id=overrides.pop("id", "A1"),
        action_type="create_issue",
        title=overrides.pop("title", "Search-API budget exhaustion re-verified"),
        body=overrides.pop("body", "1,824 403s in three hours."),
        duplicate_of=overrides.pop("duplicate_of", 6928),
        **overrides,
    )
    action.validate()
    return action


class TestWhichOutcomesAccrue:
    """Only an AGENT-CITED duplicate earns the durable ledger."""

    def test_verified_citation_the_session_cannot_comment_on_accrues(self) -> None:
        accrual = accrual_for(
            GateSuspectedDuplicate(6928, None, "agent-confirmed duplicate; gated"),
            _proposal(),
        )
        assert accrual is not None
        assert accrual.candidate_issue_number == 6928
        assert accrual.signature == "duplicate-of-#6928"
        assert "agent-confirmed duplicate" in accrual.reason

    def test_unverifiable_citation_accrues(self) -> None:
        accrual = accrual_for(
            GateUnverifiedDuplicate(6928, "open-issue corpus unavailable"),
            _proposal(),
        )
        assert accrual is not None
        assert accrual.candidate_issue_number == 6928

    def test_lexical_suspicion_does_not_accrue(self) -> None:
        # The agent claimed nothing; burying a false-positive match in an
        # evidence ledger would lose real work.
        assert (
            accrual_for(
                GateSuspectedDuplicate(6928, 0.93, "lexical near-duplicate"),
                _proposal(duplicate_of=None),
            )
            is None
        )

    @pytest.mark.parametrize(
        "outcome",
        [
            FileNew(),
            CommentExisting(6928, "agent-confirmed duplicate"),
            GateDedupUnavailable("corpus unavailable"),
            RejectCandidate(4242, "cited duplicate is not a known open issue"),
        ],
        ids=["file-new", "comment-existing", "no-candidate", "rejected-candidate"],
    )
    def test_outcomes_with_no_accrual_point_keep_the_create(self, outcome) -> None:
        assert accrual_for(outcome, _proposal()) is None

    def test_an_unknown_outcome_fails_fast_rather_than_minting(self) -> None:
        # A future DedupOutcome variant added without a routing decision must
        # not silently fall through to "mint a fresh issue".
        with pytest.raises(AssertionError):
            accrual_for(object(), _proposal())  # type: ignore[arg-type]


class TestAccrualSignature:
    def test_defaults_to_the_cited_issue(self) -> None:
        assert accrual_signature(_proposal(), 6928) == "duplicate-of-#6928"

    def test_a_named_recurring_class_wins_verbatim(self) -> None:
        # Verbatim: flag_pattern keys the same ledger with the raw value, so
        # normalizing here would split one class across two case files.
        proposal = _proposal(pattern_signature=" search-api-budget-exhaustion ")
        assert accrual_signature(proposal, 6928) == " search-api-budget-exhaustion "


class TestRestatedObservation:
    def _accrual(self, proposal: ProposedTechLeadAction):
        accrual = accrual_for(
            GateSuspectedDuplicate(6928, None, "agent-confirmed duplicate; gated"),
            proposal,
        )
        assert accrual is not None
        return accrual

    def test_keeps_the_proposal_identity_and_gains_the_signature(self) -> None:
        proposal = _proposal(id="A7", finding_ids=("T3",))
        observation = observation_of(proposal, self._accrual(proposal))
        assert observation.id == "A7"
        assert observation.finding_ids == ("T3",)
        assert observation.pattern_signature == "duplicate-of-#6928"

    def test_classifies_nothing_but_records_what_was_claimed(self) -> None:
        # area is the ledger's other IMMUTABLE field: reconciling it raises, and
        # that raise rejects the whole decision. A sighting must not supply it —
        # nor pick the repo a fix:code promotion routes to.
        proposal = _proposal(
            area="github-api", labels=("bug",), expedite=True
        )
        observation = observation_of(proposal, self._accrual(proposal))
        assert observation.area is None
        body = observation.body or ""
        assert "`github-api`" in body  # evidence kept for the human, not applied
        assert "`bug`" in body
        assert "expedited" in body

    def test_body_carries_the_proposal_and_its_reconciliation_evidence(self) -> None:
        proposal = _proposal()
        body = observation_of(proposal, self._accrual(proposal)).body or ""
        assert "Search-API budget exhaustion re-verified" in body
        assert "1,824 403s in three hours." in body
        assert "#6928" in body
        assert "agent-confirmed duplicate" in body

    def test_an_intra_decision_sibling_reason_is_not_lost(self) -> None:
        proposal = _proposal()
        observation = observation_of(
            proposal, self._accrual(proposal), sibling_action_id="A1"
        )
        assert "A1" in (observation.body or "")

    def test_stays_unclassified_so_it_is_never_promoted(self) -> None:
        # fix_class is valid only on flag_pattern, so an accrued sighting can
        # never make a signature promotable on its own.
        proposal = _proposal()
        observation = observation_of(proposal, self._accrual(proposal))
        assert observation.fix_class is None
