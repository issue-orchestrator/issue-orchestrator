"""An approved decision carries out its steps beyond the item (#8691, #8694).

Approving a ``propose_decision`` used to act on the ISSUE only. Every other
consequence was "Before you approve" prose the operator carried out by hand:
porchpin#327/#530 (retarget PR #525 to ``Refs #327``, take the PR's
needs-human off, send it back for rework) and #459 (move #326 and #327 to M1,
note the delivery plan in #326's body and a line in #262's, close the proposal
it superseded).

Driven end to end from the tech lead's decision through planning, the stored op
and approval, over the REAL operator-decision owner, the real shared-block
owner with a real cause store, the real standing-rulings owner and the real
scoped-rework owner. Only GitHub is a fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_results import ActionResult, ActionResultType
from issue_orchestrator.control.actions import (
    Action,
    AddCommentAction,
    AddLabelAction,
    ApplyOperatorDecisionAction,
    CloseIssueAction,
    CreateTechLeadProposalIssueAction,
    RemoveLabelAction,
    RequestReworkAction,
)
from issue_orchestrator.control.human_gates import merge_decision_request
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import (
    BlockOutcome,
    HumanBlockRequest,
    NeedsHumanBlock,
    NeedsHumanCause,
)
from issue_orchestrator.control.pending_work_successors import PendingWorkSuccessors
from issue_orchestrator.control.proposal_dedup_gate import DuplicateTargetGrant, OpenIssueCorpus
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.scoped_rework import RequestReworkExecutor
from issue_orchestrator.control.standing_rulings import StandingRulingsOwner
from issue_orchestrator.control.tech_lead_charter_records import CharterDecisionLog
from issue_orchestrator.control.tech_lead_decision_actions import plan_tech_lead_decision_actions
from issue_orchestrator.control.tech_lead_decision_steps import DecisionStepsOwner
from issue_orchestrator.control.tech_lead_operator_decision import OperatorDecisionExecutor
from issue_orchestrator.control.tech_lead_proposals import plan_approved_tech_lead_op_executions
from issue_orchestrator.domain.decision_steps import DecisionFollowThrough, DecisionStep, DecisionStepKind, step_marker
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.scoped_rework import ReworkTarget
from issue_orchestrator.domain.standing_ruling import parse_rulings_block
from issue_orchestrator.domain.tech_lead_approval import AWAITING_APPROVAL_LABEL, TECH_LEAD_PROPOSAL_LABEL
from issue_orchestrator.domain.tech_lead_artifacts import ProposedTechLeadAction, TechLeadDecision
from issue_orchestrator.domain.tech_lead_session import ApprovedTechLeadOp
from issue_orchestrator.execution.pending_work_claim_store import SqlitePendingWorkClaimStore
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.operator_issue_commands import (
    OperatorCommandIntent,
    OperatorCommandOutcome,
    OperatorCommandStatus,
)
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from tests.standing_ruling_helpers import InMemoryStandingRulingsIndex

REPO = "porchpin/porchpin"
ITEM, PR, PROPOSAL, ANCHOR = 327, 525, 530, 900
HEAD = "c0ffee" * 6 + "abcd"
OBSERVED = "2026-10-08T01:40:00+00:00"


@dataclass
class GitHub:
    """Issues and PRs as GitHub holds them (a PR is an issue with a head)."""

    labels: dict[int, set[str]] = field(default_factory=dict)
    bodies: dict[int, str] = field(default_factory=dict)
    states: dict[int, str] = field(default_factory=dict)
    milestones: dict[int, int | None] = field(default_factory=dict)
    comments: dict[int, list[str]] = field(default_factory=dict)
    prs: set[int] = field(default_factory=set)
    branches: dict[int, str] = field(default_factory=dict)
    open_milestones: list[dict[str, Any]] = field(
        default_factory=lambda: [{"number": 1, "title": "M1 - Surfaces"}, {"number": 3, "title": "M3 - Polish"}]
    )
    writes: list[str] = field(default_factory=list)

    def plant(self, number: int, *labels: str, body: str = "", milestone: int | None = 3) -> None:
        self.labels[number] = set(labels)
        self.bodies[number] = body
        self.states[number] = "open"
        self.milestones[number] = milestone

    def read(self, number: int) -> list[str]:
        return sorted(self.labels.get(number, set()))

    def add_label(self, number: int, label: str) -> None:
        self.writes.append(f"+{label}:{number}")
        self.labels.setdefault(number, set()).add(label)

    def remove_label(self, number: int, label: str) -> None:
        self.writes.append(f"-{label}:{number}")
        self.labels.setdefault(number, set()).discard(label)

    def get_issue(self, number: int) -> Issue | None:
        if number not in self.labels:
            return None
        return Issue(number=number, title=f"Issue {number}", labels=self.read(number), repo=REPO,
                     state=self.states[number], body=self.bodies[number],
                     milestone_number=self.milestones[number])

    def get_pr(self, number: int) -> PRInfo | None:
        if number not in self.prs:
            return None
        return PRInfo(number, f"PR {number}", f"https://github.com/{REPO}/pull/{number}",
                      self.branches.get(number, f"{ITEM}-slice"),
                      self.bodies[number], self.states[number], self.read(number), draft=True, head_sha=HEAD)

    def write_body(self, number: int, body: str) -> None:
        self.writes.append(f"body:{number}")
        self.bodies[number] = body

    def set_milestone(self, number: int, milestone: int) -> None:
        self.writes.append(f"milestone:{number}={milestone}")
        self.milestones[number] = milestone

    def marker_present(self, number: int, marker: str) -> bool:
        return any(marker in body for body in self.comments.get(number, []))

    issue_comment_marker_present = marker_present

    def add_comment(self, number: int, body: str) -> str:
        self.writes.append(f"comment:{number}")
        self.comments.setdefault(number, []).append(body)
        return "url"

    # The rework owner's forward-fix path (unused for an open PR).
    def find_issue_by_marker(self, **_: Any) -> None:
        return None

    def create_issue(self, **_: Any) -> dict[str, Any]:
        raise AssertionError("an approved decision with no follow-ups files no issue")


@dataclass
class World:
    tmp: Path
    github: GitHub = field(default_factory=GitHub)
    retried: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.config = Config()
        self.config.repo = REPO
        self.labels = LabelManager(self.config)
        self.causes = SqlitePendingWorkClaimStore(self.tmp / "causes.sqlite")
        self.block = NeedsHumanBlock("needs-human", "tech-lead-needs-human", self.github, self.github.read,
                                     frozenset, self.causes)
        self.authority = InMemoryTechLeadAuthorityStore()
        self.rework = RequestReworkExecutor(
            self.github, self.authority, self.labels, self.block, MagicMock(), lambda _n: False,  # type: ignore[arg-type]
            self._mutate, lambda _a: None,
            PendingWorkSuccessors(SqlitePendingWorkClaimStore(self.tmp / "claims.sqlite")),
        )

    def _mutate(self, parent: Action, action: Action) -> ActionResult:
        return self.apply(action)

    def apply(self, action: Action) -> ActionResult:
        if isinstance(action, AddCommentAction):
            self.github.add_comment(action.number, action.comment)
        elif isinstance(action, AddLabelAction):
            self.github.add_label(action.issue_number, action.label)
        elif isinstance(action, RemoveLabelAction):
            self.github.remove_label(action.issue_number, action.label)
        elif isinstance(action, CloseIssueAction):
            self.github.writes.append(f"close:{action.issue_number}")
            self.github.states[action.issue_number] = "closed"
            self.github.add_comment(action.issue_number, action.comment)
        elif isinstance(action, RequestReworkAction):
            return self.rework.apply(action)
        else:
            raise AssertionError(f"unexpected write {action}")
        return ActionResult.ok(action)

    def retry(self, number: int) -> OperatorCommandOutcome:
        """The operator's Retry: the item's own needs-human comes off."""
        self.retried.append(number)
        assert self.block.force_clear(number, "operator retry") is BlockOutcome.CLEARED
        return OperatorCommandOutcome(intent=OperatorCommandIntent.RETRY, status=OperatorCommandStatus.COMMITTED,
                                      issue_number=number, removed=("needs-human",))

    def executor(self) -> OperatorDecisionExecutor:
        rulings = StandingRulingsOwner(read_issue=self.github.get_issue, write_body=self.github.write_body,
                                       index=InMemoryStandingRulingsIndex())
        return OperatorDecisionExecutor(
            events=MagicMock(), labels=self.labels, read_issue=self.github.get_issue, retry_issue=self.retry,
            unsettleable_holders=lambda number: (), find_issue_by_marker=self.github.find_issue_by_marker,
            create_issue=self.github.create_issue, comment_marker_present=self.github.marker_present,
            apply_action=self.apply, require_authority=lambda action, number: None,
            retries=self.authority, rulings=rulings,
            steps=DecisionStepsOwner(
                read_issue=self.github.get_issue, read_pr=self.github.get_pr,
                list_milestones=lambda: list(self.github.open_milestones),
                set_milestone=self.github.set_milestone, write_body=self.github.write_body,
                comment_marker_present=self.github.marker_present, apply_action=self.apply,
                require_authority=lambda action, number: None, rulings=rulings, block=self.block,
                repo_slug=REPO,
            ),
        )

    def plan(self, proposed: ProposedTechLeadAction) -> CreateTechLeadProposalIssueAction:
        """The tech lead's decision, planned as the engine plans it."""
        target = ReworkTarget(REPO, PR, ITEM, HEAD, f"{ITEM}-slice", tuple(self.github.read(PR)),
                              tuple(self.github.read(ITEM)))
        actions = plan_tech_lead_decision_actions(
            TechLeadDecision(summary="health review", proposed_actions=(proposed,)), self.config, self.labels,
            anchor_issue=Issue(ANCHOR, "Health review", ["agent:tech-lead"], repo=REPO),
            expected=build_expected_for_mutation(), op_ledger={}, pattern_ledger={},
            source_run_id="run-1", source_session_name="tech-lead-900", observed_at=OBSERVED,
            observed_session_generation=lambda _n: None, rework_targets=(target,),
            report_text="T1: #327's rework asks for a ruling amendment and a PR re-route.",
            dedup_corpus=OpenIssueCorpus.ready(()), dedup_grant=DuplicateTargetGrant.of(frozenset()),
            charter_log=CharterDecisionLog(run_id="run-1", anchor_issue_number=ANCHOR, decided_at=OBSERVED),
        )
        [proposal] = [a for a in actions if isinstance(a, CreateTechLeadProposalIssueAction)]
        return proposal

    def approve(self, proposal: CreateTechLeadProposalIssueAction) -> ApplyOperatorDecisionAction:
        [action] = plan_approved_tech_lead_op_executions((ApprovedTechLeadOp(PROPOSAL, proposal.op),))
        assert isinstance(action, ApplyOperatorDecisionAction)
        return action


