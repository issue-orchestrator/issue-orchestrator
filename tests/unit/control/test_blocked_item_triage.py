"""Blocked-item triage (#7593): every blocked item gets one class and one applied action."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.actions import (
    ApplyOperatorDecisionAction,
    CreateTechLeadProposalIssueAction,
)
from issue_orchestrator.control.blocked_item_triage import (
    StateBlockedItemTriage,
    triage_coverage_violation,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.proposal_dedup_gate import DuplicateTargetGrant, OpenIssueCorpus
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.tech_lead_charter_records import CharterDecisionLog
from issue_orchestrator.control.tech_lead_decision_actions import plan_tech_lead_decision_actions
from issue_orchestrator.control.tech_lead_operator_decision import (
    OperatorDecisionExecutor,
    decision_marker,
    follow_up_marker,
)
from issue_orchestrator.control.tech_lead_proposals import plan_approved_tech_lead_op_executions
from issue_orchestrator.domain.blocked_item_triage import (
    MAX_TRIAGE_ITEMS_PER_RUN,
    TriageGrant,
    block_fingerprint,
)
from issue_orchestrator.domain.human_block import NeedsHumanCause
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.domain.tech_lead_artifacts import (
    DecisionFollowUp,
    ProposedTechLeadAction,
    TechLeadDecision,
    TriageClass,
)
from issue_orchestrator.domain.tech_lead_charter import (
    CharterAuthority,
    CharterBinding,
    CharterDepth,
    CharterOutcome,
    CharterReason,
    CharterRole,
)
from issue_orchestrator.domain.tech_lead_charter_decisions import (
    CharterDecisionSource,
    CharterExecutionResult,
    CharterProposalLifecycle,
    TechLeadCharterDecision,
)
from issue_orchestrator.domain.tech_lead_session import (
    ApprovedTechLeadOp,
    OperatorDecision,
    PROPOSED_TECH_LEAD_LABEL,
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.operator_issue_commands import (
    OperatorCommandIntent,
    OperatorCommandOutcome,
    OperatorCommandStatus,
)
from issue_orchestrator.ports.timeline_store import TimelineRecord

ANCHOR = 900
SPLIT_QUESTION = (
    "#262 is more than one session. Its share-page slice is done and gate-green;"
    " the live D1 seller index is not started. Should I split #262: land this"
    " branch as a PR under 'Refs #262' and move the live index into its own issue(s)?"
)


def _config() -> Config:
    config = Config()
    config.repo = "porchpin/porchpin"
    config.tech_lead_review_agent = "agent:tech-lead"
    return config


def _issue(number: int, *labels: str, title: str | None = None) -> Issue:
    return Issue(
        number=number, title=title or f"Issue {number}", labels=list(labels),
        repo="porchpin/porchpin", state="open",
    )


@dataclass
class _Ledger:
    decisions: dict[int, list[TechLeadCharterDecision]] = field(default_factory=dict)

    def list_about_issue(self, issue_number: int, *, limit: int = 100):
        return tuple(self.decisions.get(issue_number, ()))[:limit]


def _triage_record(
    issue: int, triage_class: TriageClass, fingerprint: str, *, effect: str, filed: bool = True,
) -> TechLeadCharterDecision:
    executed = effect in {"applied", "failed", "withheld", "refused", "parked"}
    return TechLeadCharterDecision(
        decision_id=f"decision:run:{issue}", source=CharterDecisionSource.DECISION,
        run_id="run", action_id="A1", anchor_issue_number=ANCHOR, target_number=issue,
        target_is_pr=False, action_kind="escalate_to_human" if executed else "propose_decision",
        role=CharterRole.FLOW, required_depth=CharterDepth.WORKAROUND,
        binding=CharterBinding.FLOOR if executed else CharterBinding.OPERATOR_DECISION,
        role_enabled=True, role_depth=CharterDepth.RESTRUCTURE,
        role_authority=CharterAuthority.EXECUTE, action_ceiling=CharterAuthority.EXECUTE,
        ceiling_source="c",
        outcome=CharterOutcome.EXECUTED if executed else CharterOutcome.PROPOSED,
        reason_code=(
            CharterReason.FLOOR_ALWAYS_EXECUTES if executed
            else CharterReason.OPERATOR_DECISION_ALWAYS_PROPOSED
        ),
        reason="r", decided_at="2026-10-02T12:00:00+00:00",
        execution=CharterExecutionResult(effect) if executed else None,
        lifecycle=None if executed else CharterProposalLifecycle(effect),
        proposal_issue_number=None if executed or not filed else 950,
        triage_class=triage_class, triage_fingerprint=fingerprint,
    )


def _owner(
    issues: list[Issue],
    *,
    causes: dict[int, frozenset[NeedsHumanCause]] | None = None,
    ledger: _Ledger | None = None,
    timeline: dict[int, list[TimelineRecord]] | None = None,
) -> StateBlockedItemTriage:
    state = OrchestratorState()
    state.cached_scope_issues = issues
    config = _config()
    return StateBlockedItemTriage(
        config=config,
        state=lambda: state,
        labels=LabelManager(config),
        needs_human_causes=lambda numbers: {n: (causes or {}).get(n, frozenset()) for n in numbers},
        charter_ledger=ledger or _Ledger(),
        timeline_reader=lambda number, limit: (timeline or {}).get(number, []),
    )


def _porchpin_board() -> list[Issue]:
    """Porchpin's four blocked items on 2026-10-02, plus things that are not."""
    return [
        _issue(179, "agent:backend", "needs-human", "tech-lead-needs-human"),
        _issue(262, "agent:backend", "needs-human", title="Live seller pickup index"),
        _issue(326, "agent:backend", "needs-human", "blocked-cross-milestone"),
        _issue(364, "agent:backend", "needs-human", "pr-pending"),
        _issue(400, "agent:backend"),  # runnable, not blocked
        _issue(410, "agent:tech-lead", "needs-human"),  # tech-lead machinery
        _issue(411, "agent:tech-lead", PROPOSED_TECH_LEAD_LABEL),  # a proposal
        _issue(ANCHOR, "agent:tech-lead", "needs-human"),  # this review's anchor
    ]


