"""Disposition commit, finite ownership, and failure/replay boundaries."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.actions import (
    AddCommentAction,
    RecordTechLeadDispositionAction,
)
from issue_orchestrator.control.tech_lead_dispositions import (
    TechLeadDispositionLedger,
    apply_record_tech_lead_disposition,
    disposition_comment,
    disposition_marker,
    recovery_tracker_grants,
)
from issue_orchestrator.control.tech_lead_reset_retry import (
    apply_completion_actions_gated,
    evaluate_required_act_level_outcome,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadDisposition,
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore

BLOCKED, TRACKER = 6410, 6914
NOW = datetime(2026, 8, 9, 1, tzinfo=timezone.utc)


def _disposition(issue_number=BLOCKED, tracker=TRACKER, **kwargs):
    return TechLeadDisposition(
        issue_number=issue_number,
        tracker_issue_number=tracker,
        rationale="Validated work is stranded; recover it, do NOT reset.",
        source_run_id="run-1",
        source_session_name="issue-6410",
        source_action_id="A2",
        recorded_at="2026-08-09T00:00:00+00:00",
        **kwargs,
    )


class _Host:
    def __init__(self):
        self.issue = Issue(
            number=BLOCKED,
            title="stuck",
            body=f"Depends-on: #{TRACKER}",
            labels=["blocked-failed"],
        )
        self.comments = []
        self.state = "open"
        self.comment_error = None
        self.read_error = None
        self.after_comment = lambda: None
        self.state_reads = []

    def get_issue(self, number):
        assert number == BLOCKED
        return self.issue

    def get_issue_state(self, number):
        self.state_reads.append(number)
        if self.read_error:
            raise self.read_error
        return self.state

    def find_issue_comment_receipt(self, number, *, body):
        from issue_orchestrator.ports.comment_receipt import IssueCommentReceipt
        import hashlib
        if body in self.comments:
            return IssueCommentReceipt("1", "https://example.test/comment", "user:7",
                hashlib.sha256(body.encode()).hexdigest())
        return None

    def add_comment(self, number, comment):
        if self.comment_error:
            raise self.comment_error
        self.comments.append(comment)
        self.after_comment()
        return "https://example.test/comment"


class _Harness:
    def __init__(self, store=None):
        self.store = store if store is not None else InMemoryTechLeadAuthorityStore()
        self.host = _Host()
        self.now = NOW
        self.store.record(
            run_id="run-1",
            session_name="issue-6410",
            authority=TechLeadLaunchAuthority(
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                anchor_issue_number=BLOCKED,
                focus_issue_number=BLOCKED,
                recovery_tracker_numbers=(TRACKER,),
            ),
        )
        self.applier = ActionApplier(
            labels=MagicMock(),
            sessions=MagicMock(),
            events=MagicMock(),
            repository_host=self.host,
        )
        self.guards = []
        self.claim_error = None

    def guard(self, action, number):
        self.guards.append(number)
        if self.claim_error:
            raise self.claim_error

    def apply(self, action):
        if isinstance(action, RecordTechLeadDispositionAction):
            return apply_record_tech_lead_disposition(
                action,
                authority=self.store,
                repository_host=self.host,
                apply_action=self.applier.apply,
                require_expected=self.guard,
                verify_claim=self.guard,
                clock=lambda: self.now,
            )
        return self.applier.apply(action)

    def apply_all(self, actions):
        return [self.apply(action) for action in actions]

    def finish(self):
        return apply_completion_actions_gated(
            self,
            [
                AddCommentAction(number=BLOCKED, comment="success-only"),
                RecordTechLeadDispositionAction(disposition=_disposition()),
            ],
            issue_number=BLOCKED,
        )

    def ledger(self):
        return TechLeadDispositionLedger(
            authority=self.store, issue_state=self.host.get_issue_state, now=self.now
        )


def test_publication_commits_explanation_then_binding_and_success_effects():
    harness = _Harness()
    seen = []
    harness.host.after_comment = lambda: seen.append(
        harness.store.load_disposition(issue_number=BLOCKED)
    )
    results, error = harness.finish()
    assert error is None and not evaluate_required_act_level_outcome(results).failed
    assert seen[0].phase == "prepared"
    assert seen[1] == _disposition()
    assert "#6914" in harness.host.comments[0]
    assert "2026-08-10" in harness.host.comments[0]
    assert harness.host.comments[-1] == "success-only"
    assert harness.ledger().owned_issue_numbers() == {BLOCKED}


def test_comment_failure_preserves_pending_command_but_withholds_success_effect():
    harness = _Harness()
    harness.host.comment_error = RuntimeError("comment unavailable")
    results, _ = harness.finish()
    assert evaluate_required_act_level_outcome(results).failed
    assert harness.store.load_disposition(issue_number=BLOCKED).phase == "prepared"
    assert harness.host.comments == []


def test_store_failure_withholds_success_and_replay_deduplicates_remote_comment():
    class Store(InMemoryTechLeadAuthorityStore):
        fail = True

        def transition_disposition(self, *, previous, disposition):
            if self.fail and disposition.phase == "waiting":
                raise RuntimeError("store unavailable")
            return super().transition_disposition(previous=previous, disposition=disposition)

    store = Store()
    harness = _Harness(store)
    results, _ = harness.finish()
    assert evaluate_required_act_level_outcome(results).failed
    assert store.load_disposition(issue_number=BLOCKED).phase == "prepared"
    assert len(harness.host.comments) == 1
    store.fail = False
    results, _ = harness.finish()
    assert not evaluate_required_act_level_outcome(results).failed
    assert len(harness.host.comments) == 2  # original explanation + success-only
    assert store.load_disposition(issue_number=BLOCKED) == _disposition()


def test_crash_after_remote_write_replays_the_same_marker():
    harness = _Harness()

    def crash():
        raise KeyboardInterrupt("crash after remote commit")

    harness.host.after_comment = crash
    with pytest.raises(KeyboardInterrupt):
        harness.apply(RecordTechLeadDispositionAction(disposition=_disposition()))
    assert harness.store.load_disposition(issue_number=BLOCKED).phase == "prepared"
    harness.host.after_comment = lambda: None
    assert harness.apply(
        RecordTechLeadDispositionAction(disposition=_disposition())
    ).success
    assert len(harness.host.comments) == 1


@pytest.mark.parametrize("failure", ["claim", "dependency", "tracker"])
def test_drift_after_explanation_cannot_commit_the_binding(failure):
    harness = _Harness()

    def drift():
        if failure == "claim":
            harness.claim_error = RuntimeError("claim lost")
        elif failure == "dependency":
            harness.host.issue = replace(harness.host.issue, body="dependency revoked")
        else:
            harness.host.state = "closed"

    harness.host.after_comment = drift
    results, _ = harness.finish()
    assert evaluate_required_act_level_outcome(results).failed
    assert harness.store.load_disposition(issue_number=BLOCKED).phase == ("reassess" if failure == "tracker" else "prepared")
    assert len(harness.host.comments) == 1


def test_no_launch_grant_means_no_remote_or_local_write():
    harness = _Harness()
    harness.store.discard(run_id="run-1", session_name="issue-6410")
    results, _ = harness.finish()
    assert evaluate_required_act_level_outcome(results).failed
    assert harness.host.comments == []
    assert harness.store.load_disposition(issue_number=BLOCKED) is None


@pytest.mark.parametrize("state", ["closed", None])
def test_authoritatively_lapsed_tracker_releases_ownership_but_keeps_context(state):
    harness = _Harness()
    harness.store.transition_disposition(previous=None, disposition=_disposition())
    harness.host.state = state
    assert harness.ledger().owned_issue_numbers() == set()
    assert harness.store.load_disposition(issue_number=BLOCKED).phase == "reassess"
    harness.host.read_error = RuntimeError("temporary outage")
    assert harness.ledger().owned_issue_numbers() == set()


def test_unknown_tracker_cannot_extend_expired_binding():
    harness = _Harness()
    harness.store.transition_disposition(previous=None, disposition=_disposition())
    harness.now += timedelta(days=3)
    harness.host.read_error = RuntimeError("rate limited")
    assert harness.ledger().owned_issue_numbers() == set()
    assert harness.store.load_disposition(issue_number=BLOCKED) .phase == "reassess"


def test_deadline_is_finite_across_restart_and_repeated_decisions():
    harness = _Harness()
    harness.finish()
    harness.now += timedelta(hours=12)
    later = replace(
        _disposition(), source_action_id="A3", recorded_at=harness.now.isoformat()
    )
    assert not harness.apply(RecordTechLeadDispositionAction(disposition=later)).success
    assert (
        harness.store.load_disposition(issue_number=BLOCKED).recorded_at
        == _disposition().recorded_at
    )
    harness.now += timedelta(days=1)
    assert harness.ledger().owned_issue_numbers() == set()
    assert not harness.apply(RecordTechLeadDispositionAction(disposition=later)).success
    assert (
        harness.store.load_disposition(issue_number=BLOCKED).recorded_at
        == _disposition().recorded_at
    )


def test_target_recovery_removes_even_lapsed_incident_context():
    harness = _Harness()
    harness.store.transition_disposition(previous=None, disposition=_disposition())
    harness.now += timedelta(days=2)
    assert harness.ledger().release_recovered(frozenset(), frozenset({BLOCKED})) == {BLOCKED}
    assert harness.store.load_disposition(issue_number=BLOCKED).phase == "recovered"


def test_tracker_reads_are_bounded_by_distinct_trackers():
    harness = _Harness()
    harness.store.transition_disposition(previous=None, disposition=_disposition())
    harness.store.transition_disposition(previous=None, disposition=_disposition(issue_number=6411))
    assert harness.ledger().owned_issue_numbers() == {6410, 6411}
    assert harness.host.state_reads == [TRACKER]


def test_only_explicit_same_repository_dependencies_grant_tracker_authority():
    issue = Issue(
        number=BLOCKED,
        title="x",
        labels=[],
        body=f"Mentions #999.\nDepends-on: #{TRACKER}\nDepends-on: elsewhere/repo#444\nDepends-on: #{BLOCKED}",
    )
    assert recovery_tracker_grants(issue) == (TRACKER,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("rationale", None),
        ("source_run_id", 7),
        ("source_session_name", {}),
        ("source_action_id", False),
        ("recorded_at", "yesterday"),
        ("recorded_at", "2026-08-09T00:00:00"),
        ("finding_ids", [None]),
    ],
)
def test_malformed_persisted_disposition_fails_fast(field, value):
    data = _disposition().to_dict()
    data[field] = value
    with pytest.raises(ValueError):
        TechLeadDisposition.from_dict(data)


def test_comment_explains_recovery_and_unknown_state():
    comment = disposition_comment(_disposition())
    assert "read outage preserves" in comment
    assert "Clearing this issue's blocking state releases" in comment


@pytest.mark.parametrize("existing_human", [False, True])
def test_human_disposition_owns_sweep_and_preserves_existing_requests(
    tmp_path, existing_human
):
    from issue_orchestrator.control.actions import EscalateTechLeadDispositionAction
    from issue_orchestrator.control.label_manager import LabelManager
    from issue_orchestrator.control.needs_human_block import (
        NeedsHumanBlock,
        NeedsHumanCause,
    )
    from issue_orchestrator.control.stuck_sweep import run_stuck_sweep
    from issue_orchestrator.control.tech_lead_needs_human_reconcile import (
        TechLeadNeedsHumanLifecycle,
    )
    from issue_orchestrator.domain.models import OrchestratorState
    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )
    from issue_orchestrator.infra.config import Config

    config = Config()
    labels = LabelManager(config)

    class Host(_Host):
        def __init__(self):
            super().__init__()
            self.live = {"blocked-failed"} | (
                {labels.needs_human} if existing_human else set()
            )

        def get_issue_labels_fresh(self, number):
            return list(self.live)

        def read_issue_labels(self, number):
            return list(self.live)

        def has_label(self, number, label):
            return label in self.live

        def add_label(self, number, label):
            self.live.add(label)

        def remove_label(self, number, label):
            self.live.discard(label)

        def list_issues(self, **kwargs):
            return [replace(self.issue, labels=list(self.live))]

    host = Host()
    causes = SqlitePendingWorkClaimStore.for_repo(tmp_path)
    block = NeedsHumanBlock(
        needs_human_label=labels.needs_human,
        tech_lead_marker=labels.tech_lead_needs_human,
        labels=host,
        read_labels=host.get_issue_labels_fresh,
        quarantined_issue_numbers=frozenset,
        causes=causes,
    )
    applier = ActionApplier(
        labels=host,
        sessions=MagicMock(),
        events=MagicMock(),
        repository_host=host,
        label_manager=labels,
        needs_human_block=block,
        fresh_issue_reader=host,
        reconcile=True,
    )
    action = EscalateTechLeadDispositionAction(
        issue_number=BLOCKED, comment="Need a human decision"
    )
    results, error = apply_completion_actions_gated(
        applier, [action], issue_number=BLOCKED
    )
    assert error is None and not evaluate_required_act_level_outcome(results).failed
    assert labels.tech_lead_needs_human in host.live
    assert labels.needs_human in host.live
    assert host.comments == ["Need a human decision"]
    state = OrchestratorState()
    state.recovery_attempts = {BLOCKED: 1}
    for _ in range(2):
        assert run_stuck_sweep(config, state, host, labels, 1.0).recovered == ()
        assert state.recovery_attempts == {BLOCKED: 1}
    if existing_human:
        assert NeedsHumanCause.SESSION_LIFECYCLE.value in causes.needs_human_causes(
            BLOCKED
        )
        assert block.held_by_another_cause(
            BLOCKED, excluding=NeedsHumanCause.TECH_LEAD_ESCALATION
        )
    # A fresh process follows marker provenance, preserving the human outcome.
    owner = TechLeadNeedsHumanLifecycle(
        labels=labels,
        events=MagicMock(),
        read_labels=host.get_issue_labels_fresh,
        discover_marked_issue_numbers=lambda: [BLOCKED],
        apply_actions=lambda actions, context: all(
            r.success for r in applier.apply_all(actions)
        ),
        needs_human_block=block,
    )
    owner.reconcile([])
    assert labels.tech_lead_needs_human in host.live
    # Explicit operator release removes both owner and shared block; still
    # blocked work becomes eligible again with its prior budget intact.
    host.live.difference_update({labels.tech_lead_needs_human, labels.needs_human})
    assert [
        f.issue_number
        for f in run_stuck_sweep(config, state, host, labels, 2.0).recovered
    ] == [BLOCKED]


def test_same_command_identity_cannot_rebind_its_explanation():
    harness = _Harness()
    harness.finish()
    changed = replace(_disposition(), rationale="Different remedy")
    assert not harness.apply(
        RecordTechLeadDispositionAction(disposition=changed)
    ).success
    assert harness.store.load_disposition(issue_number=BLOCKED) == _disposition()
    assert len(harness.host.comments) == 2


@pytest.mark.parametrize("crash_after_write", [False, True])
def test_tick_resumes_persisted_intent_after_session_authority_is_disposed(tmp_path, crash_after_write):
    from issue_orchestrator.control.fact_gatherer import FactGatherer
    from issue_orchestrator.domain.models import OrchestratorState
    from issue_orchestrator.control.tech_lead_ledger_planning import plan_tech_lead_ledger_actions
    from issue_orchestrator.control.stuck_sweep import run_stuck_sweep
    from issue_orchestrator.control.label_manager import LabelManager
    from issue_orchestrator.infra.config import Config
    from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore

    path = tmp_path / "authority.sqlite"
    store = SqliteTechLeadAuthorityStore(path)
    harness = _Harness(store)
    def crash():
        raise KeyboardInterrupt("process terminated")
    if crash_after_write:
        harness.host.after_comment = crash
    else:
        harness.host.comment_error = KeyboardInterrupt("before remote write")
    with pytest.raises(KeyboardInterrupt):
        harness.apply(RecordTechLeadDispositionAction(disposition=_disposition()))
    assert store.load_disposition(issue_number=BLOCKED).phase == "prepared"
    store.discard(run_id="run-1", session_name="issue-6410")

    resumed = SqliteTechLeadAuthorityStore(path)
    harness.store = resumed
    harness.host.after_comment = lambda: None
    harness.host.comment_error = None
    harness.host.list_issues = lambda **kwargs: [harness.host.issue] if not kwargs.get("labels") else []
    config = Config()
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_review_threshold = 0
    config.tech_lead.health_review.interval_minutes = 0
    config.tech_lead.stuck_sweep.enabled = False
    state = OrchestratorState()
    state.recovery_attempts[BLOCKED] = 2
    gatherer = FactGatherer(config=config, repository_host=harness.host, tech_lead_authority=resumed)
    facts = gatherer.gather_tech_lead_facts(state, board_issues=[], now=NOW.timestamp())
    assert facts is not None
    assert len(facts.pending_dispositions) == 1
    assert resumed.load(run_id="run-1", session_name="issue-6410") is None
    before = run_stuck_sweep(config, state, harness.host, LabelManager(config), NOW.timestamp(), dispositions=harness.ledger())
    assert before.recovered == () and state.recovery_attempts == {BLOCKED: 2}
    planned = plan_tech_lead_ledger_actions(config, facts)
    assert len(planned) == 1 and isinstance(planned[0], RecordTechLeadDispositionAction)
    assert all(result.success for result in harness.apply_all(planned))
    assert len(harness.host.comments) == 1
    assert resumed.load_disposition(issue_number=BLOCKED).phase == "waiting"
    facts = gatherer.gather_tech_lead_facts(state, board_issues=[], now=NOW.timestamp())
    assert facts is None or facts.pending_dispositions == ()


@pytest.mark.parametrize("first_observation", ["closed", None, "expired"])
def test_lapse_is_latched_across_sqlite_restart(tmp_path, first_observation):
    from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
    path = tmp_path / "authority.sqlite"
    store = SqliteTechLeadAuthorityStore(path)
    harness = _Harness(store)
    store.transition_disposition(previous=None, disposition=_disposition())
    if first_observation == "expired":
        harness.now += timedelta(days=2)
        harness.host.read_error = RuntimeError("outage")
    else:
        harness.host.state = first_observation
    assert harness.ledger().owned_issue_numbers() == set()
    reopened = SqliteTechLeadAuthorityStore(path)
    harness.store = reopened
    harness.host.read_error = RuntimeError("outage")
    assert harness.ledger().owned_issue_numbers() == set()
    harness.host.read_error, harness.host.state = None, "open"
    assert harness.ledger().owned_issue_numbers() == set()
    assert reopened.load_disposition(issue_number=BLOCKED).phase == "reassess"


def test_sqlite_compare_and_transition_rejects_stale_binding_and_recovery(tmp_path):
    from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
    path = tmp_path / "authority.sqlite"
    first, second = SqliteTechLeadAuthorityStore(path), SqliteTechLeadAuthorityStore(path)
    prepared = replace(_disposition(), phase="prepared")
    assert first.transition_disposition(previous=None, disposition=prepared)
    assert not second.transition_disposition(previous=None, disposition=prepared)
    waiting = replace(prepared, phase="waiting")
    assert second.transition_disposition(previous=prepared, disposition=waiting)
    assert not first.transition_disposition(previous=prepared, disposition=replace(prepared, phase="reassess"))
    recovered = replace(waiting, phase="recovered", recovered_at=NOW.isoformat())
    assert first.transition_disposition(previous=waiting, disposition=recovered)
    assert not second.transition_disposition(previous=waiting, disposition=waiting)


def test_recovered_tombstone_rejects_old_command_but_allows_a_new_incident():
    harness = _Harness()
    assert not evaluate_required_act_level_outcome(harness.finish()[0]).failed
    harness.ledger().release_recovered(frozenset(), frozenset({BLOCKED}))
    assert not harness.apply(RecordTechLeadDispositionAction(disposition=_disposition())).success
    harness.now += timedelta(days=2)
    next_incident = replace(_disposition(), source_run_id="new-run", recorded_at=harness.now.isoformat())
    grant = harness.store.load(run_id="run-1", session_name="issue-6410")
    harness.store.record(run_id="new-run", session_name="issue-6410", authority=grant)
    assert harness.apply(RecordTechLeadDispositionAction(disposition=next_incident)).success
    assert harness.store.load_disposition(issue_number=BLOCKED).recorded_at == harness.now.isoformat()


@pytest.mark.parametrize("different_action", [False, True])
def test_recovery_rejects_a_command_from_the_old_launch(different_action):
    harness = _Harness()
    harness.finish()
    harness.ledger().release_recovered(frozenset(), frozenset({BLOCKED}))
    proposed = replace(_disposition(), source_action_id="A9" if different_action else "A2")
    assert not harness.apply(RecordTechLeadDispositionAction(disposition=proposed)).success
    assert harness.store.load_disposition(issue_number=BLOCKED).phase == "recovered"


def test_pending_disposition_failure_is_owned_without_a_new_human_gate():
    from issue_orchestrator.control.tech_lead_reset_retry import build_required_act_level_failure_actions
    harness = _Harness()
    harness.host.comment_error = RuntimeError("temporary remote failure")
    results, _ = harness.finish()
    outcome = evaluate_required_act_level_outcome(results)
    assert outcome.failed and len(outcome.pending_dispositions) == 1
    assert build_required_act_level_failure_actions(issue_number=BLOCKED,
        needs_human_label="needs-human", outcome=outcome, session_id="issue-6410", runtime_minutes=1) == []
    assert harness.ledger().owned_issue_numbers() == {BLOCKED}


def test_production_replay_retains_the_pause_guard():
    from issue_orchestrator.control.tech_lead_ledger_planning import plan_tech_lead_ledger_actions
    from issue_orchestrator.control.reconciliation import ReconciliationRequired
    from issue_orchestrator.domain.models import TechLeadFacts
    from issue_orchestrator.infra.config import Config
    prepared = replace(_disposition(), phase="prepared")
    harness = _Harness()
    assert harness.store.transition_disposition(previous=None, disposition=prepared)
    facts = TechLeadFacts(pending_dispositions=(prepared,))
    [action] = plan_tech_lead_ledger_actions(Config(), facts)
    fresh = MagicMock()
    fresh.read_issue_labels.return_value = ["blocked-failed", "io:needs-reconcile"]
    applier = ActionApplier(labels=MagicMock(), sessions=MagicMock(), events=MagicMock(),
        repository_host=harness.host, tech_lead_ops=harness.store,
        fresh_issue_reader=fresh, reconcile=True)
    with pytest.raises(ReconciliationRequired):
        applier.apply(action)
    assert harness.host.comments == []
    assert harness.store.load_disposition(issue_number=BLOCKED) == prepared


@pytest.mark.parametrize("persistent", [False, True])
def test_competing_replay_cannot_publish_while_first_owns_marker_check(tmp_path, persistent):
    from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
    if persistent:
        path = tmp_path / "authority.sqlite"
        first_store, second_store = SqliteTechLeadAuthorityStore(path), SqliteTechLeadAuthorityStore(path)
    else:
        first_store = second_store = InMemoryTechLeadAuthorityStore()
    first, second = _Harness(first_store), _Harness(second_store)
    # Both executors talk to the same remote issue.
    second.host = first.host
    second.applier = first.applier
    competitor_results = []
    original_read = first.host.find_issue_comment_receipt
    def interleaved_read(number, *, body):
        present = original_read(number, body=body)
        competitor_results.append(second.apply(RecordTechLeadDispositionAction(disposition=_disposition())))
        return present
    first.host.find_issue_comment_receipt = interleaved_read
    assert first.apply(RecordTechLeadDispositionAction(disposition=_disposition())).success
    competing = competitor_results[0]
    assert not competing.success and competing.details["pending_disposition"] is True
    assert first_store.load_disposition(issue_number=BLOCKED).phase == "waiting"
    assert len(first.host.comments) == 1
    first.host.find_issue_comment_receipt = original_read
    assert second.apply(RecordTechLeadDispositionAction(disposition=_disposition())).success
    assert len(first.host.comments) == 1


@pytest.mark.parametrize("advance_at", ["marker", "comment"])
def test_publication_rechecks_deadline_after_remote_calls(advance_at):
    harness = _Harness()
    if advance_at == "marker":
        def late_read(number, *, body):
            harness.now += timedelta(days=2)
            return None
        harness.host.find_issue_comment_receipt = late_read
    else:
        def late_write():
            harness.now += timedelta(days=2)
        harness.host.after_comment = late_write
    assert not harness.apply(RecordTechLeadDispositionAction(disposition=_disposition())).success
    assert harness.store.load_disposition(issue_number=BLOCKED).phase == "reassess"
    assert len(harness.host.comments) == (0 if advance_at == "marker" else 1)
    assert harness.ledger().owned_issue_numbers() == set()


def test_publisher_rechecks_durable_identity_after_claim_admission():
    from contextlib import contextmanager
    class Store(InMemoryTechLeadAuthorityStore):
        @contextmanager
        def disposition_publication(self, *, issue_number):
            with super().disposition_publication(issue_number=issue_number) as acquired:
                row = self.load_disposition(issue_number=issue_number)
                assert self.transition_disposition(previous=row, disposition=replace(row,
                    phase="recovered", recovered_at=NOW.isoformat()))
                yield acquired
    harness = _Harness(Store())
    assert not harness.apply(RecordTechLeadDispositionAction(disposition=_disposition())).success
    assert harness.host.comments == []
    assert harness.store.load_disposition(issue_number=BLOCKED).phase == "recovered"


@pytest.mark.parametrize("state", ["closed", None])
@pytest.mark.parametrize("after", ["reopen", "outage"])
def test_publish_time_tracker_lapse_is_irreversible_across_restart(tmp_path, state, after):
    from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
    path = tmp_path / "authority.sqlite"
    harness = _Harness(SqliteTechLeadAuthorityStore(path))
    harness.host.after_comment = lambda: setattr(harness.host, "state", state)
    command = RecordTechLeadDispositionAction(disposition=_disposition())
    assert not harness.apply(command).success
    assert harness.store.load_disposition(issue_number=BLOCKED).phase == "reassess"
    restarted = _Harness(SqliteTechLeadAuthorityStore(path))
    restarted.host = harness.host
    restarted.host.state = "open"
    restarted.host.after_comment = lambda: None
    if after == "outage":
        restarted.host.read_error = RuntimeError("unknown tracker state")
    assert restarted.ledger().owned_issue_numbers() == set()
    assert not restarted.apply(command).success
    assert restarted.store.load_disposition(issue_number=BLOCKED).phase == "reassess"
    assert len(restarted.host.comments) == 1


def test_marker_only_comment_cannot_satisfy_disposition_publication():
    harness = _Harness()
    harness.host.comments.append(disposition_marker(_disposition()))
    assert harness.apply(RecordTechLeadDispositionAction(disposition=_disposition())).success
    assert harness.host.comments[-1] == disposition_comment(_disposition())
    assert len(harness.host.comments) == 2


def test_posted_disposition_without_receipt_cannot_activate_wait():
    harness = _Harness()
    harness.host.find_issue_comment_receipt = lambda number, *, body: None
    assert not harness.apply(RecordTechLeadDispositionAction(disposition=_disposition())).success
    assert harness.store.load_disposition(issue_number=BLOCKED).phase == "prepared"


def test_successful_remedy_with_failed_required_diagnosis_withholds_success_effects():
    from issue_orchestrator.control.tech_lead_completion_gate import require_investigation_terminal_effect
    harness = _Harness()
    actions = require_investigation_terminal_effect([
        RecordTechLeadDispositionAction(disposition=_disposition()),
        AddCommentAction(number=BLOCKED, comment="required diagnosis"),
    ], focus_issue_number=BLOCKED)
    actions.append(AddCommentAction(number=BLOCKED, comment="success-only"))
    original = harness.host.add_comment
    def write(number, body):
        if body == "required diagnosis":
            raise RuntimeError("diagnosis write failed")
        return original(number, body)
    harness.host.add_comment = write
    results, error = apply_completion_actions_gated(harness, actions, issue_number=BLOCKED)
    assert error is None
    assert evaluate_required_act_level_outcome(results).failed
    assert results[0].success
    assert "success-only" not in harness.host.comments


@pytest.mark.parametrize("payload", [{}, {"number": BLOCKED}, [], None, {"state": "unexpected"}, {"state": []}])
def test_production_malformed_snapshot_preserves_incident_budget(payload):
    from issue_orchestrator.adapters.github.github_adapter import GitHubAdapter
    from issue_orchestrator.control.label_manager import LabelManager
    from issue_orchestrator.control.stuck_sweep import run_stuck_sweep
    from issue_orchestrator.domain.models import OrchestratorState
    from issue_orchestrator.infra.config import Config
    client = MagicMock()
    client.get_issue.side_effect = lambda number, **kwargs: {"state": "open"} if number == TRACKER else payload
    host = GitHubAdapter(repo="owner/repo", http_client=client, verify_writes=False)
    harness = _Harness()
    row = _disposition()
    assert harness.store.transition_disposition(previous=None, disposition=row)
    ledger = TechLeadDispositionLedger(authority=harness.store, issue_state=host.get_issue_state, now=NOW)
    state, config = OrchestratorState(), Config()
    state.recovery_attempts[BLOCKED] = 2
    host.list_issues = lambda *args, **kwargs: []
    run_stuck_sweep(config, state, host, LabelManager(config), 1, dispositions=ledger)
    assert harness.store.load_disposition(issue_number=BLOCKED) == row
    assert state.recovery_attempts == {BLOCKED: 2}


def test_unknown_tracker_read_preserves_admission_without_publishing():
    harness = _Harness()
    row = replace(_disposition(), phase="prepared")
    assert harness.store.transition_disposition(previous=None, disposition=row)
    harness.host.state = "unknown"
    result = harness.apply(RecordTechLeadDispositionAction(disposition=row))
    assert not result.success and result.details["pending_disposition"] is True
    assert harness.store.load_disposition(issue_number=BLOCKED) == row
    assert harness.host.comments == []
