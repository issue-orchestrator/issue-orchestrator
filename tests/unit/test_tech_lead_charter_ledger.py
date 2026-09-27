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
        ]
    )

    rows = charter_board_rows(TechLeadCharterPolicy.from_config(config), store)
    markdown = render_tech_lead_board_md(
        build_tech_lead_board_view(
            ops=(), gated_proposals=(), case_files=(), area_counts=(),
            last_health_review_at=0, now=datetime(2026, 9, 26, tzinfo=timezone.utc),
            charter=rows,
        )
    )

    assert "## Charter" in markdown
    # flow: A3 executed; A1 proposed (default kill mode); A2 refused (destructive).
    assert "| flow | restructure / execute | 1 | 1 | 1 | 0 |" in markdown
    assert "| review_loop | fix / propose | 0 | 0 | 0 | 0 |" in markdown
    assert "| intake | disabled | 0 | 0 | 0 | 0 |" in markdown
    assert "| general | workaround / propose | 0 | 0 | 0 | 0 |" in markdown


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
