"""The persisted charter decision ledger and its read port (#7330, read by #7331)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_results import ActionResult
from issue_orchestrator.control.actions import ResetRetryIssueAction
from issue_orchestrator.control.tech_lead_charter_policy import (
    RecordTechLeadCharterDecisionsAction,
    TechLeadCharterPolicy,
    apply_record_tech_lead_charter_decisions,
)
from issue_orchestrator.control.tech_lead_proposal_execution import (
    finalize_tech_lead_op_execution,
)
from issue_orchestrator.control.tech_lead_proposals import (
    DiscardTerminalTechLeadProposalOpsAction,
    apply_discard_terminal_tech_lead_proposal_ops,
)
from issue_orchestrator.domain.tech_lead_charter import CharterOutcome, CharterRole
from issue_orchestrator.domain.tech_lead_charter_decisions import (
    CharterDecisionSource,
    CharterExecutionLink,
    CharterExecutionResult,
    CharterProposalLifecycle,
    TechLeadCharterDecision,
    decision_key,
)
from issue_orchestrator.domain.tech_lead_session import StoredTechLeadOp
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from issue_orchestrator.ports.tech_lead_charter_ledger import TechLeadCharterLedger


def _policy() -> TechLeadCharterPolicy:
    return TechLeadCharterPolicy.from_config(Config())


def _decision(
    action_id: str,
    kind: str = "kill_hung_session",
    *,
    run_id: str = "run-1",
    target: int | None = 13,
    anchor: int = 99,
    at: str = "2026-09-26T10:00:00+00:00",
    proposal_issue_number: int | None = None,
) -> TechLeadCharterDecision:
    verdict = _policy().decide(kind)
    return TechLeadCharterDecision.from_verdict(
        verdict,
        decision_id=decision_key(run_id, action_id),
        source=CharterDecisionSource.DECISION,
        run_id=run_id,
        action_id=action_id,
        anchor_issue_number=anchor,
        target_number=target,
        target_is_pr=False,
        decided_at=at,
        tracks_proposal=kind in {"kill_hung_session", "reset_retry"},
        proposal_issue_number=proposal_issue_number,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path: Path) -> Iterator[InMemoryTechLeadAuthorityStore | SqliteTechLeadAuthorityStore]:
    if request.param == "memory":
        yield InMemoryTechLeadAuthorityStore()
    else:
        yield SqliteTechLeadAuthorityStore(tmp_path / "tech_lead_authority.sqlite")


def _ledger(store) -> TechLeadCharterLedger:
    return store.charter_ledger


def test_read_port_by_issue_by_role_and_recent(store) -> None:
    ledger = _ledger(store)
    ledger.record_decisions(
        [
            _decision("A1", "kill_hung_session", target=13, at="2026-09-26T10:00:00+00:00"),
            _decision("A2", "create_issue", target=None, at="2026-09-26T11:00:00+00:00"),
            _decision("A3", "request_rework", target=40, anchor=7, at="2026-09-26T12:00:00+00:00"),
        ]
    )

    assert [r.action_id for r in ledger.list_for_issue(13)] == ["A1"]
    # The anchor issue finds every decision its run made, newest first.
    assert [r.action_id for r in ledger.list_for_issue(99)] == ["A2", "A1"]
    assert [r.action_id for r in ledger.list_for_role(CharterRole.REVIEW_LOOP)] == ["A3"]
    assert [r.action_id for r in ledger.list_for_role(CharterRole.FLOW)] == ["A2", "A1"]
    assert [r.action_id for r in ledger.list_recent(limit=2)] == ["A3", "A2"]
    assert ledger.list_for_role(CharterRole.GENERAL) == ()


def test_records_round_trip_exactly(store) -> None:
    decision = _decision("A1")
    _ledger(store).record_decisions([decision])

    assert _ledger(store).list_recent() == (decision,)


@pytest.mark.parametrize("limit", [0, 501])
def test_reads_are_bounded(store, limit: int) -> None:
    with pytest.raises(ValueError, match="read limit"):
        _ledger(store).list_recent(limit=limit)


def test_replay_keeps_the_original_date_of_an_unchanged_decision(store) -> None:
    ledger = _ledger(store)
    ledger.record_decisions([_decision("A1", at="2026-09-26T10:00:00+00:00")])
    ledger.record_decisions([_decision("A1", at="2026-09-27T10:00:00+00:00")])

    [row] = ledger.list_recent()
    assert row.decided_at == "2026-09-26T10:00:00+00:00"


def test_replay_with_a_changed_verdict_replaces_it_but_keeps_a_linked_outcome(store) -> None:
    ledger = _ledger(store)
    ledger.record_decisions([_decision("A1")])
    ledger.link_proposal_outcome(
        run_id="run-1",
        action_id="A1",
        proposal_issue_number=500,
        lifecycle=CharterProposalLifecycle.APPROVED_APPLIED,
        at="2026-09-26T12:00:00+00:00",
    )
    changed = replace(_decision("A1", at="2026-09-28T00:00:00+00:00"), reason="changed")

    ledger.record_decisions([changed])

    [row] = ledger.list_recent()
    assert row.reason == "changed"
    assert row.lifecycle is CharterProposalLifecycle.APPROVED_APPLIED
    assert row.proposal_issue_number == 500


def test_link_reaches_the_filing_decision_and_its_reproposals(store) -> None:
    ledger = _ledger(store)
    ledger.record_decisions(
        [
            _decision("A1", run_id="run-1"),
            # A later run re-proposed the same op onto the open proposal #500.
            _decision("A4", run_id="run-2", proposal_issue_number=500),
            _decision("A5", run_id="run-2"),
        ]
    )

    updated = ledger.link_proposal_outcome(
        run_id="run-1",
        action_id="A1",
        proposal_issue_number=500,
        lifecycle=CharterProposalLifecycle.DECLINED,
        at="2026-09-26T12:00:00+00:00",
    )

    assert updated == 2
    by_id = {row.action_id: row for row in ledger.list_recent()}
    assert by_id["A1"].lifecycle is CharterProposalLifecycle.DECLINED
    assert by_id["A1"].proposal_issue_number == 500
    assert by_id["A4"].lifecycle is CharterProposalLifecycle.DECLINED
    assert by_id["A5"].lifecycle is CharterProposalLifecycle.AWAITING_APPROVAL


def test_a_terminal_outcome_is_never_relinked(store) -> None:
    ledger = _ledger(store)
    ledger.record_decisions([_decision("A1")])
    kwargs = dict(run_id="run-1", action_id="A1", proposal_issue_number=500)
    ledger.link_proposal_outcome(
        **kwargs, lifecycle=CharterProposalLifecycle.APPROVED_APPLIED, at="t1"
    )

    assert ledger.link_proposal_outcome(
        **kwargs, lifecycle=CharterProposalLifecycle.DECLINED, at="t2"
    ) == 0
    [row] = ledger.list_recent()
    assert row.lifecycle is CharterProposalLifecycle.APPROVED_APPLIED


def test_the_record_action_persists_through_its_applier(store) -> None:
    action = RecordTechLeadCharterDecisionsAction(decisions=(_decision("A1"),))

    result = apply_record_tech_lead_charter_decisions(action, authority=store)

    assert result.success
    assert [row.action_id for row in store.charter_ledger.list_recent()] == ["A1"]


def test_the_record_action_fails_loudly_without_the_store() -> None:
    action = RecordTechLeadCharterDecisionsAction(decisions=(_decision("A1"),))

    result = apply_record_tech_lead_charter_decisions(action, authority=None)

    assert not result.success


def test_the_record_action_rejects_empty_and_duplicate_decisions() -> None:
    with pytest.raises(ValueError, match="needs decisions"):
        RecordTechLeadCharterDecisionsAction(decisions=())
    with pytest.raises(ValueError, match="duplicate"):
        RecordTechLeadCharterDecisionsAction(decisions=(_decision("A1"), _decision("A1")))


# -- the proposal lifecycle links back ---------------------------------------


def _op(action_id: str = "A1") -> StoredTechLeadOp:
    return StoredTechLeadOp(
        op_type="reset_retry",
        target_issue_number=13,
        rationale="r",
        source_run_id="run-1",
        source_session_name="issue-99",
        source_action_id=action_id,
        created_at="2026-09-26T10:00:00+00:00",
    )


@pytest.mark.parametrize(
    ("success", "details", "lifecycle"),
    [
        (True, {}, CharterProposalLifecycle.APPROVED_APPLIED),
        (False, {"mode": "stale_downgrade", "skip_reason": "gone"},
         CharterProposalLifecycle.APPROVED_STALE),
    ],
)
def test_finalizing_an_approved_op_links_its_decision(
    store, success: bool, details: dict, lifecycle: CharterProposalLifecycle
) -> None:
    store.charter_ledger.record_decisions([_decision("A1", "reset_retry")])
    store.record_op(issue_number=500, op=_op())
    action = ResetRetryIssueAction(
        issue_number=13, rationale="r", proposal_id="A1", anchor_issue_number=500,
        proposal_issue_number=500,
    )
    result = (
        ActionResult.ok(action)
        if success
        else ActionResult.fail(action, "stale", **details)
    )

    finalize_tech_lead_op_execution(
        result, action, repository_host=MagicMock(), ops=store
    )

    [row] = store.charter_ledger.list_recent()
    assert row.lifecycle is lifecycle
    assert row.proposal_issue_number == 500
    assert store.load_op(issue_number=500) is None


def test_a_proposal_closed_unapproved_links_its_decision_as_declined(store) -> None:
    store.charter_ledger.record_decisions([_decision("A1", "reset_retry")])
    store.record_op(issue_number=500, op=_op())
    tracker = MagicMock()
    tracker.get_issue_state.return_value = "closed"
    tracker.get_issue.return_value = None

    apply_discard_terminal_tech_lead_proposal_ops(
        DiscardTerminalTechLeadProposalOpsAction(candidate_issue_numbers=(500,)),
        tracker=tracker,
        authority=store,
    )

    [row] = store.charter_ledger.list_recent()
    assert row.lifecycle is CharterProposalLifecycle.DECLINED
    assert row.outcome is CharterOutcome.REFUSED_DESTRUCTIVE


# -- the tech-lead board shows the active charter and recorded outcomes -------


def test_board_shows_active_dials_and_counts_read_from_the_ledger(store) -> None:
    from issue_orchestrator.control.tech_lead_charter_board import charter_board_rows
    from issue_orchestrator.view_models.tech_lead_board import (
        build_tech_lead_board_view,
        render_tech_lead_board_md,
    )
    from datetime import datetime, timezone

    config = Config()
    config.tech_lead.charter.intake.enabled = False
    config.tech_lead.charter.review_loop.depth = "fix"
    config.tech_lead.charter.review_loop.authority = "propose"
    store.charter_ledger.record_decisions(
        [
            _decision("A1", "kill_hung_session"),
            _decision("A2", "reset_retry"),
            _decision("A3", "post_comment"),
            _decision("A4", "post_comment"),
        ]
    )
    # #7362: an executed verdict only allowed it; the applier's result decides.
    store.charter_ledger.link_execution_outcomes(
        [
            CharterExecutionLink(decision_key("run-1", "A3"), CharterExecutionResult.APPLIED),
            CharterExecutionLink(
                decision_key("run-1", "A4"), CharterExecutionResult.FAILED, "comment refused"
            ),
        ],
        at="2026-09-26T10:00:00+00:00",
    )

    rows = charter_board_rows(TechLeadCharterPolicy.from_config(config), store)
    markdown = render_tech_lead_board_md(
        build_tech_lead_board_view(
            ops=(), gated_proposals=(), case_files=(), area_counts=(),
            last_health_review_at=0, now=datetime(2026, 9, 26, tzinfo=timezone.utc),
            held_actions=(), charter=rows,
        )
    )

    assert "## Charter" in markdown
    # flow: A3 executed and applied, A4 executed but failed; A1 proposed
    # (default kill mode); A2 refused (destructive).
    assert "| flow | restructure / execute | 1 | 1 | 1 | 1 | 0 |" in markdown
    assert "| review_loop | fix / propose | 0 | 0 | 0 | 0 | 0 |" in markdown
    assert "| intake | disabled | 0 | 0 | 0 | 0 | 0 |" in markdown
    assert "| general | workaround / propose | 0 | 0 | 0 | 0 | 0 |" in markdown


def test_board_omits_the_charter_section_without_a_policy() -> None:
    from issue_orchestrator.control.tech_lead_charter_board import charter_board_rows

    assert charter_board_rows(None, InMemoryTechLeadAuthorityStore()) == ()


def test_an_applied_op_is_never_recorded_declined_after_a_failed_close(store) -> None:
    """The approval is linked before the proposal closes, so a close that fails
    and a later terminal cleanup cannot relabel the applied op "declined"."""
    store.charter_ledger.record_decisions([_decision("A1", "reset_retry")])
    store.record_op(issue_number=500, op=_op())
    action = ResetRetryIssueAction(
        issue_number=13, rationale="r", proposal_id="A1", anchor_issue_number=500,
        proposal_issue_number=500,
    )
    host = MagicMock()
    host.update_issue_state.side_effect = RuntimeError("GitHub 502")

    result = finalize_tech_lead_op_execution(
        ActionResult.ok(action), action, repository_host=host, ops=store
    )
    assert not result.success  # finalization failed; the op row is preserved
    tracker = MagicMock()
    tracker.get_issue_state.return_value = "closed"  # the operator closed it later
    apply_discard_terminal_tech_lead_proposal_ops(
        DiscardTerminalTechLeadProposalOpsAction(candidate_issue_numbers=(500,)),
        tracker=tracker,
        authority=store,
    )

    [row] = store.charter_ledger.list_recent()
    assert row.lifecycle is CharterProposalLifecycle.APPROVED_APPLIED


# -- round 3: coalesced proposals, audited promotion filings ------------------


def test_a_coalesced_sibling_follows_its_origin_proposal_lifecycle(store) -> None:
    """Two same-target gated kills in ONE decision file one proposal; approving
    it must resolve BOTH records, never leave the sibling awaiting forever."""
    from issue_orchestrator.control.tech_lead_charter_records import CharterDecisionLog

    log = CharterDecisionLog(run_id="run-1", anchor_issue_number=99, decided_at="t0")
    from issue_orchestrator.domain.tech_lead_artifacts import ProposedTechLeadAction

    for action_id in ("A1", "A2"):
        log.note(
            ProposedTechLeadAction(id=action_id, action_type="kill_hung_session",
                                   target_number=13, body="Hung."),
            _policy().decide("kill_hung_session"),
        )
    log.note_coalesced("A2", "A1")
    store.charter_ledger.record_decisions(log.records())

    updated = store.charter_ledger.link_proposal_outcome(
        run_id="run-1", action_id="A1", proposal_issue_number=500,
        lifecycle=CharterProposalLifecycle.APPROVED_APPLIED, at="t1",
    )

    assert updated == 2
    assert {row.lifecycle for row in store.charter_ledger.list_recent()} == {
        CharterProposalLifecycle.APPROVED_APPLIED
    }


def _promotion_facts():
    from issue_orchestrator.domain.tech_lead_findings import PatternEvidence, PromotableFinding

    evidence = PatternEvidence(
        signature="sig-a", case_file_issue_number=65, observation_count=3,
        fix_class="code", area="", diagnosis="Retry never backs off.",
    )
    return (PromotableFinding(evidence=evidence, target_repo="o/r"),)


def test_a_promotion_filing_never_runs_without_its_recorded_decision() -> None:
    from issue_orchestrator.control.actions import PromoteTechLeadFindingAction
    from issue_orchestrator.control.tech_lead_charter_policy import (
        CharterAuditedAction,
        apply_charter_audited_action,
    )
    from issue_orchestrator.control.tech_lead_charter_records import audit_promotions
    from issue_orchestrator.control.tech_lead_finding_promotion import plan_finding_promotions

    config = Config()
    config.repo = "o/r"
    config.agents = {"agent:web": MagicMock()}
    config.tech_lead_follow_up_agent = "agent:web"
    promotable = _promotion_facts()
    [audited] = audit_promotions(
        TechLeadCharterPolicy.from_config(config), promotable,
        plan_finding_promotions(config, promotable=promotable), decided_at="t0",
    )
    assert isinstance(audited, CharterAuditedAction)
    assert isinstance(audited.effect, PromoteTechLeadFindingAction)

    effects: list = []
    failing = MagicMock()
    failing.charter_ledger.record_decisions.side_effect = RuntimeError("disk full")
    with pytest.raises(RuntimeError):
        apply_charter_audited_action(audited, authority=failing, apply_action=effects.append)
    assert effects == []  # the external filing never ran
    missing = apply_charter_audited_action(audited, authority=None, apply_action=effects.append)
    assert not missing.success and effects == []

    store = InMemoryTechLeadAuthorityStore()
    apply_charter_audited_action(
        audited, authority=store,
        apply_action=lambda action: effects.append(action) or ActionResult.ok(action),
    )
    assert effects == [audited.effect]
    [row] = store.charter_ledger.list_recent()
    assert (row.action_id, row.outcome) == ("sig-a", CharterOutcome.PROPOSED)


def _executed_promotion():
    from issue_orchestrator.control.tech_lead_charter_records import audit_promotions
    from issue_orchestrator.control.tech_lead_finding_promotion import plan_finding_promotions

    config = Config()
    config.repo = "o/r"
    config.agents = {"agent:web": MagicMock()}
    config.tech_lead_follow_up_agent = "agent:web"
    config.tech_lead.findings.promote = "auto"
    promotable = _promotion_facts()
    [audited] = audit_promotions(
        TechLeadCharterPolicy.from_config(config), promotable,
        plan_finding_promotions(config, promotable=promotable), decided_at="t0",
    )
    assert audited.decisions[0].outcome is CharterOutcome.EXECUTED
    return audited


@pytest.mark.parametrize(
    ("apply_effect", "expected", "reason"),
    [
        (lambda action: ActionResult.ok(action), CharterExecutionResult.APPLIED, None),
        (lambda action: ActionResult.fail(action, "403 from o/r"), CharterExecutionResult.FAILED,
         "403 from o/r"),
        (lambda action: ActionResult.skip(action, "stale precondition: already filed"),
         CharterExecutionResult.REFUSED, "stale precondition: already filed"),
    ],
    ids=["applied", "failed", "refused"],
)
def test_an_executed_promotion_links_its_filing_s_real_result(apply_effect, expected, reason) -> None:
    """#7362: an ungated promotion is recorded ``executed`` before it files;
    what the filing then did is linked back, so a failed filing never reads
    as a remedy that took effect."""
    from issue_orchestrator.control.tech_lead_charter_policy import apply_charter_audited_action

    audited = _executed_promotion()
    store = InMemoryTechLeadAuthorityStore()

    apply_charter_audited_action(audited, authority=store, apply_action=apply_effect)

    [row] = store.charter_ledger.list_recent()
    assert row.outcome is CharterOutcome.EXECUTED
    assert (row.execution, row.execution_reason) == (expected, reason)
    assert row.took_effect is (expected is CharterExecutionResult.APPLIED)


def test_an_executed_promotion_that_raises_is_linked_failed_and_still_raises() -> None:
    from issue_orchestrator.control.tech_lead_charter_policy import apply_charter_audited_action

    audited = _executed_promotion()
    store = InMemoryTechLeadAuthorityStore()

    def boom(action):
        raise RuntimeError("host exploded")

    with pytest.raises(RuntimeError, match="host exploded"):
        apply_charter_audited_action(audited, authority=store, apply_action=boom)

    [row] = store.charter_ledger.list_recent()
    assert row.execution is CharterExecutionResult.FAILED
    assert row.execution_reason == "RuntimeError: host exploded"
    assert not row.took_effect


def test_an_executed_decision_links_what_its_applier_did(store) -> None:
    """#7362, the port: the verdict is kept as decided, the applier's latest
    result is linked beside it, a replay keeps it, and nothing but an executed
    decision can be linked."""
    ledger = _ledger(store)
    executed = _decision("A1", "post_comment", target=40, at="2026-09-26T10:00:00+00:00")
    proposed = _decision("A2", "kill_hung_session", target=40)
    assert executed.outcome is CharterOutcome.EXECUTED
    ledger.record_decisions([executed, proposed])

    refused = CharterExecutionLink(
        executed.decision_id, CharterExecutionResult.REFUSED, "stale precondition: gone"
    )
    assert ledger.link_execution_outcomes([refused], at="2026-09-26T10:01:00+00:00") == 1
    [row] = [r for r in ledger.list_for_issue(40) if r.action_id == "A1"]
    assert (row.outcome, row.execution) == (CharterOutcome.EXECUTED, CharterExecutionResult.REFUSED)
    assert row.execution_reason == "stale precondition: gone" and not row.took_effect

    applied = CharterExecutionLink(executed.decision_id, CharterExecutionResult.APPLIED)
    ledger.link_execution_outcomes([applied], at="2026-09-26T10:05:00+00:00")
    [row] = [r for r in ledger.list_for_issue(40) if r.action_id == "A1"]
    assert row.took_effect and row.execution_reason is None
    assert row.effect_at == "2026-09-26T10:05:00+00:00"
    # A replayed plan re-records the same verdict right before its effects run
    # again: the decision keeps its date, but the earlier attempt's "applied"
    # is not carried onto the new attempt, whose own link may never land.
    ledger.record_decisions([replace(executed, decided_at="2026-09-26T11:00:00+00:00")])
    [row] = [r for r in ledger.list_for_issue(40) if r.action_id == "A1"]
    assert row.execution is None and not row.took_effect
    assert row.decided_at == "2026-09-26T10:00:00+00:00"
    assert ledger.list_remedies_on_issue(40) == ()

    with pytest.raises(ValueError, match="not executed"):
        ledger.link_execution_outcomes(
            [CharterExecutionLink(proposed.decision_id, CharterExecutionResult.APPLIED)], at="t"
        )
    unknown = CharterExecutionLink(decision_key("run-9", "A9"), CharterExecutionResult.APPLIED)
    assert ledger.link_execution_outcomes([unknown], at="t") == 0

    # A verdict that no longer executes carries no execution result.
    advice = replace(executed, outcome=CharterOutcome.ADVICE_ONLY)
    ledger.record_decisions([advice])
    [row] = [r for r in ledger.list_for_issue(40) if r.action_id == "A1"]
    assert row.execution is None and not row.took_effect


def test_an_approval_never_vouches_for_a_replay_that_executed_directly(store) -> None:
    """#7362 review r2: a decision approved and applied, replayed under a
    charter that now executes it directly, then refused at apply time, did not
    take effect - the old approval is not carried onto the new verdict."""
    ledger = _ledger(store)
    gated = _decision("A1", "kill_hung_session", target=40, at="2026-09-26T08:00:00+00:00")
    ledger.record_decisions([gated])
    ledger.link_proposal_outcome(
        run_id="run-1", action_id="A1", proposal_issue_number=800,
        lifecycle=CharterProposalLifecycle.APPROVED_APPLIED, at="2026-09-26T09:00:00+00:00",
    )
    ledger.record_decisions([replace(gated, outcome=CharterOutcome.EXECUTED, lifecycle=None,
                                     lifecycle_updated_at=None)])
    ledger.link_execution_outcomes(
        [CharterExecutionLink(gated.decision_id, CharterExecutionResult.REFUSED, "stale")],
        at="2026-09-26T10:00:00+00:00",
    )

    [row] = ledger.list_recent()
    assert row.lifecycle is None and row.execution is CharterExecutionResult.REFUSED
    assert not row.took_effect and row.effect_at == "2026-09-26T10:00:00+00:00"
    assert ledger.list_remedies_on_issue(40) == ()
    with pytest.raises(ValueError, match="gated decision"):
        replace(row, lifecycle=CharterProposalLifecycle.APPROVED_APPLIED)


def test_an_advice_only_promotion_files_nothing_but_is_recorded() -> None:
    from issue_orchestrator.control.tech_lead_charter_records import audit_promotions
    from issue_orchestrator.control.tech_lead_finding_promotion import plan_finding_promotions

    config = Config()
    config.repo = "o/r"
    config.tech_lead.charter.learning.depth = "workaround"
    promotable = _promotion_facts()
    planned = plan_finding_promotions(config, promotable=promotable)

    [record] = audit_promotions(
        TechLeadCharterPolicy.from_config(config), promotable, planned, decided_at="t0"
    )

    assert planned == []
    assert isinstance(record, RecordTechLeadCharterDecisionsAction)
    [decision] = record.decisions
    assert decision.outcome is CharterOutcome.ADVICE_ONLY


def test_an_advice_only_promotion_lane_needs_no_filing_dependency_and_still_records() -> None:
    """learning kept at workaround: nothing can be filed, so the self route's
    follow-up worker agent is not required, and the lane still runs (through
    the real readiness + fact gathering) to record why nothing was filed
    (review r4 F1)."""
    from dataclasses import replace as dc_replace

    from issue_orchestrator.control.tech_lead_finding_promotion import (
        gather_finding_promotion_facts,
        plan_finding_promotion_actions,
    )
    from issue_orchestrator.control.tech_lead_promotion_read_budget import PromotionReadBudget
    from issue_orchestrator.domain.models import TechLeadFacts
    from issue_orchestrator.infra.tech_lead_promotion_activation import promotion_lane_readiness

    config = Config()
    config.repo = "o/r"
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_follow_up_agent = None
    config.tech_lead.charter.learning.depth = "workaround"
    store = InMemoryTechLeadAuthorityStore()
    store.record_pattern(
        signature="sig-a", issue_number=65, observation_id="sig-a:obs-1",
        fix_class="code", area="", diagnosis="Retry never backs off.",
    )
    for index in (2, 3):
        store.note_pattern_observation(signature="sig-a", observation_id=f"sig-a:obs-{index}")

    # (Startup validation separately requires the follow-up agent whenever a
    # tech-lead agent is configured, #6779 R14; the LANE's readiness must not.)
    assert promotion_lane_readiness(config).problems == ()
    promotable, _, _ = gather_finding_promotion_facts(
        config, authority=store, target=None, read_budget=PromotionReadBudget()
    )
    [record] = plan_finding_promotion_actions(
        config, dc_replace(TechLeadFacts(), promotable_findings=promotable)
    )

    assert isinstance(record, RecordTechLeadCharterDecisionsAction)
    [decision] = record.decisions
    assert (decision.action_id, decision.outcome) == ("sig-a", CharterOutcome.ADVICE_ONLY)
    # Once learning may fix, filing is possible again and the dependency returns.
    config.tech_lead.charter.learning.depth = "fix"
    assert promotion_lane_readiness(config).problems


def test_an_advice_only_lane_records_every_candidate_not_just_the_capped_one() -> None:
    """The in-flight cap bounds FILED work; with nothing filed, every eligible
    signature is selected and recorded, every tick (review r5 F1)."""
    from issue_orchestrator.control.tech_lead_finding_promotion import (
        plan_finding_promotion_actions,
        select_promotable_findings,
    )
    from dataclasses import replace as dc_replace
    from issue_orchestrator.domain.models import TechLeadFacts

    config = Config()
    config.repo = "o/r"
    config.tech_lead.findings.max_open_promoted = 1
    store = InMemoryTechLeadAuthorityStore()
    for number, signature in ((65, "sig-a"), (66, "sig-b")):
        store.record_pattern(
            signature=signature, issue_number=number, observation_id=f"{signature}:1",
            fix_class="code", area="", diagnosis="d",
        )
        store.note_pattern_observation(signature=signature, observation_id=f"{signature}:2")

    def tick() -> set[str]:
        promotable = select_promotable_findings(
            config, evidence=store.list_pattern_evidence(), promotions=store.list_promotions()
        )
        actions = plan_finding_promotion_actions(
            config, dc_replace(TechLeadFacts(), promotable_findings=promotable)
        )
        assert all(isinstance(a, RecordTechLeadCharterDecisionsAction) for a in actions)
        for action in actions:
            store.charter_ledger.record_decisions(action.decisions)
        return {row.action_id for row in store.charter_ledger.list_recent()}

    # Filing possible: the cap holds to ONE candidate per target.
    assert len(select_promotable_findings(
        config, evidence=store.list_pattern_evidence(), promotions=store.list_promotions()
    )) == 1
    config.tech_lead.charter.learning.depth = "workaround"
    assert tick() == {"sig-a", "sig-b"}
    assert tick() == {"sig-a", "sig-b"}
    assert all(
        row.outcome is CharterOutcome.ADVICE_ONLY for row in store.charter_ledger.list_recent()
    )


def test_doctor_proves_no_filing_capability_for_an_advice_only_lane() -> None:
    """Nothing is filed, so no filing probe runs; once learning may fix, the
    foreign route's filing capability is probed again (review r5 F2)."""
    from issue_orchestrator.infra.config_models_tech_lead import PromotionRouteTarget
    from issue_orchestrator.infra.doctor.checks.tech_lead import check_tech_lead_finding_routes

    config = Config()
    config.repo = "o/r"
    config.agents = {"agent:web": MagicMock()}
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_follow_up_agent = "agent:web"
    config.tech_lead.findings.route = {
        "default": PromotionRouteTarget(repo="other/repo", agent_label="agent:web"),
    }
    config.tech_lead.charter.learning.depth = "workaround"
    host = MagicMock()
    host.check_filing_ready.return_value = "token cannot create issues in other/repo"

    [check] = check_tech_lead_finding_routes(config, target_host=host)

    assert check.status == "ok" and "advice only" in check.detail
    host.check_filing_ready.assert_not_called()

    config.tech_lead.charter.learning.depth = "fix"
    [check] = check_tech_lead_finding_routes(config, target_host=host)
    assert check.status == "error" and "cannot create issues" in check.detail
    host.check_filing_ready.assert_called()


