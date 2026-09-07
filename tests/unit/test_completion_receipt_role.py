"""Receipt consumers retain allocation-owned role and enforce Tech Lead policy."""

import json
import sqlite3
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.completion_intake import CompletionEvidenceIntakeService
from issue_orchestrator.control.completion_intake_validation import (
    ConfiguredCompletionEvidenceValidator,
)
from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.control.issue_run_allocator import IssueRunAllocationService
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.issue_run_allocation import (
    IssueRunAllocation,
    IssueExchangeRunAllocation,
)
from issue_orchestrator.domain.models import (
    AgentConfig,
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadAssignment,
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.tech_lead_authority_store import (
    SqliteTechLeadAuthorityStore,
)
from issue_orchestrator.ports.background_job import BackgroundJobRunner
from issue_orchestrator.ports.command_runner import CommandResult, CommandRunner
from issue_orchestrator.ports.completion_intake import CompletionValidationWorkspace
from issue_orchestrator.ports.historical_intake import HistoricalIntakeHandler
from issue_orchestrator.ports.working_copy import WorkingCopy
from issue_orchestrator.ports.session_output import SessionOutput
from tests.callback_endpoint_helpers import ready_callback_endpoint
from tests.unit.test_completion_evidence_intake import command
from tests.unit.test_completion_processor import (
    mock_label_adapter,
    mock_pr_adapter,
    mock_git_adapter,
)


@pytest.fixture
def receipt_output():
    filesystem = FileSystemSessionOutput()
    return filesystem, Mock(spec=SessionOutput, wraps=filesystem)


@pytest.fixture
def role_boundary(
    tmp_path, mock_label_adapter, mock_pr_adapter, mock_git_adapter, receipt_output
):
    _, output = receipt_output
    ledger = SqliteIssueRunLedger(tmp_path / "state" / "runs.sqlite")
    config = Config(repo="example/repo")
    config.repo_root = tmp_path
    config.tech_lead_review_agent = "agent:tech-lead"
    config.validation.publish.dirty_check = "off"
    config.review_enabled = False
    config.agents = {
        label: AgentConfig(prompt_path=tmp_path / "prompt.md")
        for label in ("agent:tech-lead", "agent:coder")
    }
    allocator = IssueRunAllocationService(output, ledger, configuration=config)
    working_copy = Mock(spec=WorkingCopy)
    working_copy.get_head_sha.return_value = "a" * 40
    working_copy.has_uncommitted_changes.return_value = False
    runner = Mock(spec=CommandRunner)
    runner.run.return_value = CommandResult(returncode=0, stdout="validated", stderr="")
    workspace = Mock(spec=CompletionValidationWorkspace)
    workspace.checkout.side_effect = lambda run, head, entry: run.worktree_path
    validator = ConfiguredCompletionEvidenceValidator(
        working_copy, runner, workspace, command="configured-check", timeout_seconds=30
    )
    owner = CompletionEvidenceIntakeService(
        ledger,
        validator,
        Mock(spec=HistoricalIntakeHandler),
        Mock(spec=BackgroundJobRunner),
    )
    authority = SqliteTechLeadAuthorityStore.for_repo(tmp_path)
    processor = CompletionProcessor(
        label_adapter=mock_label_adapter,
        pr_adapter=mock_pr_adapter,
        git_adapter=mock_git_adapter,
        session_output=output,
        agent_callback_endpoint=ready_callback_endpoint(),
        issue_run_allocator=allocator,
        completion_intake=owner,
        config=config,
        tech_lead_authority=authority,
    )
    mock_git_adapter.default_branch.return_value = "main"
    return (
        ledger,
        allocator,
        owner,
        processor,
        authority,
        config,
        (mock_label_adapter, mock_pr_adapter, mock_git_adapter),
    )


def allocate(tmp_path, allocator, *, label="agent:tech-lead", exchange=False):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    # Production Tech Lead launches share the CODE slot. Completion policy is
    # independently selected by the allocation owner from configured identity.
    key = SessionKey(FakeIssueKey("42", "example/repo"), TaskKind.CODE)
    if exchange:
        return allocator.allocate_exchange(
            IssueExchangeRunAllocation(worktree, 42, key, "issue-42", label)
        ).session_run
    return allocator.allocate(
        IssueRunAllocation(worktree, "issue-42", 42, key, label, "test")
    )


def submit(owner, ledger, run):
    record = CompletionRecord(
        session_id="forged-coder",
        timestamp="2026-09-07",
        outcome=CompletionOutcome.COMPLETED,
        summary="done",
        implementation="done",
        problems="none",
        comment_body="done",
        requested_actions=[RequestedAction.POST_COMMENT],
    )
    return owner.submit(
        ledger.submission_capability(run),
        command(json.dumps(record.to_dict()).encode()),
    )


def arm(authority, run, *, valid):
    authority.record(
        run_id=run.run_id,
        session_name=run.session_name,
        authority=TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.BATCH_REVIEW, anchor_issue_number=42
        ),
    )
    data = run.run_dir / "tech-lead-data"
    TechLeadAssignment(
        flavor=TechLeadSessionFlavor.BATCH_REVIEW
        if valid
        else TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        focus_issue_number=None if valid else 999,
    ).write(data / "tech-lead-assignment.json")
    (data / "tech-lead-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "Clean audit.",
                "findings": [],
                "proposed_actions": [],
            }
        )
    )
    (data / "tech-lead-report.md").write_text("# Report\n\nNothing found.\n")