# -- the agenda ----------------------------------------------------------------


def test_every_blocked_work_item_is_on_the_agenda_with_its_facts() -> None:
    owner = _owner(
        _porchpin_board(),
        causes={262: frozenset({NeedsHumanCause.AGENT_COMPLETION, NeedsHumanCause.SESSION_LIFECYCLE})},
        timeline={262: [TimelineRecord(
            event_id="e1", timestamp="2026-09-23T06:38:06Z", event="issue.needs_human",
            data={"question": SPLIT_QUESTION, "reason": "Agent requested human input"},
        )]},
    )

    agenda = owner.agenda(anchor_issue_number=ANCHOR)

    assert [item.issue_number for item in agenda.items] == [179, 262, 326, 364]
    by_number = {item.issue_number: item for item in agenda.items}
    assert by_number[262].agent_question == SPLIT_QUESTION
    assert by_number[262].needs_human_causes == ("agent_completion", "session_lifecycle")
    assert by_number[262].reason == "never triaged"
    # The tech lead's own hand-over marker does not count as a block change.
    assert by_number[179].fingerprint == ""
    assert by_number[326].fingerprint == "blocked-cross-milestone,needs-human"
    assert agenda.grants == tuple(TriageGrant(i.issue_number, i.fingerprint) for i in agenda.items)


@pytest.mark.parametrize(
    ("effect", "in_force"),
    [
        ("awaiting_approval", True),
        ("declined", True),  # the operator answered: theirs now
        ("approved_applied", True),
        ("approved_stale", False),
        ("applied", True),
        ("failed", False),
        ("withheld", False),
        ("parked", False),
    ],
)
def test_an_unchanged_item_is_not_triaged_again_while_its_triage_is_in_force(
    effect: str, in_force: bool
) -> None:
    triage_class = (
        TriageClass.HUMAN_HAND_OVER
        if effect in {"applied", "failed", "withheld", "parked"}
        else TriageClass.OPERATOR_DECISION
    )
    ledger = _Ledger({262: [_triage_record(262, triage_class, "needs-human", effect=effect)]})
    owner = _owner([_issue(262, "agent:backend", "needs-human")], ledger=ledger)

    agenda = owner.agenda(anchor_issue_number=ANCHOR)

    assert (agenda.in_force == (262,)) is in_force
    assert ([item.issue_number for item in agenda.items] == [262]) is (not in_force)


