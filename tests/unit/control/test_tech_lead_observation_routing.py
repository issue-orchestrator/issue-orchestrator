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
    case_file_sighting,
)
from issue_orchestrator.control.tech_lead_case_files import CaseFileIntake
from issue_orchestrator.domain.tech_lead_artifacts import ProposedTechLeadAction
from issue_orchestrator.domain.tech_lead_findings import CaseFileClassification


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
        intake = case_file_sighting(proposal, self._accrual(proposal))
        assert intake.proposal.id == "A7"
        assert intake.proposal.finding_ids == ("T3",)
        assert intake.signature == "duplicate-of-#6928"

    def test_records_what_was_claimed_without_claiming_it(self) -> None:
        # area is one of the ledger's IMMUTABLE fields: reconciling it raises,
        # and that raise rejects the whole decision. A sighting must not supply
        # it — nor pick the repo a fix:code promotion routes to.
        proposal = _proposal(
            area="github-api", labels=("bug",), expedite=True
        )
        intake = case_file_sighting(proposal, self._accrual(proposal))
        assert intake.classification.area == ""
        body = intake.proposal.body or ""
        assert "`github-api`" in body  # evidence kept for the human, not applied
        assert "`bug`" in body
        assert "expedited" in body

    def test_body_carries_the_proposal_and_its_reconciliation_evidence(self) -> None:
        proposal = _proposal()
        body = case_file_sighting(proposal, self._accrual(proposal)).proposal.body or ""
        assert "Search-API budget exhaustion re-verified" in body
        assert "1,824 403s in three hours." in body
        assert "#6928" in body
        assert "agent-confirmed duplicate" in body

    def test_an_intra_decision_sibling_reason_is_not_lost(self) -> None:
        proposal = _proposal()
        intake = case_file_sighting(
            proposal, self._accrual(proposal), sibling_action_id="A1"
        )
        assert "A1" in (intake.proposal.body or "")

    def test_establishes_no_durable_fact_at_all(self) -> None:
        # #6989 round-1 review F1: an accrued sighting classifies nothing AND
        # diagnoses nothing. fix_class/area decide promotability and routing;
        # the diagnosis is the mechanism a routed promotion is FILED ON, so a
        # re-sighting that says "seen again" must never become it.
        proposal = _proposal(area="github-api")
        intake = case_file_sighting(proposal, self._accrual(proposal))
        assert intake.classification == CaseFileClassification()

    def test_a_diagnosing_intake_is_the_only_one_that_classifies(self) -> None:
        # The contrast that makes the intake contract meaningful: the same lane,
        # entered by a reviewed flag_pattern, carries all three durable facts.
        flag = ProposedTechLeadAction(
            id="A9",
            action_type="flag_pattern",
            body="The lease renewer stalls; renew off the tick thread.",
            pattern_signature="lease-renewer-stall",
            area="control",
            fix_class="code",
        )
        intake = CaseFileIntake.diagnosing(flag)
        assert intake.classification == CaseFileClassification(
            fix_class="code",
            area="control",
            diagnosis="The lease renewer stalls; renew off the tick thread.",
        )