def _decision(*steps: dict[str, Any], operator_steps: tuple[str, ...] = ()) -> ProposedTechLeadAction:
    return ProposedTechLeadAction.from_mapping({
        "id": "A2", "action_type": "propose_decision", "target_number": ITEM,
        "title": "Amend ruling m-55cb838d2f51: 'exactly' means the two named fields",
        "body": "The rework agent asked for the amendment. PR #525 delivers part of #327 under Refs #327.",
        "steps": list(steps), "operator_steps": list(operator_steps),
    }, index=1)


def _plant_327(world: World) -> None:
    """porchpin#327 on 2026-10-08: the issue blocked on its agent's question,
    draft PR #525 'Closes #327' held by a forced completion's merge hold."""
    github = world.github
    github.plant(ITEM, "agent:backend", "needs-human", body="## Spec\nBuyer contact index.")
    github.plant(PR, "needs-human", "code-reviewed", body="Closes #327\n\nThe buyer contact index.")
    github.prs.add(PR)
    github.plant(PROPOSAL, TECH_LEAD_PROPOSAL_LABEL, "approved", body="proposal")
    assert world.block.acquire(HumanBlockRequest(ITEM, NeedsHumanCause.AGENT_COMPLETION, "asked")) is BlockOutcome.HELD
    assert world.block.acquire(merge_decision_request(PR, "forced completion pr_labels")) is BlockOutcome.HELD