def test_a_proposal_that_never_got_filed_is_not_a_triage_in_force() -> None:
    """Awaiting approval only counts once the proposal issue exists: a creation
    that failed left the record routed to the gate with nothing to approve."""
    ledger = _Ledger({262: [_triage_record(
        262, TriageClass.OPERATOR_DECISION, "needs-human", effect="awaiting_approval", filed=False,
    )]})
    owner = _owner([_issue(262, "agent:backend", "needs-human")], ledger=ledger)

    [item] = owner.agenda(anchor_issue_number=ANCHOR).items

    assert item.reason.endswith("did not take effect: awaiting_approval")


def test_a_changed_block_is_triaged_again() -> None:
    ledger = _Ledger({326: [_triage_record(
        326, TriageClass.HUMAN_HAND_OVER, "blocked-cross-milestone,needs-human", effect="applied",
    )]})
    # The engine fix took the stale dependency label off (#7333).
    owner = _owner([_issue(326, "agent:backend", "needs-human")], ledger=ledger)

    [item] = owner.agenda(anchor_issue_number=ANCHOR).items

    assert item.issue_number == 326
    assert item.reason.startswith("its block changed since it was triaged human_hand_over")
    assert item.prior is not None and item.prior.effect == "applied"


def test_the_agenda_is_capped_per_run_oldest_first() -> None:
    issues = [_issue(n, "agent:backend", "blocked-failed") for n in range(100, 100 + MAX_TRIAGE_ITEMS_PER_RUN + 3)]

    agenda = _owner(issues).agenda(anchor_issue_number=ANCHOR)

    assert [item.issue_number for item in agenda.items] == list(range(100, 100 + MAX_TRIAGE_ITEMS_PER_RUN))
    assert len(agenda.deferred) == 3


def test_an_unreadable_ledger_fails_the_agenda_rather_than_guessing() -> None:
    class _Broken:
        def list_about_issue(self, issue_number: int, *, limit: int = 100):
            raise RuntimeError("authority store locked")

    owner = _owner([_issue(262, "agent:backend", "needs-human")], ledger=_Broken())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="locked"):
        owner.agenda(anchor_issue_number=ANCHOR)


def test_fingerprint_ignores_order_and_case() -> None:
    assert block_fingerprint(
        ["Needs-Human", "blocked-failed"], tech_lead_marker=False, needs_human_label="needs-human"
    ) == block_fingerprint(
        ["blocked-failed", "needs-human"], tech_lead_marker=False, needs_human_label="needs-human"
    )


# -- the completion rule -------------------------------------------------------


def _authority(*grants: TriageGrant) -> TechLeadLaunchAuthority:
    return TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW, anchor_issue_number=ANCHOR,
        triage_grants=tuple(grants),
    )


def _decide(*actions: ProposedTechLeadAction) -> TechLeadDecision:
    return TechLeadDecision(summary="walk the floor", proposed_actions=tuple(actions))


def _split(action_id: str = "A1", target: int = 262) -> ProposedTechLeadAction:
    return ProposedTechLeadAction(
        id=action_id, action_type="propose_decision", target_number=target,
        title="Split #262: land the share-page slice under Refs #262",
        body=f"The agent asked: {SPLIT_QUESTION}\n\nRecommend: split.",
        triage_class=TriageClass.OPERATOR_DECISION,
        follow_up_issues=(DecisionFollowUp(
            title="Live D1 seller pickup index", body="The remainder of #262's acceptance list.",
        ),),
    )


def _hand_over(action_id: str, target: int) -> ProposedTechLeadAction:
    return ProposedTechLeadAction(
        id=action_id, action_type="escalate_to_human", target_number=target,
        body="Cloudflare provisioning (D1, Access app, tokens) is the operator's.",
        triage_class=TriageClass.HUMAN_HAND_OVER,
    )


def test_a_decision_that_triages_every_granted_item_is_valid() -> None:
    authority = _authority(TriageGrant(179, ""), TriageGrant(262, "needs-human"))

    assert triage_coverage_violation(_decide(_split(), _hand_over("A2", 179)), authority) is None


def test_the_porchpin_shape_advice_on_the_anchor_alone_is_rejected() -> None:
    """What porchpin's tech lead did: diagnose, advise on its anchor, act on nothing."""
    advice = ProposedTechLeadAction(
        id="A1", action_type="post_comment", target_number=ANCHOR,
        body="#262 waits on the maintainer's split decision.",
    )

    violation = triage_coverage_violation(_decide(advice), _authority(TriageGrant(262, "needs-human")))

    assert violation is not None and "untriaged: #262" in violation