def test_list_about_issue_filters_before_its_limit(store) -> None:
    """#7331: decisions a run anchored on #99 took about OTHER issues never
    crowd #99's own decisions out of a bounded read."""
    own = _decision("A0", target=99, anchor=99, at="2026-09-26T09:00:00+00:00")
    untargeted = _decision(
        "A1", "create_issue", target=None, anchor=99, at="2026-09-26T09:30:00+00:00"
    )
    others = [
        _decision(f"B{i}", target=500 + i, anchor=99, at=f"2026-09-26T1{i}:00:00+00:00")
        for i in range(5)
    ]
    elsewhere = _decision("C0", target=None, anchor=7)
    _ledger(store).record_decisions([own, untargeted, *others, elsewhere])

    about = _ledger(store).list_about_issue(99, limit=2)

    assert [d.decision_id for d in about] == [untargeted.decision_id, own.decision_id]
    assert all(d.is_about_issue(99) for d in about)
    assert not others[0].is_about_issue(99)


def test_list_about_issue_is_an_index_search_not_a_ledger_scan(tmp_path: Path) -> None:
    """#7331: the board runs this once per blocked card."""
    from issue_orchestrator.infra.tech_lead_charter_ledger_store import ABOUT_ISSUE_QUERY

    import sqlite3

    path = tmp_path / "tech_lead_authority.sqlite"
    store = SqliteTechLeadAuthorityStore(path)
    store.charter_ledger.list_about_issue(1, limit=5)  # the store has built its schema
    with sqlite3.connect(path) as connection:
        plan = [
            str(row[3])
            for row in connection.execute(f"EXPLAIN QUERY PLAN {ABOUT_ISSUE_QUERY}", (1, 1, 5))
        ]

    assert any("USING INDEX tech_lead_charter_decisions_target" in step for step in plan), plan
    assert any("USING INDEX tech_lead_charter_decisions_anchor" in step for step in plan), plan
    assert not any(step.startswith("SCAN tech_lead_charter_decisions") for step in plan), plan