def test_approving_the_327_decision_routes_its_pr_with_no_hand_edits(tmp_path: Path) -> None:
    """The improver's reproduction (#8691): after approval PR #525 refs #327,
    its needs-human is off, its rework is queued, and #327 is released."""
    world = World(tmp_path)
    _plant_327(world)
    proposal = world.plan(_decision({"kind": "retarget_pr", "number": PR},
                                    {"kind": "request_pr_rework", "number": PR}))

    result = world.executor().apply(world.approve(proposal))

    assert result.success, result.error
    github = world.github
    assert "Closes #327" not in github.bodies[PR] and github.bodies[PR].startswith("Refs #327")
    assert "needs-human" not in github.labels[PR]
    assert world.labels.needs_rework in github.labels[PR]
    [receipt] = world.authority.list_rework_receipts()
    assert (receipt.status, receipt.proposal_issue_number) == ("queued", PROPOSAL)
    assert "Amend ruling m-55cb838d2f51" in receipt.request.feedback  # the decision is the brief
    assert "needs-human" not in github.labels[ITEM] and world.retried == [ITEM]
    assert all(github.marker_present(PROPOSAL, step_marker(str(PROPOSAL), index)) for index in (1, 2))


def test_the_rework_runs_after_the_release_and_every_step_runs_once(tmp_path: Path) -> None:
    """The engine refuses a blocked issue's rework, so the rework follows the
    retry; a replay of the op (its applied marker failed) writes nothing again."""
    world = World(tmp_path)
    _plant_327(world)
    proposal = world.plan(_decision({"kind": "retarget_pr", "number": PR},
                                    {"kind": "request_pr_rework", "number": PR}))
    action = world.approve(proposal)
    executor = world.executor()

    executor.apply(action)
    order = list(world.github.writes)
    again = executor.apply(action)

    retarget, needs_rework = order.index(f"body:{PR}"), order.index(f"+needs-rework:{PR}")
    released = order.index(f"-needs-human:{ITEM}")
    assert retarget < released < needs_rework
    assert again.success and again.details.get("replayed") is True
    assert world.github.writes == order  # nothing ran twice
    assert world.retried == [ITEM]


def test_a_step_that_no_longer_applies_refuses_the_decision_before_any_write(tmp_path: Path) -> None:
    """It never half-applies: PR #525 was closed after the proposal was filed,
    so nothing is written (no comment, no ruling, no retarget, no retry) and the
    proposal closes stale saying which step and why."""
    world = World(tmp_path)
    _plant_327(world)
    proposal = world.plan(_decision({"kind": "set_milestone", "number": ITEM, "milestone": "M1 - Surfaces"},
                                    {"kind": "retarget_pr", "number": PR}))
    world.github.states[PR] = "closed"
    before = list(world.github.writes)

    result = world.executor().apply(world.approve(proposal))

    assert result.result_type is ActionResultType.SKIPPED
    assert "step 2 (retarget_pr #525)" in result.details["skip_reason"]
    assert "PR #525 is closed" in result.details["skip_reason"]
    assert world.github.writes == before and world.retried == []
    assert world.github.milestones[ITEM] == 3


# -- porchpin#459 ---------------------------------------------------------------

SIBLING, PARENT, SUPERSEDED = 327, 262, 445
SUBJECT = 326
DELIVERY_PLAN = "Delivery plan (decided 2026-10-03): this issue is the per-pickup half of seller deletion."
PARENT_NOTE = "The deletion fence that meets this bullet is built by the route issue split from #326."


