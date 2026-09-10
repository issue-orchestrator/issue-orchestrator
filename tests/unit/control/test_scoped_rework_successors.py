"""A retained instruction needs exact ledger evidence across all consumers."""

from dataclasses import replace
import hashlib

import pytest

from issue_orchestrator.control.in_flight_work import (
    InFlightWorkLedger,
    SettlementOutcome,
)
from issue_orchestrator.control.launch_transaction import PendingWorkLaunchClaim
from issue_orchestrator.control.pending_work_successors import PendingWorkSuccessors
from issue_orchestrator.control.required_issue_comment import (
    ReuseTechLeadProposalAction,
)
from issue_orchestrator.control.scoped_rework import (
    note_scoped_rework_started,
    note_scoped_rework_finished,
)
from issue_orchestrator.control.scoped_rework_launch import ScopedReworkLaunch
from issue_orchestrator.domain.models import OrchestratorState, PendingRework
from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.execution.pending_work_claim_store import (
    SqlitePendingWorkClaimStore,
)
from issue_orchestrator.infra.tech_lead_authority_store import (
    SqliteTechLeadAuthorityStore,
)
from issue_orchestrator.ports.comment_receipt import IssueCommentReceipt
from issue_orchestrator.ports.provider_resilience import ProviderErrorType
from tests.unit.control.test_scoped_rework import lane as lane, approved_action
from tests.unit.control.test_scoped_rework_repair import dispatcher


@pytest.mark.parametrize(
    "provider",
    [ProviderErrorType.AUTH, ProviderErrorType.QUOTA, ProviderErrorType.TRANSIENT],
)
@pytest.mark.parametrize(
    "condition",
    [
        "exact",
        "missing",
        "wrong-pr",
        "wrong-issue",
        "wrong-repo",
        "wrong-key",
        "held",
        "stale-head",
    ],
)
@pytest.mark.parametrize("reopen", [False, True])
def test_deferred_successor_proof_governs_reuse_projection_and_launch(
    lane, tmp_path, make_session, provider, condition, reopen
):
    executor, store, host, issue, pr, proposal, request, authority_db, _ = lane
    op = store.load_op(issue_number=501)
    applier = dispatcher(executor, host, store)
    assert applier.apply(approved_action(store, proposal)).success
    session = make_session(issue_number=5)
    claim = PendingWorkClaim(
        PendingWorkKind.REWORK,
        PendingRework(
            issue.key, "agent:coder", pr_number=94, scoped_request_keys=(request.key,)
        ),
    )
    claims_db = tmp_path / "claims.sqlite"
    claims = SqlitePendingWorkClaimStore(claims_db)
    ScopedReworkLaunch(store, host, applier.apply_all).bind(
        (request.key,), session.run_assets.identity
    )
    note_scoped_rework_started(store, session.run_assets.identity)
    ledger = InFlightWorkLedger(OrchestratorState(), claims)
    ledger.take(session, claim)
    outcome = SettlementOutcome.for_provider_error(provider)
    ledger.settle(session, outcome)
    note_scoped_rework_finished(
        store, session.run_assets.identity, False, work_outcome=outcome
    )
    if condition in {
        "missing",
        "wrong-pr",
        "wrong-issue",
        "wrong-repo",
        "wrong-key",
        "held",
    }:
        claims.consume_pending_work_claim(session.run_assets)
        changed = claim.request
        if condition == "wrong-pr":
            changed = replace(changed, pr_number=95)
        elif condition == "wrong-issue":
            changed = replace(
                changed, issue_key=GitHubIssueKey(request.target.repository, "6")
            )
        elif condition == "wrong-repo":
            changed = replace(changed, issue_key=GitHubIssueKey("other/repo", "5"))
        elif condition == "wrong-key":
            changed = replace(
                changed, scoped_request_keys=("different-head-or-finding",)
            )
        if condition != "missing":
            claims.hold_pending_work_claim(
                session.run_assets,
                PendingWorkClaim(PendingWorkKind.REWORK, changed),
                issue_number=5,
            )
            if condition != "held":
                claims.defer_pending_work_claim(session.run_assets)
    if condition == "stale-head":
        pr.head_sha = "b" * 40
    if reopen:
        claims = SqlitePendingWorkClaimStore(claims_db)
        store = SqliteTechLeadAuthorityStore(authority_db)
        executor.receipts, applier.tech_lead_ops = store, store
    executor.pending_successors = PendingWorkSuccessors(claims)
    executor.is_attempt_active = lambda *_: False
    assert (PendingWorkLaunchClaim(claim, claims).can_reclaim_deferred()) is (
        condition in {"exact", "stale-head"}
    )
    before = host.add_comment.call_count
    published = set()
    original_post = host.add_comment.side_effect

    def post(number, body):
        published.add((number, body))
        return original_post(number, body)

    host.add_comment.side_effect = post
    host.find_issue_comment_receipt.side_effect = lambda number, body: (
        IssueCommentReceipt(
            "receipt", "url", "user:7", hashlib.sha256(body.encode()).hexdigest()
        )
        if (number, body) in published
        else None
    )
    result = applier.apply(
        ReuseTechLeadProposalAction(
            number=501,
            required_op=op,
            comment="The original exact instruction remains owned",
        )
    )
    assert result.success is (condition == "exact")
    assert (host.add_comment.call_count > before) is (condition == "exact")
    view = executor.proposal_views()[0]
    assert view.status == ("queued" if condition == "exact" else "unavailable")
    assert not view.can_approve and not view.can_decline
    if condition == "exact":
        assert "exact durable request" in view.detail
        executor.validate_proposal_reuse(501, request)
    else:
        with pytest.raises(ValueError):
            executor.validate_proposal_reuse(501, request)


def test_receipt_projection_keeps_known_proposal_identity(lane):
    from issue_orchestrator.domain.scoped_rework import ReworkReceipt

    executor, store, _, _, _, proposal, request, _, _ = lane
    store.save_rework_receipt(ReworkReceipt(request, "queued"))
    view = executor.proposal_views()[0]
    assert view.proposal_issue_number == proposal.number
    assert view.status == "queued"
    assert not view.can_approve and not view.can_decline
