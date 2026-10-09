"""Every MUTATING tech-lead command crosses the reconciliation gate (#6957).

Round-5 review F15/A6 wrapped the four commands whose reconciliation subjects
the dispatch table happened to spell out by hand. Round-6 F3/A3 found the
consequence: issue CREATION — the largest mutation of the lot — was not one of
them, so a case file or gated proposal could still be filed against a source
anchor paused behind ``io:needs-reconcile``.

A hand-maintained subject table is the defect. These tests pin the replacement:
the subject belongs to each command, and the wrapper is applied mechanically to
the complete mutating set — so a new tech-lead command cannot be added
unguarded, and one that names no subject cannot carry expectations either.
"""

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.domain.tech_lead_approval import with_proposal_marker
from issue_orchestrator.domain.tech_lead_approval import AWAITING_APPROVAL_LABEL
from issue_orchestrator.domain.tech_lead_session import OperatorDecision
from issue_orchestrator.domain.block_resolution import BlockResolution, ResolutionKind
from issue_orchestrator.domain.human_block import NeedsHumanCause
from issue_orchestrator.domain.tech_lead_approval import ApprovalTransition
from issue_orchestrator.control.actions import (
    Action,
    ActionResult,
    ActionType,
    AppendPatternObservationAction,
    CreateTechLeadCaseFileIssueAction,
    CreateTechLeadIssueAction,
    CreateTechLeadProposalIssueAction,
    DiscardTerminalTechLeadProposalOpsAction,
    KillHungSessionAction,
    PromoteTechLeadFindingAction,
    RecordTechLeadDispositionAction,
    RecoverValidatedWorkAction,
    ReleaseValidatedWorkAction,
    ReleaseWithheldReviewAction,
    ApplyOperatorDecisionAction,
    ResolveBlockAction,
    EscalateTechLeadDispositionAction,
    ReportPromotedFindingEvidenceAction,
    ResetRetryIssueAction,
    RequestReworkAction,
    SettleProposalApprovalAction,
    SettleTechLeadPromotionAction,
    SurfaceTechLeadProposalAction,
)
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.tech_lead_proposal_creation import RecoverTechLeadProposalAction
from issue_orchestrator.control.tech_lead_applier_handlers import (
    TECH_LEAD_MUTATING_ACTION_TYPES,
    tech_lead_action_handlers,
)
from issue_orchestrator.domain.tech_lead_findings import PatternObservation
from issue_orchestrator.domain.scoped_rework import ReworkRequest, ReworkTarget
from issue_orchestrator.domain.validated_work_release import ValidatedWorkRelease
from issue_orchestrator.domain.tech_lead_session import (
    StoredTechLeadOp,
    TECH_LEAD_OBSERVATION_LABEL,
    TechLeadCreationOrigin,
    TechLeadDisposition,
)
from issue_orchestrator.domain.validated_work import (
    RemoteBaselineStatus,
    ValidatedWorkKey,
)
from issue_orchestrator.domain.validated_work_commands import (
    ValidatedWorkAuthoritySnapshot,
)

ANCHOR = 77
CASE_FILE = 65
TARGET = 12
PROPOSAL_ISSUE = 512
UPSTREAM = "owner/upstream"
PROMOTION_MARKER = "<!-- issue-orchestrator:tech-lead-promotion:v1:abc -->"
CASE_FILE_MARKER = "<!-- issue-orchestrator:tech-lead-case-file:v1:abc -->"


def _validated_work_authority() -> ValidatedWorkAuthoritySnapshot:
    key = ValidatedWorkKey("owner/repo", TARGET, "12-work", "a" * 40)
    return ValidatedWorkAuthoritySnapshot(
        record_id=key.record_id,
        evidence_id="evidence-1",
        observation_revision=2,
        validated_head_sha=key.validated_head_sha,
        branch_name=key.branch_name,
        repo_slug=key.repo_slug,
        issue_number=key.issue_number,
        pr_number=None,
        expected_remote_head_sha=None,
        remote_baseline_status=RemoteBaselineStatus.UNOBSERVED,
    )


def _op() -> StoredTechLeadOp:
    return StoredTechLeadOp(
        op_type="reset_retry",
        target_issue_number=TARGET,
        rationale="stuck",
        source_run_id="r1",
        source_session_name="s",
        source_action_id="A1",
        created_at="2026-08-04T00:00:00Z",
    )