def _plant_459(world: World) -> ProposedTechLeadAction:
    github = world.github
    github.plant(SUBJECT, "agent:backend", "needs-human", body="## Scope\nSeller deletion.")
    github.plant(SIBLING, "agent:backend", body="Buyer deletion.")
    github.plant(PARENT, "agent:backend", body="## Acceptance\n- Verified deletion stops delayed writes.",
                 milestone=1)
    github.plant(SUPERSEDED, TECH_LEAD_PROPOSAL_LABEL, AWAITING_APPROVAL_LABEL, body="older proposal")
    github.plant(PROPOSAL, TECH_LEAD_PROPOSAL_LABEL, "approved", body="proposal")
    assert world.block.acquire(HumanBlockRequest(SUBJECT, NeedsHumanCause.AGENT_COMPLETION, "asked")) is BlockOutcome.HELD
    return ProposedTechLeadAction.from_mapping({
        "id": "A1", "action_type": "propose_decision", "target_number": SUBJECT,
        "title": "Split #326: build the deletion command now; the route waits on #262",
        "body": "Recommendation: split.",
        "steps": [
            {"kind": "set_milestone", "number": SUBJECT, "milestone": "M1 - Surfaces"},
            {"kind": "set_milestone", "number": SIBLING, "milestone": "M1 - Surfaces"},
            {"kind": "record_ruling", "number": SUBJECT, "text": DELIVERY_PLAN},
            {"kind": "record_ruling", "number": PARENT, "text": PARENT_NOTE},
            {"kind": "close_superseded_proposal", "number": SUPERSEDED},
        ],
        "operator_steps": ["Raise the CI ceiling in .github/workflows/ci.yml (the bot may not push workflows)."],
    }, index=1)


def test_the_459_decision_carries_out_every_hand_step_exactly_once(tmp_path: Path) -> None:
    world = World(tmp_path)
    proposed = _plant_459(world)
    proposal = world.plan(proposed)
    action = world.approve(proposal)
    executor = world.executor()

    first = executor.apply(action)
    writes = list(world.github.writes)
    executor.apply(action)

    github = world.github
    assert first.success, first.error
    assert (github.milestones[SUBJECT], github.milestones[SIBLING]) == (1, 1)
    assert DELIVERY_PLAN in {r.text for r in parse_rulings_block(github.bodies[SUBJECT])}
    assert PARENT_NOTE in {r.text for r in parse_rulings_block(github.bodies[PARENT])}
    assert github.states[SUPERSEDED] == "closed"
    assert world.retried == [SUBJECT]
    # Every step lands before the release, so the resumed session sees them.
    assert writes.index(f"close:{SUPERSEDED}") < writes.index(f"-needs-human:{SUBJECT}")
    assert github.writes == writes  # the replay wrote nothing
    applied = [body for body in github.comments[PROPOSAL] if "io:decision-step:" in body]
    assert len(applied) == 5 and all(body.startswith(f"Step {i} applied") for i, body in enumerate(applied, 1))


def test_the_proposal_shows_the_steps_and_the_operator_checklist(tmp_path: Path) -> None:
    world = World(tmp_path)
    proposal = world.plan(_plant_459(world))

    body = proposal.body
    assert "### Steps approval executes" in body
    assert "1. Moves #326 to milestone `M1 - Surfaces`." in body
    assert "5. Closes proposal #445, which this decision supersedes." in body
    assert "### You do by hand (io cannot)\n\n- [ ] Raise the CI ceiling" in body
    assert proposal.op.follow_through.operator_steps == (
        "Raise the CI ceiling in .github/workflows/ci.yml (the bot may not push workflows).",
    )


def test_a_closed_proposal_is_not_reopened_and_an_unknown_milestone_refuses(tmp_path: Path) -> None:
    world = World(tmp_path)
    proposed = _plant_459(world)
    world.github.open_milestones = [{"number": 3, "title": "M3 - Polish"}]

    result = world.executor().apply(world.approve(world.plan(proposed)))

    assert result.result_type is ActionResultType.SKIPPED
    assert "no open milestone is named 'M1 - Surfaces'" in result.details["skip_reason"]
    assert world.retried == [] and world.github.states[SUPERSEDED] == "open"


def test_closing_an_issue_that_is_not_a_waiting_proposal_refuses(tmp_path: Path) -> None:
    world = World(tmp_path)
    proposed = _plant_459(world)
    world.github.labels[SUPERSEDED] = {"agent:backend"}  # ordinary work, not a proposal

    result = world.executor().apply(world.approve(world.plan(proposed)))

    assert "#445 is not a tech-lead proposal awaiting approval" in result.details["skip_reason"]
    assert world.github.states[SUPERSEDED] == "open"


# -- the agent's contract -------------------------------------------------------


def test_anything_io_cannot_do_is_not_a_step() -> None:
    with pytest.raises(ValueError, match="anything else is an operator step"):
        _decision({"kind": "edit_workflow", "number": 1})


def test_steps_belong_to_decisions_only() -> None:
    with pytest.raises(ValueError, match="only valid on propose_decision or resolve_block"):
        ProposedTechLeadAction.from_mapping({
            "id": "A1", "action_type": "post_comment", "target_number": ITEM, "body": "x",
            "steps": [{"kind": "comment", "number": 1, "text": "hi"}],
        }, index=1)


def test_a_decision_never_closes_its_own_item() -> None:
    with pytest.raises(ValueError, match="would close the decision's own item"):
        _decision({"kind": "close_superseded_proposal", "number": ITEM})


def test_the_agent_cannot_bind_a_rework_head() -> None:
    with pytest.raises(ValueError, match="unknown fields"):
        _decision({"kind": "request_pr_rework", "number": PR, "rework": {"head_sha": "f" * 40}})


def test_a_stored_follow_through_round_trips() -> None:
    follow_through = DecisionFollowThrough(
        steps=(DecisionStep(DecisionStepKind.COMMENT, 12, text="See #326", on_pr=True),),
        operator_steps=("Edit the workflow",),
    )
    assert DecisionFollowThrough.from_dict(follow_through.to_dict()) == follow_through


# -- the charter and the launch scope -------------------------------------------


