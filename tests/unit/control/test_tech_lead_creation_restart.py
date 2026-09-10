"""Resume original gated creation through real tick dispatch and reopened SQLite."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.actions import AddCommentAction
from issue_orchestrator.control.claim_gate import ClaimGate, ClaimLostError
from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.control.reconciliation import (
    ExpectedState,
    ReconciliationRequired,
)
from issue_orchestrator.control.tech_lead_actions import (
    CreateTechLeadProposalIssueAction,
)
from issue_orchestrator.control.tech_lead_ledger_planning import (
    plan_tech_lead_ledger_actions,
)
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.domain.tech_lead_session import TechLeadCreationOrigin
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.tech_lead_authority_store import (
    SqliteTechLeadAuthorityStore,
)
from tests.unit.control.test_scoped_rework import lane as lane
from tests.unit.control.test_scoped_rework_repair import dispatcher


@pytest.fixture
def creation(lane):
    executor, store, host, issue, pr, proposal, request, db, _ = lane
    op = store.load_op(issue_number=501)
    store.discard_op(issue_number=501)
    anchor = Issue(77, "Original review", ["original-authority"], repo=issue.repo)
    original_get = host.get_issue.side_effect
    host.get_issue.side_effect = lambda number: (
        anchor if number == 77 else original_get(number)
    )
    host.get_issue_state.side_effect = lambda number: (
        host.get_issue(number).state if host.get_issue(number) is not None else None
    )
    host.list_labels.return_value = [{"name": "proposed-tech-lead"}]
    host.list_milestones.return_value = []
    host.list_issues.side_effect = lambda **_: (
        [proposal] if "<!-- tech-lead-proposal:" in proposal.body else []
    )
    applier = dispatcher(executor, host, store)
    action = CreateTechLeadProposalIssueAction(
        title="Original gated title",
        body="Original instruction",
        labels=("proposed-tech-lead",),
        op=op,
        origin=TechLeadCreationOrigin.derived_from_anchor(77),
        expected=ExpectedState.with_labels(
            required={"original-authority"}, forbidden={"io:needs-reconcile"}
        ),
    )
    return lane, anchor, applier, action


def tick(host, store, applier):
    config = Config()
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_review_threshold = 0
    config.tech_lead.health_review.interval_minutes = 0
    facts = FactGatherer(
        config, host, tech_lead_authority=store
    ).gather_tech_lead_facts(OrchestratorState(), board_issues=[], now=1000)
    assert facts is not None
    return applier.apply_all(plan_tech_lead_ledger_actions(config, facts))


def reopen(lane, applier):
    executor, _, host, _, _, _, _, db, _ = lane
    store = SqliteTechLeadAuthorityStore(db)
    executor.receipts = store
    applier.tech_lead_ops = store
    return host, store


def publish(host, proposal):
    def create(**kwargs):
        proposal.title, proposal.body = kwargs["title"], kwargs["body"]
        proposal.labels = list(kwargs["labels"])
        return {"number": proposal.number}

    host.create_issue.side_effect = create
    host.find_issue_by_marker.side_effect = lambda **kw: (
        proposal.number if kw["marker"] in proposal.body else None
    )


@pytest.mark.parametrize(
    "interruption",
    ["before-lookup", "before-create", "accepted-lost", "accepted-empty"],
)
def test_normal_ticks_complete_original_intent_once_after_restart(
    creation, interruption
):
    lane, anchor, applier, action = creation
    _, store, host, _, _, proposal, request, _, _ = lane
    publish(host, proposal)
    normal_create = host.create_issue.side_effect
    if interruption == "before-lookup":
        host.find_issue_by_marker.side_effect = TimeoutError(
            "lookup unavailable before create"
        )
    elif interruption == "before-create":
        host.list_labels.side_effect = TimeoutError(
            "pre-create label lookup unavailable"
        )
    else:

        def accepted(**kwargs):
            normal_create(**kwargs)
            if interruption == "accepted-empty":
                return None
            raise TimeoutError("accepted; response lost")

        host.create_issue.side_effect = accepted
    assert not applier.apply(action).success
    pending = store.list_pending_proposals()[0]
    assert pending.creation_authority.anchor_issue_number == anchor.number
    assert pending.creation_authority.required_labels == ("original-authority",)
    assert store.list_ops() == ()
    host, store = reopen(lane, applier)
    assert store.list_pending_proposals() == (pending,)
    host.list_labels.side_effect = None
    publish(host, proposal)
    # These are ordinary gathered ticks, with no original action resubmission.
    for _ in range(3):
        assert all(result.success for result in tick(host, store, applier))
    assert host.create_issue.call_count == 1
    assert store.list_pending_proposals() == ()
    assert store.load_op(issue_number=501) == action.op
    assert proposal.title == pending.title
    assert proposal.body == f"{pending.marker}\n{pending.body}"
    assert proposal.labels == list(pending.labels)
    assert host.create_issue.call_args.kwargs["milestone"] == pending.milestone
    assert store.load_op(issue_number=501).rework_request == request
    assert all(
        call.kwargs["authoritative"]
        for call in host.find_issue_by_marker.call_args_list
    )


@pytest.mark.parametrize(
    "refusal",
    [
        "ambiguous",
        "stale-head",
        "missing-gate",
        "original-expectation",
        "legacy-authority",
    ],
)
def test_absence_cannot_bypass_original_authority_or_eligibility(creation, refusal):
    lane, anchor, applier, action = creation
    _, store, host, _, pr, proposal, _, _, _ = lane
    host.find_issue_by_marker.side_effect = TimeoutError("interrupted before lookup")
    assert not applier.apply(action).success
    pending = store.list_pending_proposals()[0]
    if refusal == "legacy-authority":
        store.discard_pending_proposal(pending.key)
        store.record_pending_proposal(replace(pending, creation_authority=None))
    host, store = reopen(lane, applier)
    publish(host, proposal)
    if refusal == "ambiguous":
        host.find_issue_by_marker.side_effect = TimeoutError("absence is not proven")
    elif refusal == "stale-head":
        pr.head_sha = "b" * 40
    elif refusal == "missing-gate":
        host.list_labels.return_value = []
    elif refusal == "original-expectation":
        anchor.labels.clear()
    for _ in range(3):
        if refusal == "original-expectation":
            with pytest.raises(ReconciliationRequired):
                tick(host, store, applier)
        else:
            results = tick(host, store, applier)
            assert any(not result.success for result in results)
    assert host.create_issue.call_count == 0
    assert store.list_ops() == ()
    assert len(store.list_pending_proposals()) == 1


@pytest.mark.parametrize("subject", [77, 5, 94])
@pytest.mark.parametrize("loss", ["pause", "claim"])
def test_recovery_rechecks_every_creation_subject_after_lookup_and_stops_batch(
    creation, subject, loss
):
    lane, _, applier, action = creation
    _, _, host, _, _, proposal, _, _, _ = lane
    host.find_issue_by_marker.side_effect = TimeoutError("before creation")
    assert not applier.apply(action).success
    host, store = reopen(lane, applier)
    publish(host, proposal)
    lost = set()
    manager = MagicMock()
    manager.check_winner.side_effect = lambda number, *_: number not in lost
    applier.claim_gate = ClaimGate(manager, applier.events) if loss == "claim" else None
    applier.lease_id_lookup = lambda _: "lease"

    def absent(**kwargs):
        if loss == "pause":
            host.get_issue(subject).labels.append("io:needs-reconcile")
        else:
            lost.add(subject)
        return None

    host.find_issue_by_marker.side_effect = absent
    # Gather through the normal production fact/planner path, then append a
    # canary to prove authority failure stops the entire batch.
    config = Config()
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_review_threshold = 0
    config.tech_lead.health_review.interval_minutes = 0
    facts = FactGatherer(
        config, host, tech_lead_authority=store
    ).gather_tech_lead_facts(OrchestratorState(), board_issues=[], now=1000)
    actions = plan_tech_lead_ledger_actions(config, facts)
    with pytest.raises(ReconciliationRequired if loss == "pause" else ClaimLostError):
        applier.apply_all([*actions, AddCommentAction(number=5, comment="after-batch")])
    host.create_issue.assert_not_called()
    host.add_comment.assert_not_called()
    assert store.list_ops() == () and len(store.list_pending_proposals()) == 1


def test_restart_after_local_commit_never_recreates_even_when_remote_lookup_is_absent(
    creation,
):
    lane, _, applier, action = creation
    _, store, host, _, _, proposal, _, _, _ = lane
    host.find_issue_by_marker.side_effect = TimeoutError("interrupted")
    assert not applier.apply(action).success
    pending = store.list_pending_proposals()[0]
    # Exact durable state at interruption between record_op and pending cleanup.
    store.record_op(issue_number=501, op=pending.op)
    host, store = reopen(lane, applier)
    publish(host, proposal)
    assert all(result.success for result in tick(host, store, applier))
    assert store.list_pending_proposals() == ()
    assert store.load_op(issue_number=501) == pending.op
    host.create_issue.assert_not_called()


@pytest.mark.parametrize("loss", ["pause", "claim", "stale"])
def test_recovery_rechecks_authority_between_label_provisioning_and_create(
    creation, loss
):
    lane, anchor, applier, action = creation
    _, _, host, _, pr, proposal, _, _, _ = lane
    action = replace(action, labels=(*action.labels, "tech-lead"))
    host.find_issue_by_marker.side_effect = TimeoutError("before creation")
    assert not applier.apply(action).success
    host, store = reopen(lane, applier)
    publish(host, proposal)
    lost = []
    manager = MagicMock()
    manager.check_winner.side_effect = lambda *_: not lost
    applier.claim_gate = ClaimGate(manager, applier.events) if loss == "claim" else None
    applier.lease_id_lookup = lambda _: "lease"

    def label_created(*args, **kwargs):
        if loss == "pause":
            anchor.labels.append("io:needs-reconcile")
        elif loss == "stale":
            pr.head_sha = "b" * 40
        else:
            lost.append(True)

    host.create_label.side_effect = label_created
    if loss == "stale":
        assert any(not result.success for result in tick(host, store, applier))
    else:
        with pytest.raises(
            ReconciliationRequired if loss == "pause" else ClaimLostError
        ):
            tick(host, store, applier)
    host.create_label.assert_called_once()
    host.create_issue.assert_not_called()
    assert store.list_ops() == () and store.list_pending_proposals()


@pytest.mark.parametrize("legacy", [False, True])
def test_accepted_response_loss_remains_ambiguous_until_marker_is_observable(
    creation, legacy
):
    lane, _, applier, action = creation
    _, store, host, _, _, proposal, _, _, _ = lane
    publish(host, proposal)
    normal_create = host.create_issue.side_effect

    def accepted(**kwargs):
        normal_create(**kwargs)
        raise TimeoutError("response lost")

    host.create_issue.side_effect = accepted
    assert not applier.apply(action).success
    pending = store.list_pending_proposals()[0]
    if legacy:
        # Prior on-disk format lacks creation authority; attribution can still
        # recover its accepted issue, but absence must never grant a new create.
        from issue_orchestrator.domain.tech_lead_proposal_creation import (
            PendingTechLeadProposal,
        )

        raw = pending.to_dict()
        del raw["creation_authority"]
        store.discard_pending_proposal(pending.key)
        store.record_pending_proposal(PendingTechLeadProposal.from_dict(raw))
    host, store = reopen(lane, applier)
    host.find_issue_by_marker.side_effect = TimeoutError("incomplete search")
    assert any(not result.success for result in tick(host, store, applier))
    assert host.create_issue.call_count == 1 and store.list_pending_proposals()
    publish(host, proposal)
    for _ in range(2):
        assert all(result.success for result in tick(host, store, applier))
    assert host.create_issue.call_count == 1
    assert store.load_op(issue_number=501) == pending.op
    assert not store.list_pending_proposals()