def _mutating_actions() -> dict[ActionType, tuple[Action, int]]:
    """One representative command per mutating type, with its expected subject."""
    expected = build_expected_for_mutation()
    return {
        ActionType.CREATE_TECH_LEAD_ISSUE: (
            CreateTechLeadIssueAction(
                title="follow-up",
                body="b",
                labels=("agent:backend",),
                origin=TechLeadCreationOrigin.derived_from_anchor(ANCHOR),
                expected=expected,
            ),
            ANCHOR,
        ),
        ActionType.CREATE_TECH_LEAD_PROPOSAL_ISSUE: (
            CreateTechLeadProposalIssueAction(
                title="Tech Lead proposal",
                body=with_proposal_marker("b"),
                labels=("agent:tech-lead", "tech-lead-proposal", AWAITING_APPROVAL_LABEL),
                origin=TechLeadCreationOrigin.derived_from_anchor(ANCHOR),
                op=_op(),
                expected=expected,
            ),
            ANCHOR,
        ),
        ActionType.CREATE_TECH_LEAD_CASE_FILE_ISSUE: (
            CreateTechLeadCaseFileIssueAction(
                title="Pattern case file: sig",
                body=f"b\n\n{CASE_FILE_MARKER}",
                labels=("agent:tech-lead", TECH_LEAD_OBSERVATION_LABEL),
                pattern_signature="sig",
                origin=TechLeadCreationOrigin.derived_from_anchor(ANCHOR),
                idempotency_marker=CASE_FILE_MARKER,
                observations=(
                    PatternObservation(observation_id="r1:s:A1", comment="observed"),
                ),
                expected=expected,
            ),
            ANCHOR,
        ),
        ActionType.ESCALATE_TECH_LEAD_DISPOSITION: (
            EscalateTechLeadDispositionAction(issue_number=TARGET, comment="human decision", expected=expected), TARGET,
        ),
        ActionType.RESET_RETRY_ISSUE: (
            ResetRetryIssueAction(
                issue_number=TARGET,
                proposal_id="A1",
                anchor_issue_number=ANCHOR,
                expected=expected,
            ),
            TARGET,
        ),
        ActionType.REQUEST_REWORK: (
            RequestReworkAction(request=ReworkRequest(
                ReworkTarget("owner/repo", 94, TARGET, "a" * 40, "12-work", (), ()),
            "finding", "report", "feedback"), proposal_id="A1", expected=expected), TARGET,
        ),
        ActionType.RECOVER_VALIDATED_WORK: (
            RecoverValidatedWorkAction(
                authority=_validated_work_authority(),
                rationale="publish retained validated work",
                proposal_id="A1",
                anchor_issue_number=ANCHOR,
                expected=expected,
            ),
            TARGET,
        ),
        ActionType.RELEASE_VALIDATED_WORK: (
            ReleaseValidatedWorkAction(
                release=ValidatedWorkRelease(479, (_validated_work_authority(),), "rebuilt in PR 479"),
                proposal_id="A1",
                anchor_issue_number=ANCHOR,
                proposal_issue_number=800,
                expected=expected,
            ),
            TARGET,
        ),
        ActionType.RELEASE_WITHHELD_REVIEW: (
            ReleaseWithheldReviewAction(
                issue_number=TARGET,
                proposal_id="A1",
                anchor_issue_number=ANCHOR,
                observed_at="2026-09-27T14:12:09+00:00",
                source_session_name="tech-lead-12",
                expected=expected,
            ),
            TARGET,
        ),
        ActionType.APPLY_OPERATOR_DECISION: (
            ApplyOperatorDecisionAction(
                issue_number=TARGET,
                decision=OperatorDecision(title="Split it", body="Land the slice."),
                proposal_id="A1",
                anchor_issue_number=ANCHOR,
                proposal_issue_number=800,
                expected=expected,
            ),
            TARGET,
        ),
        ActionType.RESOLVE_BLOCK: (
            ResolveBlockAction(
                issue_number=TARGET,
                resolution=BlockResolution(
                    kind=ResolutionKind.LIFT,
                    causes=frozenset({NeedsHumanCause.SESSION_LIFECYCLE}),
                    title="Stale", body="The engine gave up on a label that is gone.",
                    evidence=("board-snapshot.json",),
                ),
                proposal_id="A1",
                anchor_issue_number=ANCHOR,
                observed_at="2026-09-27T14:12:09+00:00",
                source_session_name="tech-lead-12",
                source_run_id="run-12",
                expected=expected,
            ),
            TARGET,
        ),
        ActionType.RECOVER_TECH_LEAD_PROPOSAL: (
            RecoverTechLeadProposalAction(creation_key="key", issue_number=TARGET, expected=expected), TARGET,
        ),
        ActionType.KILL_HUNG_SESSION: (
            KillHungSessionAction(
                issue_number=TARGET,
                proposal_id="A1",
                proposal_issue_number=800,
                expected=expected,
            ),
            TARGET,
        ),
        ActionType.APPEND_PATTERN_OBSERVATION: (
            AppendPatternObservationAction(
                issue_number=CASE_FILE,
                pattern_signature="sig",
                observation=PatternObservation(
                    observation_id="r1:s:A1", comment="observed again"
                ),
                expected=expected,
            ),
            CASE_FILE,
        ),
        ActionType.RECORD_TECH_LEAD_DISPOSITION: (
            RecordTechLeadDispositionAction(
                disposition=TechLeadDisposition(
                    issue_number=TARGET,
                    tracker_issue_number=900,
                    rationale="recover, don't reset",
                    source_run_id="r1",
                    source_session_name="s",
                    source_action_id="A1",
                    recorded_at="2026-08-09T00:00:00Z",
                ),
                expected=expected,
            ),
            TARGET,
        ),
        ActionType.PROMOTE_TECH_LEAD_FINDING: (
            PromoteTechLeadFindingAction(
                signature="sig",
                case_file_issue_number=CASE_FILE,
                target_repo=UPSTREAM,
                title="[tech-lead:src] sig",
                body=f"b\n\n{PROMOTION_MARKER}",
                labels=("agent:backend",),
                observation_count=2,
                idempotency_marker=PROMOTION_MARKER,
                expected=expected,
            ),
            CASE_FILE,
        ),
        ActionType.REPORT_PROMOTED_FINDING_EVIDENCE: (
            ReportPromotedFindingEvidenceAction(
                signature="sig",
                case_file_issue_number=CASE_FILE,
                target_repo=UPSTREAM,
                target_issue_number=500,
                observation_count=3,
                comment="more evidence",
                expected=expected,
            ),
            CASE_FILE,
        ),
        ActionType.SETTLE_TECH_LEAD_PROMOTION: (
            SettleTechLeadPromotionAction(
                signature="sig",
                case_file_issue_number=CASE_FILE,
                target_repo=UPSTREAM,
                target_issue_number=500,
                shipped=True,
                merged_pr_url="https://x/pull/1",
                expected=expected,
            ),
            CASE_FILE,
        ),
        # The proposal issue whose approval labels it writes (#7763).
        ActionType.SETTLE_PROPOSAL_APPROVAL: (
            SettleProposalApprovalAction(
                issue_number=PROPOSAL_ISSUE,
                transition=ApprovalTransition.ADMIT,
                expected=expected,
            ),
            PROPOSAL_ISSUE,
        ),
        # No managed-repo subject: this writes only orchestrator-owned ledger
        # rows, and its candidates are issues that are already gone or closed.
        ActionType.DISCARD_TERMINAL_TECH_LEAD_PROPOSAL_OPS: (
            DiscardTerminalTechLeadProposalOpsAction(candidate_issue_numbers=(1,)),
            0,
        ),
        # No managed-repo subject: it writes only the orchestrator-owned
        # charter decision ledger (#7330).
        ActionType.RECORD_TECH_LEAD_CHARTER_DECISIONS: (
            _charter_record_action(),
            0,
        ),
        # The wrapper writes only the charter ledger; its effect is dispatched
        # back through the applier and guarded on its own subject there.
        ActionType.APPLY_CHARTER_AUDITED_ACTION: (
            _charter_audited_action(),
            0,
        ),
    }