def _answer_with_steps(*steps: dict[str, Any]) -> ProposedTechLeadAction:
    return ProposedTechLeadAction.from_mapping({
        "id": "A1", "action_type": "resolve_block", "target_number": ITEM, "body": "The spec answers it.",
        "resolution": {"kind": "answer", "causes": ["agent_completion"], "title": "Build the D1 index",
                       "body": "ADR-0009 rules it.", "evidence": ["#327 body: ADR-0009"]},
        "steps": list(steps),
    }, index=1)


def test_a_decision_with_steps_waits_for_the_operator_even_under_execute(tmp_path: Path) -> None:
    """``resolve_block: execute`` lets a bare answer run unattended; one that also
    acts beyond its item (here, its PR) is the operator's to approve."""
    from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
    from issue_orchestrator.domain.tech_lead_charter import CharterOutcome, CharterReason

    world = World(tmp_path)
    world.config.tech_lead.authority.resolve_block = "execute"
    policy = TechLeadCharterPolicy.from_config(world.config)
    bare = _answer_with_steps()
    routed = _answer_with_steps({"kind": "retarget_pr", "number": PR})

    assert policy.decide_for(bare).executes
    verdict = policy.decide_for(routed)
    assert (verdict.outcome, verdict.reason_code) == (
        CharterOutcome.PROPOSED, CharterReason.FOLLOW_THROUGH_REQUIRES_APPROVAL,
    )
    _plant_327(world)
    proposal = world.plan(routed)
    assert proposal.op.op_type == "resolve_block" and proposal.op.follow_through.steps
    assert "Rewrites PR #525's `Closes #327` to `Refs #327`" in proposal.body


def test_a_pr_rework_step_needs_the_pr_the_session_observed() -> None:
    from issue_orchestrator.control.tech_lead_target_scope import target_scope_violation
    from issue_orchestrator.domain.blocked_item_triage import TriageGrant
    from issue_orchestrator.domain.tech_lead_session import TechLeadLaunchAuthority, TechLeadSessionFlavor

    authority = TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW, anchor_issue_number=ANCHOR,
        triage_grants=(TriageGrant(ITEM, "needs-human"),),
    )
    decision = TechLeadDecision(summary="s", proposed_actions=(
        _decision({"kind": "request_pr_rework", "number": PR}),
    ))

    violation = target_scope_violation(decision, authority)

    assert violation is not None and "request_pr_rework step for PR #525" in violation


# -- the prompt -------------------------------------------------------------------


def test_both_prompts_teach_every_step_kind_and_forbid_before_you_approve_prose() -> None:
    """The tech lead's built-in prompt, this repository's own tech-lead prompt
    (the one the exam runs) and the engine's restatement in every health review
    all name each admitted kind and route the rest to operator_steps."""
    from issue_orchestrator.domain.blocked_item_triage import TriageAgenda, render_triage_instructions
    from issue_orchestrator.domain.blocked_item_triage import TriageAgendaItem
    from issue_orchestrator.execution.tech_lead_artifacts_prompt import TECH_LEAD_ARTIFACTS_SECTION

    health = render_triage_instructions(TriageAgenda(items=(TriageAgendaItem(
        issue_number=ITEM, title="t", labels=("needs-human",), blocking_labels=("needs-human",),
        needs_human_causes=("agent_completion",), fingerprint="f", agent_question=None, reason="r",
        prior=None,
    ),)))
    repo_prompt = (Path(__file__).resolve().parents[3] / "repo-specific" / "prompts" / "tech-lead.md").read_text()
    for prompt in (TECH_LEAD_ARTIFACTS_SECTION, health, repo_prompt):
        for kind in DecisionStepKind:
            assert f'"kind": "{kind.value}"' in prompt, kind
        assert "`operator_steps`" in prompt
        assert 'Never write "Before you approve"' in prompt


# -- review round 1 ---------------------------------------------------------------


def test_the_same_decision_with_other_steps_is_a_new_proposal(tmp_path: Path) -> None:
    """r1 F1: approval runs the stored steps, so different steps are a different
    proposal, never a reuse of one that would run the old steps."""
    from issue_orchestrator.control.tech_lead_proposals import build_op_ledger, proposal_ledger_key

    world = World(tmp_path)
    _plant_327(world)
    first = world.plan(_decision({"kind": "retarget_pr", "number": PR}))
    other = world.plan(_decision({"kind": "comment", "number": PR, "on_pr": True, "text": "see #327"}))
    bare = world.plan(_decision())
    ledger = build_op_ledger([(PROPOSAL, first.op)])

    def key(op):
        return proposal_ledger_key(op.op_type, op.target_issue_number, decision=op.decision,
                                   follow_through=op.follow_through)

    assert key(first.op) in ledger and key(other.op) not in ledger and key(bare.op) not in ledger
    assert key(bare.op) == proposal_ledger_key("propose_decision", ITEM, decision=bare.op.decision)


def test_a_pr_owned_by_another_issue_is_never_retargeted(tmp_path: Path) -> None:
    """r1 F2: a later 'Closes #327' in #999's PR does not make it #327's PR."""
    world = World(tmp_path)
    _plant_327(world)
    proposal = world.plan(_decision({"kind": "retarget_pr", "number": PR}))
    world.github.bodies[PR] = "Closes #999\n\nAlso Closes #327"
    world.github.get_pr = lambda n, _g=world.github.get_pr: (  # type: ignore[method-assign]
        None if (pr := _g(n)) is None else PRInfo(pr.number, pr.title, pr.url, "999-other", pr.body, pr.state,
                                                pr.labels, draft=True, head_sha=HEAD))
    before = list(world.github.writes)

    result = world.executor().apply(world.approve(proposal))

    assert "PR #525 belongs to #999, not #327" in result.details["skip_reason"]
    assert world.github.writes == before


