"""The exam observer reads the engine's charter ledger (#7593, #7658).

These run the production observer (``tests/e2e/exam/observe.py``) over a real
authority store, so a reverted observer fails them.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from issue_orchestrator.domain.tech_lead_artifacts import TriageClass
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
    CharterProposalLifecycle,
    TechLeadCharterDecision,
)
from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
from issue_orchestrator.testing.exam import grade
from issue_orchestrator.testing.exam.cases import (
    SPLIT,
    needs_human_block_resolutions_proposed,
)
from tests.e2e.exam.observe import observe_decisions, observe_triage
from tests.unit.testing.exam import test_grading as grading

ITEM = 920
PROPOSAL = 951


def _record_resolve_proposal(state_dir: Path) -> None:
    store = SqliteTechLeadAuthorityStore(state_dir / "tech_lead_authority.sqlite")
    store.charter_ledger.record_decisions([
        TechLeadCharterDecision(
            decision_id="decision:run:A1", source=CharterDecisionSource.DECISION,
            run_id="run", action_id="A1", anchor_issue_number=900, target_number=ITEM,
            target_is_pr=False, action_kind="resolve_block", role=CharterRole.FLOW,
            required_depth=CharterDepth.FIX, binding=CharterBinding.APPROVABLE,
            role_enabled=True, role_depth=CharterDepth.RESTRUCTURE,
            role_authority=CharterAuthority.EXECUTE, action_ceiling=CharterAuthority.PROPOSE,
            ceiling_source="c", outcome=CharterOutcome.PROPOSED,
            reason_code=CharterReason.ACTION_AUTHORITY_PROPOSE, reason="r",
            decided_at="2026-10-04T12:00:00+00:00", execution=None,
            lifecycle=CharterProposalLifecycle("awaiting_approval"),
            proposal_issue_number=PROPOSAL, triage_class=TriageClass.REMEDY,
            triage_fingerprint="needs-human",
        )
    ])


def _grade_case_g(state_dir: Path, *, proposal_open: bool) -> set[str]:
    def is_open(number: int) -> bool:
        assert number == PROPOSAL
        return proposal_open

    decisions = observe_decisions(state_dir, ITEM, proposal_open=is_open)
    triage = observe_triage(state_dir, ITEM, proposal_open=is_open)
    cls = grading.TestCasesFAndGResolution
    items = list(cls()._items(execute=False, resolve=cls.PROPOSED,
                              provisioning=cls.HANDED_OVER, blocked=True))
    items[0] = replace(items[0], role=SPLIT, decisions=decisions, triage=triage)
    case = needs_human_block_resolutions_proposed(needs_human_label="needs-human")
    obs = replace(grading.observation(case.case_id, items[0]), items=tuple(items),
                  owned_numbers=frozenset(range(900, 960)))
    return {goal.name for goal in grade(case, obs).goals if not goal.passed}


def test_an_open_resolve_proposal_is_awaiting_the_operator(tmp_path: Path) -> None:
    _record_resolve_proposal(tmp_path)

    assert _grade_case_g(tmp_path, proposal_open=True) == set()


def test_a_closed_resolve_proposal_is_no_proposal(tmp_path: Path) -> None:
    """r18 F1: the ledger may still say awaiting_approval after the proposal
    was closed (the engine has not reconciled it yet). The observer reads the
    proposal's live state, so case G does not pass on a proposal nobody can
    approve."""
    _record_resolve_proposal(tmp_path)

    assert "split.resolved_awaiting_approval" in _grade_case_g(tmp_path, proposal_open=False)
    [decision] = observe_decisions(tmp_path, ITEM, proposal_open=lambda number: False)
    assert decision.proposal_issue_number is None
    triage = observe_triage(tmp_path, ITEM, proposal_open=lambda number: False)
    assert triage is not None and triage.proposal_issue_number is None