def test_one_item_two_triages_is_rejected() -> None:
    explain = ProposedTechLeadAction(
        id="A2", action_type="post_comment", target_number=262, body="Waiting on a split decision.",
        triage_class=TriageClass.EXPLAINED,
    )

    violation = triage_coverage_violation(
        _decide(_split(), explain), _authority(TriageGrant(262, "needs-human"))
    )

    assert violation is not None and "exactly one" in violation


def test_triaging_an_item_the_run_was_not_granted_is_rejected() -> None:
    violation = triage_coverage_violation(_decide(_split(target=263)), _authority())

    assert violation is not None and "not granted" in violation


def test_a_class_only_lands_on_its_own_action_types() -> None:
    with pytest.raises(ValueError, match="cannot carry triage_class operator_decision"):
        ProposedTechLeadAction(
            id="A1", action_type="escalate_to_human", target_number=262, body="b",
            triage_class=TriageClass.OPERATOR_DECISION,
        ).validate()
    with pytest.raises(ValueError, match="follow_up_issues"):
        ProposedTechLeadAction(
            id="A1", action_type="create_issue", title="t", body="b",
            follow_up_issues=(DecisionFollowUp("t", "b"),),
        ).validate()


# -- planning: the split question becomes an approvable proposal ----------------


def _plan(decision: TechLeadDecision, *, fingerprints: dict[int, str]):
    config = _config()
    log = CharterDecisionLog(
        run_id="run-1", anchor_issue_number=ANCHOR, decided_at="2026-10-02T12:00:00+00:00",
        triage_fingerprints=fingerprints,
    )
    actions = plan_tech_lead_decision_actions(
        decision, config, LabelManager(config),
        anchor_issue=_issue(ANCHOR, "agent:tech-lead"), expected=build_expected_for_mutation(),
        op_ledger={}, pattern_ledger={}, source_run_id="run-1", source_session_name="tech-lead-900",
        observed_at="2026-10-02T12:00:00+00:00", observed_session_generation=lambda _n: None,
        dedup_corpus=OpenIssueCorpus.ready(()), dedup_grant=DuplicateTargetGrant.of(frozenset()),
        charter_log=log,
    )
    return actions, log


def test_the_split_question_becomes_an_approvable_proposal_with_its_triage_on_record() -> None:
    actions, log = _plan(_decide(_split()), fingerprints={262: "needs-human"})

    [proposal] = [a for a in actions if isinstance(a, CreateTechLeadProposalIssueAction)]
    assert PROPOSED_TECH_LEAD_LABEL in proposal.labels
    assert proposal.op.op_type == "propose_decision"
    assert proposal.op.target_issue_number == 262
    assert proposal.op.decision == OperatorDecision(
        title="Split #262: land the share-page slice under Refs #262",
        body=f"The agent asked: {SPLIT_QUESTION}\n\nRecommend: split.",
        follow_ups=(DecisionFollowUp(
            "Live D1 seller pickup index", "The remainder of #262's acceptance list.",
        ),),
    )
    assert "Decision for #262" in proposal.body and "Live D1 seller pickup index" in proposal.body
    [record] = log.records()
    assert record.outcome is CharterOutcome.PROPOSED
    assert record.lifecycle is CharterProposalLifecycle.AWAITING_APPROVAL
    assert (record.triage_class, record.triage_fingerprint) == (
        TriageClass.OPERATOR_DECISION, "needs-human",
    )


def test_an_approved_decision_plans_its_application() -> None:
    op = _plan(_decide(_split()), fingerprints={262: "needs-human"})[0][0]
    assert isinstance(op, CreateTechLeadProposalIssueAction)

    [action] = plan_approved_tech_lead_op_executions(
        (ApprovedTechLeadOp(proposal_issue_number=950, op=op.op),)
    )

    assert isinstance(action, ApplyOperatorDecisionAction)
    assert (action.issue_number, action.proposal_issue_number) == (262, 950)
    assert action.decision == op.op.decision


# -- applying the operator's approval ------------------------------------------