@pytest.mark.parametrize("path", ["resume", "direct"])
@pytest.mark.parametrize("authority_state", ["missing", "tampered", "valid"])
def test_recorded_tech_lead_gate_precedes_every_effect(
    tmp_path, role_boundary, path, authority_state
):
    ledger, allocator, owner, processor, authority, _, effects = role_boundary
    run = allocate(tmp_path, allocator)
    receipt = submit(owner, ledger, run)
    if authority_state != "missing":
        arm(authority, run, valid=authority_state == "valid")
    # An editable manifest and candidate identity cannot downgrade the role.
    manifest = json.loads(run.manifest_path.read_text())
    manifest["agent_label"] = "agent:coder"
    run.manifest_path.write_text(json.dumps(manifest))
    context = owner.processing_context(receipt, run)
    assert context.artifact.path.name == "completion.json"
    assert context.role.task is TaskKind.TECH_LEAD
    assert context.role.agent_label == "agent:tech-lead"
    if path == "resume":
        result = owner.resume_receipt(
            ledger.submission_capability(run), receipt, 42, "Review", processor
        )
    else:
        result = processor.process(
            run.worktree_path, 42, "Review", run_assets=run, intake_receipt=receipt
        )
    assert result.success is (authority_state == "valid")
    if authority_state != "valid":
        assert any("tech_lead_authority:" in error for error in result.errors)
        assert all(not port.mock_calls for port in effects)
    else:
        # Valid Tech Lead policy intentionally suppresses completion comments.
        effects[1].add_comment.assert_not_called()


@pytest.mark.parametrize("exchange", [False, True])
def test_restart_preserves_role_for_both_allocation_paths(
    tmp_path, role_boundary, exchange
):
    ledger, allocator, owner, _, _, _, _ = role_boundary
    run = allocate(tmp_path, allocator, exchange=exchange)
    receipt = submit(owner, ledger, run)
    reopened = SqliteIssueRunLedger(tmp_path / "state" / "runs.sqlite")
    assert (
        reopened.role_for_receipt(receipt.entry_id)
        == owner.processing_context(receipt, run).role
    )
    (record,) = reopened.recorded_runs(42)
    assert record.session_key.task is TaskKind.CODE
    assert record.completion_task is TaskKind.TECH_LEAD


@pytest.mark.parametrize(
    "failure",
    [
        "caller-role",
        "wrong-issue",
        "config-removed",
        "config-renamed",
        "missing-role",
        "invalid-role",
        "missing-task",
        "invalid-task",
        "mismatched-task",
    ],
)
def test_role_failures_precede_effects(tmp_path, role_boundary, failure):
    ledger, allocator, owner, processor, authority, config, effects = role_boundary
    run = allocate(tmp_path, allocator)
    receipt = submit(owner, ledger, run)
    arm(authority, run, valid=True)
    if failure.startswith("config-"):
        config.tech_lead_review_agent = (
            None if failure == "config-removed" else "agent:different"
        )
    if failure in {"missing-role", "invalid-role"}:
        with sqlite3.connect(tmp_path / "state" / "runs.sqlite") as conn:
            conn.execute(
                "UPDATE issue_runs SET agent_label=?",
                (None if failure == "missing-role" else "bad",),
            )
    if failure in {"missing-task", "invalid-task", "mismatched-task"}:
        with sqlite3.connect(tmp_path / "state" / "runs.sqlite") as conn:
            conn.execute(
                "UPDATE issue_runs SET completion_task=?",
                (
                    {
                        "missing-task": None,
                        "invalid-task": "unrecognized",
                        "mismatched-task": "code",
                    }[failure],
                ),
            )
    result = processor.process(
        run.worktree_path,
        99 if failure == "wrong-issue" else 42,
        "Review",
        run_assets=run,
        intake_receipt=receipt,
        agent_label="agent:coder" if failure == "caller-role" else None,
    )
    assert not result.success
    assert all(not port.mock_calls for port in effects)


def test_recorded_coder_receipt_still_processes(tmp_path, role_boundary):
    ledger, allocator, owner, processor, _, _, effects = role_boundary
    run = allocate(tmp_path, allocator, label="agent:coder")
    receipt = submit(owner, ledger, run)
    result = owner.resume_receipt(
        ledger.submission_capability(run), receipt, 42, "Code", processor
    )
    assert result.success
    effects[1].add_comment.assert_called_once()