def _charter_audited_action() -> Action:
    from issue_orchestrator.control.tech_lead_charter_policy import CharterAuditedAction

    from issue_orchestrator.control.tech_lead_charter_policy import (
        RecordTechLeadCharterDecisionsAction,
    )

    record = _charter_record_action()
    assert isinstance(record, RecordTechLeadCharterDecisionsAction)
    return CharterAuditedAction(
        decisions=record.decisions,
        effect=DiscardTerminalTechLeadProposalOpsAction(candidate_issue_numbers=(1,)),
    )


def _charter_record_action() -> Action:
    from issue_orchestrator.control.tech_lead_charter_policy import (
        RecordTechLeadCharterDecisionsAction,
        TechLeadCharterPolicy,
    )
    from issue_orchestrator.domain.tech_lead_charter_decisions import (
        CharterDecisionSource,
        TechLeadCharterDecision,
        decision_key,
    )
    from issue_orchestrator.infra.config_models import TechLeadConfig

    tech_lead = TechLeadConfig()
    policy = TechLeadCharterPolicy(
        charter=tech_lead.charter.to_charter(),
        authority=tech_lead.authority,
        findings=tech_lead.findings,
    )
    return RecordTechLeadCharterDecisionsAction(
        decisions=(
            TechLeadCharterDecision.from_verdict(
                policy.decide("post_comment"),
                decision_id=decision_key("run-1", "A1"),
                source=CharterDecisionSource.DECISION,
                run_id="run-1",
                action_id="A1",
                anchor_issue_number=ANCHOR,
                target_number=7,
                target_is_pr=False,
                decided_at="2026-09-26T00:00:00+00:00",
                tracks_proposal=False,
            ),
        )
    )