@dataclass
class _Host:
    issues: dict[int, Issue]
    markers: dict[str, int] = field(default_factory=dict)
    comments: list[tuple[int, str]] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)

    def get_issue(self, number: int) -> Issue | None:
        return self.issues.get(number)

    def find_issue_by_marker(self, *, title: str, marker: str, authoritative: bool = False) -> int | None:
        assert authoritative, "a miss must prove absence before a create"
        return self.markers.get(marker)

    def create_issue(self, *, title: str, body: str, labels=None, milestone=None) -> dict[str, Any]:
        number = 1000 + len(self.created)
        self.created.append({"title": title, "body": body, "labels": labels, "milestone": milestone})
        marker = body[body.index("<!--"):]
        self.markers[marker] = number
        return {"number": number}

    def comment_marker_present(self, number: int, marker: str) -> bool:
        return any(n == number and marker in body for n, body in self.comments)

    def apply(self, action):
        from issue_orchestrator.control.actions import ActionResult, AddCommentAction

        assert isinstance(action, AddCommentAction)
        self.comments.append((action.number, action.comment))
        return ActionResult.ok(action)


def _outcome(status: OperatorCommandStatus, **fields: Any) -> OperatorCommandOutcome:
    return OperatorCommandOutcome(
        intent=OperatorCommandIntent.RETRY, status=status, issue_number=262, **fields,
    )


def _executor(host: _Host, retry) -> OperatorDecisionExecutor:
    config = _config()
    return OperatorDecisionExecutor(
        events=MagicMock(), labels=LabelManager(config), read_issue=host.get_issue,
        retry_issue=retry, find_issue_by_marker=host.find_issue_by_marker,
        create_issue=host.create_issue, comment_marker_present=host.comment_marker_present,
        apply_action=host.apply,
    )


def _approved() -> ApplyOperatorDecisionAction:
    return ApplyOperatorDecisionAction(
        issue_number=262,
        decision=OperatorDecision(
            title="Split #262", body="Land the slice under Refs #262.",
            follow_ups=(DecisionFollowUp("Live D1 seller pickup index", "The remainder."),),
        ),
        proposal_id="A1", anchor_issue_number=950, proposal_issue_number=950,
    )


def test_approval_retries_files_the_split_and_posts_the_decision_once() -> None:
    target = _issue(262, "agent:backend", "priority:high", "needs-human", "pr-pending")
    target.milestone_number = 4
    host = _Host({262: target})
    retries: list[int] = []

    def retry(number: int) -> OperatorCommandOutcome:
        retries.append(number)
        return _outcome(OperatorCommandStatus.COMMITTED, removed=("needs-human",))

    executor = _executor(host, retry)
    first = executor.apply(_approved())
    again = executor.apply(_approved())  # a retried apply after a crash

    assert first.success and again.success
    assert retries == [262, 262]
    [follow_up] = host.created  # create-once by its body marker
    assert follow_up["title"] == "Live D1 seller pickup index"
    assert follow_up["labels"] == ["agent:backend", "priority:high"]  # never workflow state
    assert follow_up["milestone"] == 4
    assert follow_up_marker(950, 1) in follow_up["body"] and "Refs #262" in follow_up["body"]
    [(number, comment)] = host.comments  # create-once by its comment marker
    assert number == 262
    assert decision_marker(950) in comment and "#1000" in comment
    assert first.details["follow_up_issues"] == ["1000"]


def test_a_cause_the_retry_may_not_override_closes_the_decision_stale_with_no_writes() -> None:
    host = _Host({262: _issue(262, "agent:backend", "needs-human")})
    executor = _executor(host, lambda n: _outcome(
        OperatorCommandStatus.STILL_BLOCKED, blocked="needs-human", held_by=("claim_quarantine",),
    ))

    result = executor.apply(_approved())

    assert not result.success
    assert result.details["mode"] == "stale_downgrade"
    assert "claim_quarantine" in result.details["skip_reason"]
    assert host.created == [] and host.comments == []


def test_a_retry_github_would_not_settle_is_a_retried_failure() -> None:
    host = _Host({262: _issue(262, "agent:backend", "needs-human")})
    executor = _executor(host, lambda n: _outcome(
        OperatorCommandStatus.INCOMPLETE, failed=("blocked-failed",),
    ))

    result = executor.apply(_approved())

    assert not result.success and "mode" not in result.details
    assert host.created == [] and host.comments == []
