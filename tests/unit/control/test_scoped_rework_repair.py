"""Production dispatch and durable restart regressions for scoped ownership."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.actions import AddCommentAction, RequestReworkAction
from issue_orchestrator.control.claim_gate import ClaimGate, ClaimLostError
from issue_orchestrator.control.reconciliation import (
    ReconciliationRequired,
    build_expected_for_mutation,
)
from issue_orchestrator.control.scoped_rework_launch import ScopedReworkLaunch
from issue_orchestrator.domain.models import OrchestratorState, PendingRework
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.tech_lead_authority_store import (
    SqliteTechLeadAuthorityStore,
)
from tests.unit.control.test_scoped_rework import lane as lane, approved_action
from tests.runtime_lifecycle_helpers import make_action_applier


def dispatcher(executor, host, store):
    labels = MagicMock()
    labels.has_label.side_effect = lambda number, label: (
        label in host.get_issue(number).labels
    )
    labels.add_label.side_effect = lambda number, label: host.add_label(number, label)
    labels.remove_label.side_effect = lambda number, label: host.remove_label(
        number, label
    )
    fresh = MagicMock()
    fresh.read_issue_labels.side_effect = lambda number: list(
        host.get_issue(number).labels
    )
    applier = make_action_applier(
        labels=labels,
        sessions=MagicMock(),
        events=executor.events,
        repository_host=host,
        request_rework=executor,
        tech_lead_ops=store,
        needs_human_block=executor.block,
        fresh_issue_reader=fresh,
        reconcile=True,
    )
    executor.mutate = applier.apply_scoped_rework_mutation
    executor.before_write = applier.require_scoped_rework_authority
    return applier


@pytest.mark.parametrize("authority_loss", ["pause", "claim"])
@pytest.mark.parametrize("writes_before_loss", range(8))
def test_dispatch_stops_batch_before_each_scoped_effect_and_recovers_sqlite(
    lane,
    authority_loss,
    writes_before_loss,
):
    executor, store, host, issue, pr, proposal, request, db, _ = lane
    action = approved_action(store, proposal)
    applier = dispatcher(executor, host, store)
    effects = []
    manager = MagicMock()
    manager.check_winner.side_effect = lambda *args: len(effects) < writes_before_loss
    applier.claim_gate = (
        ClaimGate(manager, executor.events) if authority_loss == "claim" else None
    )
    applier.lease_id_lookup = lambda _: "lease"
    if authority_loss == "pause" and writes_before_loss == 0:
        pr.labels.append("io:needs-reconcile")

    def wrap(name):
        original = getattr(host, name).side_effect

        def write(*args):
            result = original(*args)
            effects.append((name, args))
            if authority_loss == "pause" and len(effects) == writes_before_loss:
                pr.labels.append("io:needs-reconcile")
            return result

        getattr(host, name).side_effect = write

    for name in ("add_comment", "add_label", "remove_label", "update_issue_state"):
        wrap(name)
    error = ReconciliationRequired if authority_loss == "pause" else ClaimLostError
    with pytest.raises(error):
        applier.apply_all([action, AddCommentAction(number=5, comment="after-batch")])
    assert len(effects) == writes_before_loss
    assert all(args != (5, "after-batch") for _, args in effects)
    assert proposal.state == "open"
    reopened = SqliteTechLeadAuthorityStore(db)
    executor.receipts = reopened
    applier.tech_lead_ops = reopened
    applier.claim_gate = None
    if "io:needs-reconcile" in pr.labels:
        pr.labels.remove("io:needs-reconcile")
    assert applier.apply(action).success
    assert reopened.load_rework_receipt(request.key).request == request
    assert proposal.state == "closed"


@pytest.mark.parametrize("at_launch", [False, True])
def test_forward_creation_rechecks_both_subjects_after_lookup(lane, at_launch):
    executor, store, host, issue, pr, proposal, request, db, _ = lane
    applier = dispatcher(executor, host, store)
    action = approved_action(store, proposal)
    if at_launch:
        assert applier.apply(action).success
    pr.state, issue.state = "merged", "closed"

    def lookup(**kwargs):
        pr.labels.append("io:needs-reconcile")
        return None

    host.find_issue_by_marker.side_effect = lookup
    with pytest.raises(ReconciliationRequired):
        if at_launch:
            launch = ScopedReworkLaunch(
                store,
                host,
                lambda actions, **_: all(r.success for r in applier.apply_all(actions)),
            )
            launch.admit(PendingRework(issue.key, "agent:coder", pr_number=94), 94)
        else:
            applier.apply(action)
    host.create_issue.assert_not_called()
    reopened = SqliteTechLeadAuthorityStore(db)
    assert reopened.load_rework_receipt(request.key).status == "executing"


@pytest.mark.parametrize("ungated", [False, True])
@pytest.mark.parametrize("paused_subject", [5, 94, 501])
def test_normal_tick_recovers_accepted_creation_after_sqlite_reopen_without_source_replay(
    lane, ungated, paused_subject
):
    from issue_orchestrator.control.fact_gatherer import FactGatherer
    from issue_orchestrator.control.tech_lead_ledger_planning import (
        plan_tech_lead_ledger_actions,
    )
    from issue_orchestrator.control.tech_lead_actions import (
        CreateTechLeadProposalIssueAction,
    )
    from issue_orchestrator.domain.tech_lead_session import TechLeadCreationOrigin

    executor, store, host, issue, pr, proposal, request, db, _ = lane
    op = store.load_op(issue_number=501)
    store.discard_op(issue_number=501)
    host.list_labels.return_value = [{"name": "proposed-tech-lead"}]
    host.list_milestones.return_value = []
    applier = dispatcher(executor, host, store)

    def accepted(**kwargs):
        proposal.body = kwargs["body"]
        raise TimeoutError("accepted; response lost")

    host.create_issue.side_effect = accepted
    create = CreateTechLeadProposalIssueAction(
        title="Original",
        body="Original report",
        labels=("proposed-tech-lead",),
        op=op,
        origin=TechLeadCreationOrigin.derived_from_anchor(5),
        expected=build_expected_for_mutation(),
    )
    assert not applier.apply(create).success
    assert store.list_ops() == ()
    store = SqliteTechLeadAuthorityStore(db)
    executor.receipts = store
    applier.tech_lead_ops = store
    if ungated:
        proposal.labels.clear()
    host.find_issue_by_marker.side_effect = lambda **kw: (
        501 if kw["marker"] in proposal.body else None
    )
    host.list_issues.return_value = [proposal]
    config = Config()
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_review_threshold = 0
    config.tech_lead.health_review.interval_minutes = 0
    from issue_orchestrator.control.health_review_trigger import (
        recover_pending_tech_lead_anchors,
    )

    startup = OrchestratorState()
    recover_pending_tech_lead_anchors(
        startup,
        repository_host=host,
        config=config,
        session_exists=lambda _: False,
        tech_lead_authority=store,
    )
    assert not startup.pending_tech_lead_reviews
    gatherer = FactGatherer(config, host, tech_lead_authority=store)
    facts = gatherer.gather_tech_lead_facts(
        OrchestratorState(), board_issues=[], now=1000
    )
    assert facts is not None and facts.pending_proposal_creations
    assert facts.existing_tech_lead_issue is None
    actions = plan_tech_lead_ledger_actions(config, facts)
    host.get_issue(paused_subject).labels.append("io:needs-reconcile")
    with pytest.raises(ReconciliationRequired):
        applier.apply_all(actions)
    assert store.list_ops() == () and store.list_pending_proposals()
    host.get_issue(paused_subject).labels.remove("io:needs-reconcile")
    assert all(result.success for result in applier.apply_all(actions))
    assert store.load_op(issue_number=501) == op
    assert not store.list_pending_proposals()
    assert executor.proposal_views()[0].feedback == request.feedback
    facts = gatherer.gather_tech_lead_facts(
        OrchestratorState(), board_issues=[], now=1001
    )
    assert len(facts.approved_tech_lead_ops) == int(ungated)
    assert all(result.success for result in applier.apply_all(actions))
    assert host.create_issue.call_count == 1


@pytest.mark.parametrize("provider", ["auth", "quota", "transient", None])
@pytest.mark.parametrize("crash_during_transfer", [False, True])
def test_completion_settlement_relaunches_exact_deferred_instruction_after_restart(
    lane,
    tmp_path,
    make_session,
    provider,
    crash_during_transfer,
):
    from issue_orchestrator.control.completion_handler import CompletionHandler
    from issue_orchestrator.control.in_flight_work import (
        InFlightWorkLedger,
        SettlementOutcome,
    )
    from issue_orchestrator.control.launch_transaction import PendingWorkLaunchClaim
    from issue_orchestrator.control.open_issue_corpus import OpenIssueCorpusManager
    from issue_orchestrator.control.scoped_rework import (
        note_scoped_rework_started,
        note_scoped_rework_finished,
    )
    from issue_orchestrator.control.session_completion import handle_session_completion
    from issue_orchestrator.control.tech_lead_run_activity import in_memory_run_activity
    from issue_orchestrator.domain.models import SessionStatus
    from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind
    from issue_orchestrator.domain.registered_completion import (
        CompletionProcessingPolicy,
    )
    from issue_orchestrator.domain.session_key import TaskKind
    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )
    from issue_orchestrator.execution.session_output_adapter import (
        FileSystemSessionOutput,
    )
    from issue_orchestrator.ports.open_issue_corpus_store import (
        InMemoryOpenIssueCorpusStore,
    )
    from issue_orchestrator.ports.provider_resilience import ProviderErrorType
    from tests.conftest import make_provider_availability
    from tests.unit.session_run_helpers import make_session_run_assets

    executor, store, host, issue, pr, proposal, request, db, _ = lane
    applier = dispatcher(executor, host, store)
    assert applier.apply(approved_action(store, proposal)).success
    session = make_session(
        issue_number=5,
        task=TaskKind.REWORK,
        terminal_id="rework-94",
        branch_name=pr.branch,
    )
    session.pr_number = 94
    state = OrchestratorState()
    state.active_sessions.append(session)
    rework = PendingRework(
        issue.key,
        "agent:coder",
        pr_number=94,
        feedback="original ordinary feedback",
        scoped_request_keys=(request.key,),
    )
    claim = PendingWorkClaim(PendingWorkKind.REWORK, rework)
    claims_path = tmp_path / "claims.sqlite"
    claims = SqlitePendingWorkClaimStore(claims_path)
    launch = ScopedReworkLaunch(store, host, MagicMock())
    assert (
        launch.claim(
            PendingWorkLaunchClaim(claim, claims), (request.key,)
        ).hold_before_spawn(session.run_assets, issue_number=5)
        is None
    )
    InFlightWorkLedger(state, claims).take(session, claim)
    note_scoped_rework_started(store, session.run_assets.identity)
    config = Config(repo_root=tmp_path)
    config.code_review_agent = None
    config.cleanup.without_tech_lead.close_ai_session_tabs = False
    output = FileSystemSessionOutput()
    handler = CompletionHandler(
        config=config,
        events=executor.events,
        repository_host=host,
        get_issue_machine_fn=lambda _: None,
        get_session_machine_fn=lambda _: None,
        get_review_machine_fn=lambda _: None,
        session_output=output,
        tech_lead_authority=store,
        open_issue_corpus=OpenIssueCorpusManager(
            host, InMemoryOpenIssueCorpusStore(), is_enabled=lambda: False
        ),
        provider_availability=make_provider_availability(config),
        tech_lead_run_activity=in_memory_run_activity(),
    )
    handle_session_completion(
        session=session,
        status=SessionStatus.BLOCKED,
        state=state,
        completion_handler=handler,
        action_applier=applier,
        observer=MagicMock(),
        worktree_manager=None,
        kill_session_fn=lambda _: None,
        config=config,
        session_output=output,
        pending_work_claims=claims,
        provider_error_type=ProviderErrorType(provider) if provider else None,
        processing_policy=CompletionProcessingPolicy.for_unprocessed_session(
            session.issue.agent_type, config.tech_lead_review_agent
        ),
    )
    store = SqliteTechLeadAuthorityStore(db)
    claims = SqlitePendingWorkClaimStore(claims_path)
    receipt = store.load_rework_receipt(request.key)
    assert receipt.request == request
    if provider is None:
        assert receipt.status == "failed" and not claims.list_unresolved_claims()
        return
    assert receipt.status == "active" and receipt.attempt == session.run_assets.identity
    assert len(state.pending_reworks) == 1
    restarted = OrchestratorState()
    assert InFlightWorkLedger(restarted, claims).recover_unresolved(MagicMock()) == 1
    replay = restarted.pending_reworks[0]
    assert replay == rework
    owner = ScopedReworkLaunch(store, host, MagicMock())
    instruction = owner.admit(replay, 94, work_claim=PendingWorkLaunchClaim(
        PendingWorkClaim(PendingWorkKind.REWORK, replay), claims))
    from issue_orchestrator.control.pending_work_successors import PendingWorkSuccessors
    executor.receipts = store
    executor.pending_successors = PendingWorkSuccessors(claims)
    executor.validate_proposal_reuse(501, request)
    view = executor.proposal_views()[0]
    assert view.status == "queued" and "exact durable request" in view.detail
    assert (
        request.feedback in instruction.feedback
        and request.report in instruction.feedback
    )
    second = make_session_run_assets(
        tmp_path / "replacement", session_name="rework-94", run_id="second"
    )
    # An equal request without the matching prior run cannot take the receipt.
    forged = replace(replay, feedback="replacement instructions")
    invalid = owner.claim(
        PendingWorkLaunchClaim(
            PendingWorkClaim(PendingWorkKind.REWORK, forged), claims
        ),
        instruction.keys,
    )
    assert invalid.hold_before_spawn(second, issue_number=5) is not None
    assert claims.list_unresolved_claims()[0].claim.request == replay
    if crash_during_transfer:
        # The new work claim may commit before receipt binding; startup must
        # recover that exact request and permit its next transfer.
        pending = PendingWorkLaunchClaim(
            PendingWorkClaim(PendingWorkKind.REWORK, replay), claims
        )
        assert pending.hold_before_spawn(second, issue_number=5) is None
        claims = SqlitePendingWorkClaimStore(claims_path)
        InFlightWorkLedger(OrchestratorState(), claims).recover_unresolved(MagicMock())
        second = make_session_run_assets(
            tmp_path / "replacement", session_name="rework-94", run_id="third"
        )
    replacement = owner.claim(
        PendingWorkLaunchClaim(
            PendingWorkClaim(PendingWorkKind.REWORK, replay), claims
        ),
        instruction.keys,
    )
    assert replacement.hold_before_spawn(second, issue_number=5) is None
    assert owner.before_spawn(instruction.keys, second.identity) is None
    note_scoped_rework_finished(
        store,
        session.run_assets.identity,
        False,
        work_outcome=SettlementOutcome.CONSUMED,
    )
    assert store.load_rework_receipt(request.key).attempt == second.identity
    completed_session = replace(session, run_assets=second)
    final_state = OrchestratorState(active_sessions=[completed_session])
    InFlightWorkLedger(final_state, claims).take(
        completed_session, PendingWorkClaim(PendingWorkKind.REWORK, replay)
    )
    handle_session_completion(
        session=completed_session,
        status=SessionStatus.COMPLETED,
        state=final_state,
        completion_handler=handler,
        action_applier=applier,
        observer=MagicMock(),
        worktree_manager=None,
        kill_session_fn=lambda _: None,
        config=config,
        session_output=output,
        pending_work_claims=claims,
        processing_policy=CompletionProcessingPolicy.for_unprocessed_session(
            completed_session.issue.agent_type, config.tech_lead_review_agent
        ),
    )
    assert not claims.list_unresolved_claims()
    assert (
        SqliteTechLeadAuthorityStore(db).load_rework_receipt(request.key).status
        == "completed"
    )