class _Registry:
    """A handler map whose gate is observable and whose writes are countable.

    The applier-owned handlers are recorded directly; the extracted owners get
    mock ports, so "did anything downstream of the gate run" is answerable for
    every command in the table, not only the four passed in here.
    """

    def __init__(self, *, blocked: bool = False) -> None:
        self.guarded: list[tuple[ActionType, int]] = []
        self.ran: list[ActionType] = []
        self._blocked = blocked
        self.repository_host = MagicMock()
        self.authority = MagicMock()
        self.promotion_target = MagicMock()
        self.pattern_registry = MagicMock()
        inert = self._inert
        self.handlers = tech_lead_action_handlers(
            require_mutation_authority=lambda *args: None, create_tech_lead_issue=inert,
            surface_proposal=inert,
            reset_retry=inert,
            kill_hung_session=inert,
            request_rework=inert,
            recover_validated_work=inert,
            release_validated_work=inert,
            release_withheld_review=inert,
            apply_operator_decision=inert,
            resolve_block=inert,
            events=MagicMock(), label_manager=MagicMock(), needs_human_block=MagicMock(),
            apply_action=inert,
            verify_claim=lambda action, number: None,
            require_expected=self._require_expected,
            repository_host=self.repository_host,
            authority=self.authority,
            pattern_registry=self.pattern_registry,
            promotion_target=self.promotion_target,
            approvals=MagicMock(),
        )

    def _inert(self, action: Action) -> ActionResult:
        self.ran.append(action.action_type)
        return ActionResult.ok(action)

    def _require_expected(self, action: Action, issue_number: int) -> None:
        self.guarded.append((action.action_type, issue_number))
        if self._blocked:
            raise AssertionError(f"gate blocked {action.action_type.value}")

    @property
    def wrote(self) -> bool:
        """True when ANY owner ran — an applier handler or a port call."""
        return bool(
            self.ran
            or self.repository_host.method_calls
            or self.authority.method_calls
            or self.promotion_target.method_calls
            or self.pattern_registry.method_calls
        )

    def apply(self, action: Action) -> ActionResult:
        return self.handlers[action.action_type](action)


def test_the_mutating_set_covers_every_tech_lead_type_but_the_event_only_one():
    """A new tech-lead command lands in the mutating set unless it is inert."""
    registry = _Registry()

    assert set(registry.handlers) - TECH_LEAD_MUTATING_ACTION_TYPES == {
        ActionType.SURFACE_TECH_LEAD_PROPOSAL, ActionType.REQUIRE_TECH_LEAD_INVESTIGATION
    }


@pytest.mark.parametrize(
    "action_type", sorted(TECH_LEAD_MUTATING_ACTION_TYPES, key=lambda t: t.value)
)
def test_every_mutating_command_is_dispatched_through_the_gate(action_type):
    """The whole point: NO mutating command reaches its owner ungated."""
    action, subject = _mutating_actions()[action_type]
    registry = _Registry()

    registry.apply(action)

    if subject:
        assert registry.guarded and set(registry.guarded) == {(action_type, subject)}
    else:
        # No subject, and therefore no expectations either — nothing to check.
        assert registry.guarded == []
    # ...and with the gate open, the command really did reach its owner, so the
    # blocked-gate assertion below is about ordering, not an inert table.
    assert registry.wrote


@pytest.mark.parametrize(
    "action_type",
    sorted(
        (
            t
            for t, (_action, subject) in _mutating_actions().items()
            if subject
        ),
        key=lambda t: t.value,
    ),
)
def test_a_blocked_gate_stops_the_owner_from_running(action_type):
    action, _subject = _mutating_actions()[action_type]
    registry = _Registry(blocked=True)

    with pytest.raises(AssertionError):
        registry.apply(action)

    assert not registry.wrote


def test_the_event_only_surface_is_not_gated():
    """Surfacing a proposal makes no calls at all, so it needs no gate."""
    registry = _Registry(blocked=True)

    registry.apply(
        SurfaceTechLeadProposalAction(issue_number=ANCHOR, action_id="A1", mode="shadow")
    )

    assert registry.guarded == []
    assert registry.ran == [ActionType.SURFACE_TECH_LEAD_PROPOSAL]


def test_expectations_without_a_subject_are_refused_before_any_write():
    """A dropped subject must fail closed, never run unguarded."""
    registry = _Registry()

    with pytest.raises(ValueError, match="no reconciliation subject"):
        registry.apply(
            DiscardTerminalTechLeadProposalOpsAction(
                candidate_issue_numbers=(1,),
                expected=build_expected_for_mutation(),
            )
        )

    assert not registry.wrote
