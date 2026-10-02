"""``resolve_block`` through the charter and the gated-proposal lifecycle (#7658).

The operator empowers the tech lead through config alone:
``tech_lead.authority.resolve_block`` defaults to ``propose`` (an approvable
proposal that carries the exact decision) and may be set to ``execute`` (the
typed owner command runs directly). Approval runs exactly the stored decision.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from issue_orchestrator.control.actions import (
    CreateTechLeadProposalIssueAction,
    ResolveBlockAction,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.proposal_dedup_gate import DuplicateTargetGrant, OpenIssueCorpus
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.control.tech_lead_decision_actions import (
    plan_tech_lead_decision_actions,
)
from issue_orchestrator.control.tech_lead_proposals import (
    build_op_ledger,
    plan_approved_tech_lead_op_executions,
    proposal_ledger_key,
)
from issue_orchestrator.domain.block_resolution import BlockResolution
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.tech_lead_artifacts import (
    TRIAGE_CLASS_ACTION_TYPES,
    ProposedTechLeadAction,
    TechLeadDecision,
    TechLeadFinding,
    TriageClass,
)
from issue_orchestrator.domain.tech_lead_charter import (
    CharterBinding,
    CharterDepth,
    CharterOutcome,
    CharterRole,
    classify_charter_action,
)
from issue_orchestrator.domain.tech_lead_session import ApprovedTechLeadOp, StoredTechLeadOp
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models_tech_lead import TechLeadAuthorityConfig

OBSERVED = "2026-10-02T06:00:00+00:00"
RESOLUTION = {
    "kind": "split",
    "causes": ["agent_completion", "session_lifecycle"],
    "title": "Land the slice; the live index is its own issue",
    "body": "S1's acceptance list has two independent halves.",
    "evidence": ["docs/cujs/S1.md", "ADR-0011"],
    "children": [{"title": "Live D1 seller index", "body": "The rest.", "edge": "depends_on", "after": "parent"}],
    "parent": "narrow",
}


def _proposed(resolution: dict | None = None, action_id: str = "A1") -> ProposedTechLeadAction:
    return ProposedTechLeadAction.from_mapping(
        {
            "id": action_id, "action_type": "resolve_block", "target_number": 262,
            "body": "The spec decides it.", "finding_ids": ["T1"],
            "resolution": resolution or RESOLUTION, "triage_class": "remedy",
        },
        index=0,
    )


def _config(mode: str | None = None) -> Config:
    config = Config()
    config.agents = {"agent:web": Mock()}
    config.tech_lead_follow_up_agent = "agent:web"
    if mode is not None:
        config.tech_lead.authority.resolve_block = mode
    return config


def _plan(config: Config, *proposed: ProposedTechLeadAction):
    decision = TechLeadDecision(
        summary="s",
        findings=(TechLeadFinding(id="T1", title="t", classification="task", evidence=("e",)),),
        proposed_actions=proposed,
    )
    return plan_tech_lead_decision_actions(
        decision,
        config,
        LabelManager(config),
        anchor_issue=Issue(number=99, title="Health", labels=[]),
        expected=build_expected_for_mutation(),
        op_ledger={},
        observed_session_generation=lambda _n: None,
        observed_validated_work_authority=lambda _n: None,
        pattern_ledger={},
        dedup_corpus=OpenIssueCorpus.disabled(),
        dedup_grant=DuplicateTargetGrant.none(),
        source_run_id="run-1",
        source_session_name="tech-lead-99",
        observed_at=OBSERVED,
    )


def test_the_charter_classifies_it_as_an_approvable_flow_fix() -> None:
    action_class = classify_charter_action("resolve_block")
    assert (action_class.role, action_class.depth, action_class.binding) == (
        CharterRole.FLOW, CharterDepth.FIX, CharterBinding.APPROVABLE,
    )
    # A triage of a blocked item that moves it: a remedy.
    assert "resolve_block" in TRIAGE_CLASS_ACTION_TYPES[TriageClass.REMEDY]


def test_the_default_ceiling_is_propose_and_execute_is_accepted() -> None:
    assert TechLeadAuthorityConfig().resolve_block == "propose"
    assert TechLeadAuthorityConfig.from_mapping({"resolve_block": "execute"}).resolve_block == "execute"
    with pytest.raises(ValueError, match="resolve_block"):
        TechLeadAuthorityConfig.from_mapping({"resolve_block": "always"})


@pytest.mark.parametrize(
    ("mode", "outcome"), [(None, CharterOutcome.PROPOSED), ("execute", CharterOutcome.EXECUTED)]
)
def test_the_operator_dial_alone_decides_execute_versus_propose(mode, outcome) -> None:
    policy = TechLeadCharterPolicy.from_config(_config(mode))
    assert policy.decide("resolve_block").outcome is outcome


def test_under_execute_the_owner_command_carries_the_decision() -> None:
    [planned] = [a for a in _plan(_config("execute"), _proposed()) if isinstance(a, ResolveBlockAction)]

    assert planned.issue_number == 262
    assert planned.resolution == BlockResolution.from_mapping(RESOLUTION, context="t")
    assert (planned.observed_at, planned.source_run_id) == (OBSERVED, "run-1")
    assert planned.source_session_name == "tech-lead-99"
    assert planned.decision_id == "run-1/A1"
    assert planned.proposal_issue_number == 0


def test_under_the_default_it_files_a_proposal_that_approval_runs_exactly() -> None:
    [filed] = [a for a in _plan(_config(), _proposed()) if isinstance(a, CreateTechLeadProposalIssueAction)]

    op = filed.op
    assert op.op_type == "resolve_block" and op.resolution is not None
    assert "resolve the needs-human block of issue #262" in filed.title
    assert "Live D1 seller index" in filed.body and "`Depends-on:`" in filed.body
    assert "agent_completion" in filed.body and "never clears" in filed.body
    # The stored op survives the authority store's round trip unchanged.
    stored = StoredTechLeadOp.from_dict(op.to_dict())
    assert stored == op

    [approved] = plan_approved_tech_lead_op_executions(
        [ApprovedTechLeadOp(proposal_issue_number=501, op=stored)]
    )
    assert isinstance(approved, ResolveBlockAction)
    assert approved.resolution == op.resolution
    assert approved.proposal_issue_number == 501
    assert approved.decision_id == "run-1/A1"


def test_a_different_resolution_for_the_same_item_is_a_different_proposal() -> None:
    first = _proposed()
    other = _proposed({**RESOLUTION, "title": "Do not split; finish it here"}, action_id="A2")
    def key(proposed: ProposedTechLeadAction) -> tuple[str, int | str]:
        return proposal_ledger_key("resolve_block", 262, resolution=proposed.resolution)

    assert key(first) != key(other)
    [filed] = [a for a in _plan(_config(), first) if isinstance(a, CreateTechLeadProposalIssueAction)]
    assert key(first) in build_op_ledger([(501, filed.op)])


def test_a_stored_op_without_its_resolution_is_rejected() -> None:
    [filed] = [a for a in _plan(_config(), _proposed()) if isinstance(a, CreateTechLeadProposalIssueAction)]
    raw = filed.op.to_dict()
    raw["resolution"] = None
    with pytest.raises(ValueError, match="BlockResolution"):
        StoredTechLeadOp.from_dict(raw)