def test_a_ruling_step_is_behind_the_mutation_authority_check(tmp_path: Path) -> None:
    """r1 F3: another issue's body is written only after its authority check."""
    from issue_orchestrator.control.claim_gate import ClaimLostError

    world = World(tmp_path)
    proposed = _plant_459(world)
    executor = world.executor()

    def refuse(action: Action, number: int) -> None:
        if number == PARENT:
            raise ClaimLostError(number, "record_ruling")

    executor = _with_steps_authority(executor, refuse)
    with pytest.raises(ClaimLostError):
        executor.apply(world.approve(world.plan(proposed)))
    assert parse_rulings_block(world.github.bodies[PARENT]) == ()


def _with_steps_authority(executor: OperatorDecisionExecutor, check) -> OperatorDecisionExecutor:
    from dataclasses import replace as dc_replace

    return dc_replace(executor, steps=dc_replace(executor.steps, require_authority=check))


def test_a_precondition_broken_at_write_time_stops_unmarked_and_unreleased(tmp_path: Path) -> None:
    """r1 F4: PR #525 closes between the check and the retarget: nothing after
    it runs, the step is not marked, #327 is not released; the replay hands it back."""
    world = World(tmp_path)
    _plant_327(world)
    proposal = world.plan(_decision({"kind": "retarget_pr", "number": PR},
                                    {"kind": "comment", "number": ITEM, "text": "routed"}))
    reads = {"n": 0}
    real = world.github.get_pr

    def closes_on_second_read(number: int) -> PRInfo | None:
        reads["n"] += 1
        if reads["n"] >= 2:
            world.github.states[PR] = "closed"
        return real(number)

    world.github.get_pr = closes_on_second_read  # type: ignore[method-assign]
    executor = world.executor()
    action = world.approve(proposal)

    first = executor.apply(action)
    replay = executor.apply(action)

    assert not first.success and "no longer applies" in (first.error or "")
    assert not world.github.marker_present(PROPOSAL, step_marker(str(PROPOSAL), 1))
    assert "routed" not in "".join(world.github.comments.get(ITEM, []))
    assert world.retried == [] and "needs-human" in world.github.labels[ITEM]
    # The decision's own comment and ruling already landed (r5 F1): handed back, never "no changes".
    assert replay.result_type is ActionResultType.FAILURE
    assert "partly applied" in (replay.error or "") and "PR #525 is closed" in (replay.error or "")


def test_an_approved_proposal_is_never_closed_as_superseded(tmp_path: Path) -> None:
    """r1 F5: a proposal carrying a maintainer's `approved` is the operator's."""
    world = World(tmp_path)
    proposed = _plant_459(world)
    world.github.labels[SUPERSEDED].add("approved")

    result = world.executor().apply(world.approve(world.plan(proposed)))

    assert "#445 is not a tech-lead proposal awaiting approval" in result.details["skip_reason"]
    assert world.github.states[SUPERSEDED] == "open"


# -- review round 2 ---------------------------------------------------------------


def test_an_operator_checklist_alone_still_waits_for_the_operator(tmp_path: Path) -> None:
    """r2 F1: an execute-authority answer whose only follow-through is the
    operator's checklist must be shown to the operator before it runs."""
    from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy

    world = World(tmp_path)
    world.config.tech_lead.authority.resolve_block = "execute"
    checklist_only = ProposedTechLeadAction.from_mapping({
        "id": "A1", "action_type": "resolve_block", "target_number": ITEM, "body": "The spec answers it.",
        "resolution": {"kind": "answer", "causes": ["agent_completion"], "title": "Build the D1 index",
                       "body": "ADR-0009 rules it.", "evidence": ["#327 body: ADR-0009"]},
        "operator_steps": ["Raise the CI ceiling in ci.yml"],
    }, index=1)

    assert not TechLeadCharterPolicy.from_config(world.config).decide_for(checklist_only).executes


def test_a_step_broken_after_an_earlier_one_applied_is_handed_back_not_closed_stale(tmp_path: Path) -> None:
    """r2 F2: step 1 moved #326; #327 closes before step 2. The replay must not
    close the proposal as 'no changes were made': it is handed to the operator."""
    world = World(tmp_path)
    proposed = _plant_459(world)
    action = world.approve(world.plan(proposed))
    real = world.github.set_milestone

    def move_then_close_sibling(number: int, milestone: int) -> None:
        real(number, milestone)
        world.github.states[SIBLING] = "closed"

    world.github.set_milestone = move_then_close_sibling  # type: ignore[method-assign]
    executor = world.executor()

    first = executor.apply(action)
    replay = executor.apply(action)

    assert not first.success and world.github.milestones[SUBJECT] == 1
    assert replay.result_type is ActionResultType.FAILURE
    assert "and step(s) 1" in (replay.error or "") and "#327 is closed" in (replay.error or "")
    assert world.retried == []


def test_an_unwritable_rulings_block_refuses_before_any_write(tmp_path: Path) -> None:
    """r2 F3: a malformed rulings block on step 4's target is found before step 1 moves anything."""
    world = World(tmp_path)
    proposed = _plant_459(world)
    world.github.bodies[PARENT] += "\n\n<!-- io:standing-rulings:begin -->\nbroken"
    before = list(world.github.writes)

    result = world.executor().apply(world.approve(world.plan(proposed)))

    assert result.result_type is ActionResultType.SKIPPED
    assert "the rulings this decision records on #262 cannot all be recorded" in result.details["skip_reason"]
    assert world.github.writes == before and world.github.milestones[SUBJECT] == 3


