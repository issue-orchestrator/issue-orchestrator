"""Clean decision → production completion planner/applier → host write → receipt.

Only external ports are replaced. The artifact loader, processor, authority
store, planner, applier, terminalization and durable activity store are real.
No live providers, GitHub calls, or production state are involved.
"""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.completion_handler import CompletionHandler
from issue_orchestrator.control.completion_processor import (
    CompletionProcessor,
    GitAdapter,
    LabelAdapter,
    PRAdapter,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.open_issue_corpus import OpenIssueCorpusManager
from issue_orchestrator.control.session_completion import handle_session_completion
from issue_orchestrator.control.tech_lead_reset_retry import (
    TechLeadResetRetryExecutor,
    ResetRetryRunOutcome,
)
from issue_orchestrator.control.tech_lead_run_activity import TechLeadRunActivity
from issue_orchestrator.domain.models import (
    CompletionRecord,
    CompletionOutcome,
    OrchestratorState,
    RequestedAction,
    SessionStatus,
)
from issue_orchestrator.domain.tech_lead_delivery import TechLeadDeliveryStatus
from issue_orchestrator.domain.tech_lead_run_record import TechLeadRunPhase
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.pending_work_claim_store import (
    SqlitePendingWorkClaimStore,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.tech_lead_authority_store import (
    SqliteTechLeadAuthorityStore,
)
from issue_orchestrator.infra.tech_lead_run_record_store import (
    SqliteTechLeadRunRecordStore,
)
from issue_orchestrator.ports import InMemoryEventSink, RepositoryHost
from issue_orchestrator.ports.label_set import LabelSet
from issue_orchestrator.ports.open_issue_corpus_store import (
    InMemoryOpenIssueCorpusStore,
)
from issue_orchestrator.ports.tech_lead_run_artifact_archive import (
    DiscardedTechLeadRunArtifacts,
)
from issue_orchestrator.ports.working_copy import DiffResult
from issue_orchestrator.view_models.tech_lead_activity import read_tech_lead_activity
from tests.callback_endpoint_helpers import ready_callback_endpoint
from tests.conftest import make_provider_availability
from tests.unit.test_completion_action_planner import (
    arm_investigation_session,
    make_tech_lead_config,
    make_tech_lead_session,
    plant_tech_lead_decision_pair,
)

NOW = datetime(2026, 9, 6, 12)


def prepare_run(tmp_path, *, mandated_reset):
    config = make_tech_lead_config(tmp_path)
    config.validation.publish.dirty_check = "off"
    config.tech_lead.authority.reset_retry = "execute"
    session = replace(
        make_tech_lead_session(tmp_path / "worktree"),
        started_at=NOW - timedelta(minutes=20),
        tech_lead_scope=TechLeadLaunchScope(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION
        ),
    )
    arm_investigation_session(config, session)
    plant_tech_lead_decision_pair(session, comment_targets=(1,))
    if mandated_reset:
        path = session.run_dir / "tech-lead-data" / "tech-lead-decision.json"
        decision = json.loads(path.read_text())
        decision["proposed_actions"].append(
            {
                "id": "A2",
                "action_type": "reset_retry",
                "target_number": 1,
                "body": "Worktree recovery required",
                "finding_ids": ["T1"],
            }
        )
        path.write_text(json.dumps(decision))
        (path.parent / "tech-lead-report.md").write_text(
            "# Report\n\nT1 leads to A1 and A2.\n"
        )
    completion = CompletionRecord(
        session_id=session.terminal_id,
        timestamp=NOW.isoformat(),
        outcome=CompletionOutcome.COMPLETED,
        summary="Clean audit completed",
        implementation="Diagnosis delivered through the decision contract",
        requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
    )
    completion_path = session.worktree_path / session.completion_path
    completion_path.parent.mkdir(parents=True, exist_ok=True)
    completion_path.write_text(json.dumps(completion.to_dict()))
    return config, session


@pytest.mark.parametrize("mandated_reset", [False, True])
def test_clean_decision_crosses_real_applier_and_failed_mandate_is_not_delivered(
    tmp_path, mandated_reset
):
    config, session = prepare_run(tmp_path, mandated_reset=mandated_reset)
    authority = SqliteTechLeadAuthorityStore.for_repo(tmp_path)
    store_path = tmp_path / "runs.sqlite"
    store = SqliteTechLeadRunRecordStore(store_path)
    activity = TechLeadRunActivity(
        store, DiscardedTechLeadRunArtifacts(), now=lambda: NOW
    )
    activity.note_started(session)
    (current,) = store.recent(limit=1)
    for index, age in enumerate((8, 4)):
        store.open_run(
            replace(
                current,
                run_id=f"earlier-{index}",
                phase=TechLeadRunPhase.FAILED,
                started_at=NOW - timedelta(hours=age),
                ended_at=NOW - timedelta(hours=age) + timedelta(minutes=10),
            )
        )
    output = FileSystemSessionOutput()
    git = Mock(spec=GitAdapter)
    git.default_branch.return_value = "main"
    git.diff_against_base.return_value = DiffResult(success=True, diff_text="")
    git.has_uncommitted_changes.return_value = False
    pr = Mock(spec=PRAdapter)
    pr.get_prs_for_branch.return_value = []
    pr.get_prs_for_issue.return_value = []
    processor = CompletionProcessor(
        label_adapter=Mock(spec=LabelAdapter),
        pr_adapter=pr,
        git_adapter=git,
        session_output=output,
        config=config,
        tech_lead_authority=authority,
        agent_callback_endpoint=ready_callback_endpoint(),
    )
    processed = processor.process(
        session.worktree_path,
        run_assets=session.run_assets,
        completion_path=session.completion_path,
        issue_number=1,
        issue_title=session.issue.title,
        agent_label="agent:tech-lead",
    )
    assert processed.success and not processed.errors and not processed.is_non_terminal
    git.push.assert_not_called()
    pr.create_pr.assert_not_called()
    pr.add_comment.assert_not_called()
    assert store.inspect_delivery_evidence().last_delivered_at is None

    host = Mock(spec=RepositoryHost)
    host.get_prs_for_branch.return_value = []
    host.get_pr.return_value = None
    host.get_issue.return_value = session.issue
    host.get_issue_labels_fresh.return_value = []
    events = InMemoryEventSink()
    handler = CompletionHandler(
        config=config,
        events=events,
        repository_host=host,
        get_issue_machine_fn=lambda _issue: None,
        get_session_machine_fn=lambda _terminal: None,
        get_review_machine_fn=lambda _pr: None,
        session_output=output,
        tech_lead_authority=authority,
        open_issue_corpus=OpenIssueCorpusManager(
            host,
            InMemoryOpenIssueCorpusStore(),
            is_enabled=lambda: False,
        ),
        provider_availability=make_provider_availability(config),
        tech_lead_run_activity=activity,
    )
    # The host port observes the receipt before the write. A premature
    # COMPLETED receipt at planning time would fail this assertion.
    written = []

    def write_comment(number, body):
        assert store.inspect_delivery_evidence().last_delivered_at is None
        written.append((number, body))
        return "https://example.test/comment/1"

    host.add_comment.side_effect = write_comment
    applier = ActionApplier(
        labels=Mock(spec=LabelSet), sessions=Mock(), events=events, repository_host=host
    )
    reset = Mock(
        return_value=ResetRetryRunOutcome(
            success=False, error="reset host write failed"
        )
    )
    applier.tech_lead_reset_retry = TechLeadResetRetryExecutor(
        events=events,
        label_manager=LabelManager(config),
        read_issue=lambda _n: replace(session.issue, labels=["blocked-failed"]),
        has_active_issue_runtime=lambda _n: False,
        run_reset=reset,
    )
    state = OrchestratorState()
    state.active_sessions = [session]
    handle_session_completion(
        session=session,
        status=SessionStatus.COMPLETED,
        state=state,
        completion_handler=handler,
        action_applier=applier,
        observer=Mock(),
        worktree_manager=None,
        kill_session_fn=lambda _name: None,
        config=config,
        session_output=output,
        events=events,
        pending_work_claims=SqlitePendingWorkClaimStore.for_repo(tmp_path),
        processing_errors=processed.errors,
    )
    restarted = SqliteTechLeadRunRecordStore(store_path)
    (receipt,) = restarted.recent(limit=1)
    diagnoses = [
        (number, body) for number, body in written if "Diagnosis for #1" in body
    ]
    if mandated_reset:
        reset.assert_called_once()
        assert diagnoses == []
        assert receipt.phase is TechLeadRunPhase.FAILED
        assert restarted.inspect_delivery_evidence().last_delivered_at is None
        assert not events.get_events(EventName.SESSION_COMPLETED.value)
    else:
        assert len(diagnoses) == 1 and diagnoses[0][0] == 1
        assert "action A1" in diagnoses[0][1]
        assert receipt.phase is TechLeadRunPhase.COMPLETED
        assert restarted.inspect_delivery_evidence().last_delivered_at == NOW
        assert len(events.get_events(EventName.SESSION_COMPLETED.value)) == 1
    expected = (
        TechLeadDeliveryStatus.STALLED
        if mandated_reset
        else TechLeadDeliveryStatus.OBSERVING
    )
    assert read_tech_lead_activity(restarted, now=NOW).delivery.status is expected
