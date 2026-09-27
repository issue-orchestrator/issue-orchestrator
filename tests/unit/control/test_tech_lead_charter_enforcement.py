"""Charter enforcement through the REAL decision path (#7330).

Every test here drives :func:`plan_tech_lead_decision_actions` — the seam where
the orchestrator turns an untrusted decision artifact into actions — and checks
both the effect it planned and the charter decision it recorded.
"""

from __future__ import annotations

import itertools
import json
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.actions import (
    AddCommentAction,
    CreateTechLeadIssueAction,
    CreateTechLeadProposalIssueAction,
    KillHungSessionAction,
    ResetRetryIssueAction,
    SurfaceTechLeadProposalAction,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.proposal_dedup_gate import (
    DuplicateTargetGrant,
    OpenIssueCorpus,
)
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.tech_lead_charter_records import CharterDecisionLog
from issue_orchestrator.control.tech_lead_decision_actions import (
    plan_tech_lead_decision_actions,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.tech_lead_artifacts import (
    ProposedTechLeadAction,
    TechLeadDecision,
    TechLeadFinding,
)
from issue_orchestrator.domain.tech_lead_charter import (
    CharterOutcome,
    CharterReason,
    CharterRole,
)
from issue_orchestrator.domain.tech_lead_charter_decisions import (
    CharterDecisionSource,
    CharterProposalLifecycle,
)
from issue_orchestrator.domain.tech_lead_session import (
    PROPOSED_TECH_LEAD_LABEL,
    TechLeadSessionGeneration,
)
from issue_orchestrator.infra.config import Config

EXPECTED = build_expected_for_mutation()
ANCHOR = 99


def _config(**charter: dict[str, object]) -> Config:
    config = Config()
    config.agents = {"agent:web": Mock()}
    config.tech_lead_follow_up_agent = "agent:web"
    for role, dials in charter.items():
        role_config = getattr(config.tech_lead.charter, role)
        for key, value in dials.items():
            setattr(role_config, key, value)
    return config


def _finding() -> TechLeadFinding:
    return TechLeadFinding(
        id="T1", title="Finding", classification="infra", evidence=("log 1-2",)
    )


def _plan(
    config: Config, *actions: ProposedTechLeadAction
) -> tuple[list, CharterDecisionLog]:
    log = CharterDecisionLog(
        run_id="run-1", anchor_issue_number=ANCHOR, decided_at="2026-09-26T10:00:00+00:00"
    )
    planned = plan_tech_lead_decision_actions(
        TechLeadDecision(
            summary="s", findings=(_finding(),), proposed_actions=tuple(actions)
        ),
        config,
        LabelManager(config),
        anchor_issue=Issue(
            number=ANCHOR, title="Anchor", labels=["agent:tech-lead"], repo="o/r"
        ),
        expected=EXPECTED,
        op_ledger={},
        pattern_ledger={},
        source_run_id="run-1",
        source_session_name="issue-99",
        observed_at="2026-09-26T09:00:00+00:00",
        observed_session_generation=lambda number: TechLeadSessionGeneration(
            issue_number=number,
            task_kind=TaskKind.CODE,
            terminal_id="term-1",
            run_id="run-7",
        ),
        dedup_corpus=OpenIssueCorpus.disabled(),
        dedup_grant=DuplicateTargetGrant.none(),
        charter_log=log,
    )
    return planned, log


def _create_issue() -> ProposedTechLeadAction:
    return ProposedTechLeadAction(
        id="A1",
        action_type="create_issue",
        title="Fix the flaky retry",
        body="The retry loop never backs off (T1).",
        finding_ids=("T1",),
    )


def _kill() -> ProposedTechLeadAction:
    return ProposedTechLeadAction(
        id="A2", action_type="kill_hung_session", target_number=13, body="Hung."
    )


def _reset() -> ProposedTechLeadAction:
    return ProposedTechLeadAction(
        id="A3", action_type="reset_retry", target_number=13, body="Unrecoverable."
    )


@pytest.mark.parametrize(
    ("depth", "authority"),
    list(itertools.product(("workaround", "fix", "restructure"), ("propose", "execute"))),
)
def test_create_issue_across_the_flow_depth_authority_grid(
    depth: str, authority: str
) -> None:
    config = _config(flow={"depth": depth, "authority": authority})

    planned, log = _plan(config, _create_issue())
    [record] = log.records()

    created = [a for a in planned if isinstance(a, CreateTechLeadIssueAction)]
    if depth == "workaround":
        # Beyond depth: recorded as advice, nothing filed, digest explains why.
        assert created == []
        assert [a.proposal_type for a in planned if isinstance(a, SurfaceTechLeadProposalAction)] == ["create_issue"]
        [digest] = [a for a in planned if isinstance(a, AddCommentAction)]
        assert "Advice only (outside the tech-lead charter)" in digest.comment
        assert "deeper than flow's allowed depth (workaround)" in digest.comment
        assert record.outcome is CharterOutcome.ADVICE_ONLY
        assert record.reason_code is CharterReason.BEYOND_DEPTH
        return
    [issue] = created
    if authority == "propose":
        assert PROPOSED_TECH_LEAD_LABEL in issue.labels
        assert record.outcome is CharterOutcome.PROPOSED
        assert record.reason_code is CharterReason.ROLE_AUTHORITY_PROPOSE
    else:
        assert PROPOSED_TECH_LEAD_LABEL not in issue.labels
        assert record.outcome is CharterOutcome.EXECUTED


@pytest.mark.parametrize(
    ("dials", "ceiling", "expected"),
    [
        ({"authority": "execute"}, "execute", KillHungSessionAction),
        ({"authority": "propose"}, "execute", CreateTechLeadProposalIssueAction),
        ({"authority": "execute"}, "propose", CreateTechLeadProposalIssueAction),
        ({"enabled": False}, "execute", SurfaceTechLeadProposalAction),
    ],
)
def test_act_level_kill_follows_the_flow_role(
    dials: dict[str, object], ceiling: str, expected: type
) -> None:
    config = _config(flow=dials)
    config.tech_lead.authority.kill_hung_session = ceiling

    planned, log = _plan(config, _kill())

    assert isinstance(planned[0], expected)
    [record] = log.records()
    assert record.role is CharterRole.FLOW
    assert record.action_ceiling.value == ceiling
    if expected is CreateTechLeadProposalIssueAction:
        assert record.lifecycle is CharterProposalLifecycle.AWAITING_APPROVAL
    else:
        assert record.lifecycle is None


@pytest.mark.parametrize(
    "flow",
    [
        {"authority": "execute"},
        {"authority": "propose"},
        {"depth": "restructure", "authority": "execute"},
    ],
)
def test_reset_from_scratch_never_executes_unattended(flow: dict[str, object]) -> None:
    config = _config(flow=flow)
    # Config loading rejects this; set it directly to prove the charter holds
    # even when a mode says execute.
    config.tech_lead.authority.reset_retry = "execute"

    planned, log = _plan(config, _reset())

    assert not any(isinstance(a, ResetRetryIssueAction) for a in planned)
    [proposal] = [a for a in planned if isinstance(a, CreateTechLeadProposalIssueAction)]
    assert proposal.op.op_type == "reset_retry"
    [record] = log.records()
    assert record.outcome is CharterOutcome.REFUSED_DESTRUCTIVE
    assert record.reason.startswith("Always needs approval")


def test_an_agent_claimed_role_cannot_route_through_a_more_permissive_role() -> None:
    """Role and depth come from the action TYPE (#7329 comment, point 5).

    The decision claims ``flow``/``workaround`` for a ``request_rework``-shaped
    fix and ``general``/``execute``; flow is disabled, so a claim-honouring
    orchestrator would file the issue. The orchestrator classifies by type.
    """
    decision = TechLeadDecision.from_agent_payload(
        {
            "schema_version": 1,
            "summary": "s",
            "findings": [
                {"id": "T1", "title": "F", "classification": "infra", "evidence": ["e"]}
            ],
            "proposed_actions": [
                {
                    "id": "A1",
                    "action_type": "create_issue",
                    "title": "Restructure the parsers",
                    "body": "Four parsers disagree (T1).",
                    "finding_ids": ["T1"],
                    "role": "general",
                    "depth": "workaround",
                    "authority": "execute",
                }
            ],
        }
    )
    config = _config(flow={"enabled": False}, general={"authority": "execute"})

    planned, log = _plan(config, *decision.proposed_actions)

    assert not any(isinstance(a, CreateTechLeadIssueAction) for a in planned)
    [record] = log.records()
    assert record.role is CharterRole.FLOW
    assert record.outcome is CharterOutcome.ADVICE_ONLY
    assert record.reason_code is CharterReason.ROLE_DISABLED


def test_advice_and_escalation_are_unrestricted_when_every_role_is_disabled() -> None:
    config = _config(
        **{
            role.value: {"enabled": False}
            for role in CharterRole
        }
    )
    comment = ProposedTechLeadAction(
        id="A1", action_type="post_comment", target_number=5, body="Diagnosis (T1).",
        finding_ids=("T1",),
    )
    escalate = ProposedTechLeadAction(
        id="A2", action_type="escalate_to_human", target_number=5, body="Needs you."
    )

    planned, log = _plan(config, comment, escalate)

    assert any(isinstance(a, AddCommentAction) and a.number == 5 for a in planned)
    assert all(record.outcome is CharterOutcome.EXECUTED for record in log.records())


def test_default_config_plans_exactly_what_it_planned_before() -> None:
    """With the default charter, the planned effects equal the per-action-mode
    plan, action for action (existing tests pin the modes themselves)."""
    default_plan, _ = _plan(_config(), _create_issue(), _kill(), _reset())
    explicit_plan, _ = _plan(
        _config(**{role.value: {} for role in CharterRole}),
        _create_issue(),
        _kill(),
        _reset(),
    )

    for plan in (default_plan, explicit_plan):
        assert [type(a) for a in plan] == [
            CreateTechLeadIssueAction,
            CreateTechLeadProposalIssueAction,
            CreateTechLeadProposalIssueAction,
        ]
        issue, kill, reset = plan
        assert PROPOSED_TECH_LEAD_LABEL not in issue.labels
        assert (kill.op.op_type, reset.op.op_type) == ("kill_hung_session", "reset_retry")


def test_records_carry_everything_a_reader_needs() -> None:
    config = _config(flow={"depth": "fix", "authority": "propose"})

    _, log = _plan(config, _kill())
    [record] = log.records()

    assert record.decision_id == "decision:run-1:A2"
    assert record.source is CharterDecisionSource.DECISION
    assert (record.run_id, record.action_id, record.anchor_issue_number) == (
        "run-1",
        "A2",
        ANCHOR,
    )
    assert (record.target_number, record.target_is_pr) == (13, False)
    assert record.action_kind == "kill_hung_session"
    assert record.required_depth.value == "workaround"
    assert (record.role_enabled, record.role_depth.value, record.role_authority.value) == (
        True,
        "fix",
        "propose",
    )
    assert record.decided_at == "2026-09-26T10:00:00+00:00"
    assert record.reason == (
        "Waiting on you: flow may apply workarounds but must propose (authority: propose)."
    )
    # The record round-trips through its persisted form exactly.
    from issue_orchestrator.domain.tech_lead_charter_decisions import (
        TechLeadCharterDecision,
    )

    assert TechLeadCharterDecision.from_dict(json.loads(json.dumps(record.to_dict()))) == record


def test_a_rejected_decision_records_nothing() -> None:
    """A decision the planner rejects wholesale applies nothing, so it must not
    leave charter records claiming actions were decided."""
    flag = ProposedTechLeadAction(
        id="A1", action_type="flag_pattern", body="b", pattern_signature="sig",
        fix_class="code", finding_ids=("T1",),
    )
    conflicting = ProposedTechLeadAction(
        id="A2", action_type="flag_pattern", body="b", pattern_signature="sig",
        fix_class="human", finding_ids=("T1",),
    )

    planned, log = _plan(_config(), flag, conflicting)

    assert len(planned) == 1
    assert log.records() == ()