def test_every_step_target_is_authority_checked_before_the_first_write(tmp_path: Path) -> None:
    """r2 F3: a claim on step 4's target stops the decision before step 1 writes."""
    from issue_orchestrator.control.claim_gate import ClaimLostError

    world = World(tmp_path)
    proposed = _plant_459(world)

    def refuse(action: Action, number: int) -> None:
        if number == PARENT:
            raise ClaimLostError(number, "decision step")

    executor = _with_steps_authority(world.executor(), refuse)
    with pytest.raises(ClaimLostError):
        executor.apply(world.approve(world.plan(proposed)))
    assert world.github.milestones[SUBJECT] == 3


def test_a_closing_split_cannot_rework_its_pr() -> None:
    """r2 F4: the rework of a closed issue's PR never launches."""
    with pytest.raises(ValueError, match="closes its item"):
        ProposedTechLeadAction.from_mapping({
            "id": "A1", "action_type": "resolve_block", "target_number": ITEM, "body": "split",
            "resolution": {"kind": "split", "causes": ["agent_completion"], "title": "Split", "body": "b",
                           "evidence": ["spec"], "parent": "close",
                           "children": [{"title": "child", "body": "rest"}]},
            "steps": [{"kind": "request_pr_rework", "number": PR}],
        }, index=1)


# -- review round 3 ---------------------------------------------------------------


def test_a_step_whose_marker_failed_is_never_read_as_no_changes(tmp_path: Path) -> None:
    """r3 F1: step 1 moves #326 and its applied marker fails; #327 then closes.
    The replay must hand the decision back as partial, never close it stale."""
    world = World(tmp_path)
    proposed = _plant_459(world)
    action = world.approve(world.plan(proposed))
    real_apply = world.apply

    def marker_fails(act: Action) -> ActionResult:
        if isinstance(act, AddCommentAction) and step_marker(str(PROPOSAL), 1) in act.comment:
            world.github.states[SIBLING] = "closed"
            return ActionResult.fail(act, "502")
        return real_apply(act)

    world.apply = marker_fails  # type: ignore[method-assign]
    executor = world.executor()

    first = executor.apply(action)
    world.apply = real_apply  # type: ignore[method-assign]
    replay = world.executor().apply(action)

    assert not first.success and world.github.milestones[SUBJECT] == 1
    assert replay.result_type is ActionResultType.FAILURE and "and step(s) 1" in (replay.error or "")


def test_rulings_that_fit_one_by_one_but_not_together_refuse_before_any_write(tmp_path: Path) -> None:
    """r3 F2: two notes on one body are preflighted together."""
    from issue_orchestrator.domain.standing_ruling import with_rulings_block

    world = World(tmp_path)
    proposed = _plant_459(world)
    note = "x" * 9_000
    steps = [{"kind": "record_ruling", "number": PARENT, "text": note},
             {"kind": "record_ruling", "number": PARENT, "text": note + "y"},
             {"kind": "set_milestone", "number": SIBLING, "milestone": "M1 - Surfaces"}]
    proposed = ProposedTechLeadAction.from_mapping({**proposed.to_dict(), "steps": steps}, index=1)
    # Fill the body so one note fits and two do not (GitHub's 65 536-character cap).
    base = world.github.bodies[PARENT]
    probe = len(with_rulings_block(base, ()))
    world.github.bodies[PARENT] = base + "\n" + "p" * (65_536 - probe - 9_000 - 2_000)
    before = list(world.github.writes)

    result = world.executor().apply(world.approve(world.plan(proposed)))

    assert result.result_type is ActionResultType.SKIPPED, result
    assert "cannot all be recorded" in result.details["skip_reason"]
    assert world.github.writes == before


def test_a_refused_rework_keeps_the_prs_merge_hold(tmp_path: Path) -> None:
    """r3 F3: the PR head moves after the check: the rework owner refuses it,
    and the merge hold stays on, since nothing was queued."""
    world = World(tmp_path)
    _plant_327(world)
    proposal = world.plan(_decision({"kind": "request_pr_rework", "number": PR}))
    real_apply = world.apply

    def head_moves_first(act: Action) -> ActionResult:
        if isinstance(act, RequestReworkAction):
            world.github.get_pr = lambda n, _g=world.github.get_pr: (  # type: ignore[method-assign]
                None if (pr := _g(n)) is None else PRInfo(pr.number, pr.title, pr.url, pr.branch, pr.body,
                                                        pr.state, pr.labels, draft=True, head_sha="f" * 40))
        return real_apply(act)

    world.apply = head_moves_first  # type: ignore[method-assign]
    result = world.executor().apply(world.approve(proposal))

    assert "needs-human" in world.github.labels[PR]
    assert "Refused at write time: step 1" in "".join(world.github.comments[PROPOSAL])
    assert result.success  # the item was released; the refused step is on the record


# -- review round 4 ---------------------------------------------------------------


def test_steps_run_in_the_order_listed_so_a_rework_must_come_last() -> None:
    """r4 F1: a PR rework runs after the item's release; listing it before
    another step would run them out of the order the operator approved."""
    with pytest.raises(ValueError, match="must come after every other step"):
        _decision({"kind": "request_pr_rework", "number": PR},
                  {"kind": "comment", "number": ITEM, "text": "routed"})
    _decision({"kind": "comment", "number": ITEM, "text": "routed"},
              {"kind": "request_pr_rework", "number": PR})