def test_new_allocation_captures_current_configuration_without_rewriting_old_role(
    tmp_path, role_boundary
):
    ledger, allocator, owner, _, _, config, _ = role_boundary
    config.tech_lead_review_agent = "agent:new-tech-lead"
    run = allocate(tmp_path, allocator, label="agent:new-tech-lead")
    receipt = submit(owner, ledger, run)
    assert owner.processing_context(receipt, run).role.task is TaskKind.TECH_LEAD
    config.tech_lead_review_agent = None
    assert ledger.role_for_receipt(receipt.entry_id).task is TaskKind.TECH_LEAD


def test_legacy_schema_upgrade_keeps_unknown_roles_and_original_facts(
    tmp_path, role_boundary
):
    from issue_orchestrator.domain.completion_intake import CompletionIntakeError

    ledger, allocator, owner, _, _, _, effects = role_boundary
    run = allocate(tmp_path, allocator)
    receipt = submit(owner, ledger, run)
    db = tmp_path / "state" / "runs.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE issue_runs DROP COLUMN agent_label")
        conn.execute("ALTER TABLE issue_runs DROP COLUMN completion_task")
        original = conn.execute("SELECT * FROM issue_runs").fetchall()
    reopened = SqliteIssueRunLedger(db)
    with sqlite3.connect(db) as conn:
        assert [
            row[:-2] for row in conn.execute("SELECT * FROM issue_runs").fetchall()
        ] == original
    (record,) = reopened.recorded_runs(42)
    assert record.agent_label is None and record.completion_task is None
    with pytest.raises(CompletionIntakeError, match="role is missing"):
        reopened.role_for_receipt(receipt.entry_id)
    assert all(not port.mock_calls for port in effects)


@pytest.mark.parametrize(
    ("label", "authority_state", "success", "comments"),
    [
        ("agent:tech-lead", "missing", False, 0),
        ("agent:tech-lead", "valid", True, 0),
        ("agent:coder", "missing", True, 1),
    ],
)
def test_settings_change_cannot_reselect_in_flight_processing_role(
    tmp_path, role_boundary, receipt_output, label, authority_state, success, comments
):
    from threading import Event
    from issue_orchestrator.infra.settings_schema import (
        ReviewSettings,
        apply_to,
        from_config,
    )
    from tests.unit.threading_helpers import join_or_fail, run_in_thread, wait_for_event

    ledger, allocator, owner, processor, authority, config, effects = role_boundary
    run = allocate(tmp_path, allocator, label=label)
    receipt = submit(owner, ledger, run)
    if authority_state == "valid":
        arm(authority, run, valid=True)
    entered, continue_processing = Event(), Event()
    filesystem, output_port = receipt_output

    def attach_after_settings_change(run_dir):
        entered.set()
        wait_for_event(continue_processing, 5, label="settings applied")
        return filesystem.attach_claude_log(run_dir)

    output_port.attach_claude_log.side_effect = attach_after_settings_change
    capability = ledger.submission_capability(run)
    thread, result = run_in_thread(
        owner.resume_receipt, capability, receipt, 42, "Review", processor
    )
    try:
        wait_for_event(entered, 5, label="processing reached SessionOutput")
        tabs = from_config(config)
        tabs["review"] = ReviewSettings.model_validate(
            {
                **tabs["review"].model_dump(),
                "tech_lead_agent": "agent:coder",
            }
        )
        apply_to(tabs, config)
        assert config.tech_lead_review_agent == "agent:coder"
    finally:
        continue_processing.set()
        join_or_fail(thread, 5, label="receipt processing")
    completed = result.unwrap()
    assert completed.success is success
    assert effects[1].add_comment.call_count == comments
    if not success:
        assert any("missing_authority" in error for error in completed.errors)
        assert all(not port.mock_calls for port in effects)


@pytest.mark.parametrize("authority_state", ["missing", "valid"])
def test_reviewer_approval_gate_keeps_registered_role_after_settings_change(
    tmp_path, role_boundary, authority_state
):
    from issue_orchestrator.control.completion_record_validation import (
        CompletionRecordValidator,
    )
    from issue_orchestrator.control.tech_lead_approval_gate import (
        build_tech_lead_decision_approval_gate,
    )
    from issue_orchestrator.infra.settings_schema import (
        ReviewSettings,
        apply_to,
        from_config,
    )

    ledger, allocator, owner, _, authority, config, effects = role_boundary
    run = allocate(tmp_path, allocator)
    receipt = submit(owner, ledger, run)
    if authority_state == "valid":
        arm(authority, run, valid=True)
    validator = CompletionRecordValidator(config=config, git_adapter=effects[2])
    policy = validator.resolve_processing_policy(
        owner.processing_context(receipt, run), 42, None, None
    )
    tabs = from_config(config)
    tabs["review"] = ReviewSettings.model_validate(
        {**tabs["review"].model_dump(), "tech_lead_agent": "agent:coder"}
    )
    apply_to(tabs, config)
    gate = build_tech_lead_decision_approval_gate(
        config,
        processing_policy=policy,
        tech_lead_authority=authority,
        run_dir=run.run_dir,
        run_id=run.run_id,
        session_name=run.session_name,
    )
    assert gate is not None
    reason = gate.rejection_reason()
    if authority_state == "valid":
        assert reason is None
    else:
        assert "authority missing" in reason