def test_effects_on_an_issue_come_newest_effect_first(store) -> None:
    """#7331: an approval applied after later history is still the newest effect."""
    approved = _decision(
        "A1", "kill_hung_session", target=40, at="2026-09-26T08:00:00+00:00"
    ).with_lifecycle(
        CharterProposalLifecycle.APPROVED_APPLIED,
        at="2026-09-26T12:00:00+00:00",
        proposal_issue_number=800,
    )
    executed = replace(
        _decision("A2", "recover_validated_work", target=40, at="2026-09-26T10:00:00+00:00"),
        outcome=CharterOutcome.EXECUTED,
        lifecycle=None,
    )
    comments = [
        _decision(f"C{i}", "post_comment", target=40, at=f"2026-09-26T11:0{i}:00+00:00")
        for i in range(5)
    ]
    proposed = _decision("A3", "kill_hung_session", target=40, at="2026-09-26T11:30:00+00:00")
    elsewhere = _decision("A4", "recover_validated_work", target=41)
    # #7362: executed, but its applier refused it; and executed with no result yet.
    refused, pending = (
        replace(
            _decision(action_id, "recover_validated_work", target=40, at=at),
            outcome=CharterOutcome.EXECUTED,
            lifecycle=None,
        )
        for action_id, at in (("A5", "2026-09-26T11:45:00+00:00"), ("A6", "2026-09-26T11:50:00+00:00"))
    )
    _ledger(store).record_decisions(
        [approved, executed, *comments, proposed, elsewhere, refused, pending]
    )
    _ledger(store).link_execution_outcomes(
        [
            CharterExecutionLink(executed.decision_id, CharterExecutionResult.APPLIED),
            CharterExecutionLink(
                refused.decision_id, CharterExecutionResult.REFUSED, "stale precondition: claimed"
            ),
        ],
        at="2026-09-26T10:00:00+00:00",
    )

    effects = _ledger(store).list_remedies_on_issue(40, limit=5)

    assert [d.decision_id for d in effects][:2] == [approved.decision_id, executed.decision_id]
    assert [d.decision_id for d in effects] == [approved.decision_id, executed.decision_id]
    assert all(d.is_remedy and d.took_effect and d.target_number == 40 for d in effects)


def test_decisions_filed_as_a_proposal_are_read_by_its_number(store) -> None:
    filed = _decision("A1", target=13, at="2026-09-26T08:00:00+00:00", proposal_issue_number=900)
    noise = [
        _decision(f"C{i}", "post_comment", target=900, anchor=900, at=f"2026-09-26T1{i}:00:00+00:00")
        for i in range(5)
    ]
    _ledger(store).record_decisions([filed, *noise])

    assert _ledger(store).list_filed_as_proposal(900, limit=1) == (filed,)