# -- review round 5 ---------------------------------------------------------------


def test_a_refusal_after_the_decisions_own_writes_is_partial_not_stale(tmp_path: Path) -> None:
    """r5 F1: the decision ruling landed, step 1's start marker failed, and its
    target closed before the replay: never "No changes were made"."""
    from issue_orchestrator.domain.decision_steps import step_started_marker

    world = World(tmp_path)
    proposed = _plant_459(world)
    action = world.approve(world.plan(proposed))
    real_apply = world.apply

    def start_fails(act: Action) -> ActionResult:
        if isinstance(act, AddCommentAction) and step_started_marker(str(PROPOSAL), 1) in act.comment:
            world.github.states[SUBJECT] = "closed"
            return ActionResult.fail(act, "502")
        return real_apply(act)

    world.apply = start_fails  # type: ignore[method-assign]
    first = world.executor().apply(action)
    world.apply = real_apply  # type: ignore[method-assign]
    world.github.states[SUBJECT] = "open"
    world.github.states[SIBLING] = "closed"
    replay = world.executor().apply(action)

    assert not first.success and parse_rulings_block(world.github.bodies[SUBJECT])  # the decision's ruling landed
    assert replay.result_type is ActionResultType.FAILURE
    assert "partly applied: its own writes" in (replay.error or "")


def test_a_rework_refused_after_the_release_is_reported_not_hidden(tmp_path: Path) -> None:
    """r5 F2: the refused step is in the result (and so the proposal's closing
    comment), on the first run and on a replay, from its durable marker."""
    from issue_orchestrator.control.tech_lead_proposal_execution import _terminal_outcome_comment

    world = World(tmp_path)
    _plant_327(world)
    proposal = world.plan(_decision({"kind": "request_pr_rework", "number": PR}))
    real_apply = world.apply

    def head_moves_first(act: Action) -> ActionResult:
        if isinstance(act, RequestReworkAction):
            world.github.get_pr = lambda n, _g=world.github.get_pr: (  # type: ignore[method-assign]
                None if (pr := _g(n)) is None else PRInfo(pr.number, pr.title, pr.url, pr.branch, pr.body,
                                                        pr.state, pr.labels, draft=True, head_sha="f" * 40))
        return real_apply(act)

    world.apply = head_moves_first  # type: ignore[method-assign]
    action = world.approve(proposal)
    first = world.executor().apply(action)
    replay = world.executor().apply(action)

    for result in (first, replay):
        assert result.details["steps_refused"] == ["step 1 (request_pr_rework #525)"]
        comment = _terminal_outcome_comment(result, "propose_decision", ITEM)
        assert comment is not None and "Not applied: step 1 (request_pr_rework #525)" in comment


# -- review round 6 ---------------------------------------------------------------


def test_a_begun_decision_whose_item_unblocked_is_handed_back_not_stale(tmp_path: Path) -> None:
    """r6 F1: the decision's ruling landed and a step failed; then someone took
    #326's block off. The replay's item refusal is still partial, never stale."""
    world = World(tmp_path)
    proposed = _plant_459(world)
    action = world.approve(world.plan(proposed))
    real = world.github.set_milestone

    def fails(number: int, milestone: int) -> None:
        raise RuntimeError("GitHub 502")

    world.github.set_milestone = fails  # type: ignore[method-assign]
    first = world.executor().apply(action)
    world.github.set_milestone = real  # type: ignore[method-assign]
    world.block.force_clear(SUBJECT, "a person took it off")

    replay = world.executor().apply(action)

    assert not first.success and parse_rulings_block(world.github.bodies[SUBJECT])
    assert replay.result_type is ActionResultType.FAILURE and "no longer blocked" in (replay.error or "")
    assert "partly applied" in (replay.error or "")


def test_a_proposal_approved_during_its_step_is_not_closed(tmp_path: Path) -> None:
    """r6 F2: a maintainer approves #445 while its step starts: it stays open."""
    from issue_orchestrator.domain.decision_steps import step_started_marker

    world = World(tmp_path)
    proposed = _plant_459(world)
    real_apply = world.apply

    def approve_on_start(act: Action) -> ActionResult:
        if isinstance(act, AddCommentAction) and step_started_marker(str(PROPOSAL), 5) in act.comment:
            world.github.labels[SUPERSEDED].add("approved")
        return real_apply(act)

    world.apply = approve_on_start  # type: ignore[method-assign]
    world.executor().apply(world.approve(world.plan(proposed)))

    assert world.github.states[SUPERSEDED] == "open"
    assert all(not w.startswith(f"close:{SUPERSEDED}") for w in world.github.writes)


def test_a_pr_that_changed_owner_during_its_step_is_not_edited(tmp_path: Path) -> None:
    """r6 F3: PR #525 moves to #999's branch while its step starts: its body is untouched."""
    from issue_orchestrator.domain.decision_steps import step_started_marker

    world = World(tmp_path)
    _plant_327(world)
    proposal = world.plan(_decision({"kind": "retarget_pr", "number": PR}))
    real_apply = world.apply

    def moves_on_start(act: Action) -> ActionResult:
        if isinstance(act, AddCommentAction) and step_started_marker(str(PROPOSAL), 1) in act.comment:
            world.github.branches[PR] = "999-other"
        return real_apply(act)

    world.apply = moves_on_start  # type: ignore[method-assign]
    world.executor().apply(world.approve(proposal))

    assert world.github.bodies[PR].startswith("Closes #327")
    assert f"body:{PR}" not in world.github.writes
