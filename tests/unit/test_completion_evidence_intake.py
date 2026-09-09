"""Trust and interruption tests through durable owners with external ports substituted."""

import json
import sqlite3
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.completion_intake import CompletionEvidenceIntakeService
from issue_orchestrator.control.completion_intake_validation import (
    ConfiguredCompletionEvidenceValidator,
)
from issue_orchestrator.domain.completion_intake import (
    CompletionIntakeError,
    CompletionParseStatus,
    IntakeClosed,
    IntakeUnauthorized,
    SubmissionConflict,
    SubmitCompletionEvidence,
)
from issue_orchestrator.domain.models import (
    CompletionOutcome,
    CompletionRecord,
    RequestedAction,
)
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.ports.command_runner import CommandResult, CommandRunner
from issue_orchestrator.ports.working_copy import WorkingCopy
from issue_orchestrator.ports.historical_intake import HistoricalIntakeHandler
from issue_orchestrator.ports.background_job import BackgroundJobRunner
from issue_orchestrator.ports.completion_intake import CompletionValidationWorkspace
from tests.unit.test_issue_run_evidence import run_record


def command(raw: bytes = b"{invalid", key: str = "first") -> SubmitCompletionEvidence:
    return SubmitCompletionEvidence(raw, sha256(raw).hexdigest(), key)


def completion() -> bytes:
    return json.dumps(
        CompletionRecord(
            session_id="forged-session",
            timestamp="1999-01-01",
            outcome=CompletionOutcome.COMPLETED,
            summary="done",
            requested_actions=[RequestedAction.PUSH_BRANCH],
            implementation="change",
            problems="none",
            validation_record_path="/attacker/passed.json",
        ).to_dict()
    ).encode()


def setup(tmp_path: Path):
    ledger = SqliteIssueRunLedger(tmp_path / "state" / "issue_run_ledger.sqlite")
    record = run_record(tmp_path)
    record.run.worktree_path.mkdir()
    ledger.record_run(42, record)
    capability = ledger.submission_capability(record.run)
    wc = Mock(spec=WorkingCopy)
    wc.get_head_sha.return_value = "a" * 40
    wc.has_uncommitted_changes.return_value = False
    runner = Mock(spec=CommandRunner)
    runner.run.return_value = CommandResult(
        returncode=0, stdout="passed", stderr="", timed_out=False
    )
    workspace = Mock(spec=CompletionValidationWorkspace)
    workspace.checkout.return_value = record.run.worktree_path
    validator = ConfiguredCompletionEvidenceValidator(
        wc, runner, workspace, command="configured-check", timeout_seconds=30
    )
    return (
        ledger,
        record.run,
        capability,
        CompletionEvidenceIntakeService(
            ledger,
            validator,
            Mock(spec=HistoricalIntakeHandler),
            Mock(spec=BackgroundJobRunner),
        ),
        wc,
        runner,
    )


def test_invalid_then_corrected_preserves_bytes_and_owner_attests(tmp_path):
    ledger, run, capability, owner, _, runner = setup(tmp_path)
    first = owner.submit(capability, command())
    second = owner.submit(capability, command(completion(), "correction"))
    assert first != second
    entries = owner.close_and_drain(42)
    assert [entry.parse_status for entry in entries] == [
        CompletionParseStatus.REJECTED,
        CompletionParseStatus.ACCEPTED,
    ]
    assert entries[0].raw_path.read_bytes() == b"{invalid"
    assert entries[1].raw_path.read_bytes() == completion()
    assert entries[1].run == run
    assert ledger.validation_for_receipt(first.entry_id) is None
    attestation = ledger.validation_for_receipt(second.entry_id)
    assert attestation is not None and attestation.passed
    assert attestation.head_sha == "a" * 40
    assert entries[1].normalized_path is not None
    certified_path = Path(
        json.loads(entries[1].normalized_path.read_bytes())["validation_record_path"]
    )
    assert certified_path.is_relative_to(run.run_dir)
    assert certified_path.read_bytes() == attestation.result_path.read_bytes()
    assert runner.run.call_args.args == ("configured-check",)
    assert ledger.pending_receipts() == ()


def test_auth_key_conflict_and_closure(tmp_path):
    ledger, run, capability, owner, _, _ = setup(tmp_path)
    with pytest.raises(IntakeUnauthorized):
        owner.submit("wrong", command())
    receipt = owner.submit(capability, command())
    assert owner.submit(capability, command()) == receipt
    with pytest.raises(SubmissionConflict):
        owner.submit(capability, command(b"different"))
    owner.close_and_drain(42)
    assert owner.submit(capability, command()) == receipt
    with pytest.raises(IntakeClosed):
        owner.submit(capability, command(key="late"))
    with pytest.raises(IntakeClosed):
        ledger.submission_capability(run)
    with pytest.raises(ValueError, match="hash"):
        replace(command(), content_sha256="a" * 64)


def test_restart_resumes_unprocessed_and_detects_corrupt_authority(tmp_path):
    ledger, run, capability, owner, _, _ = setup(tmp_path)
    receipt = owner.submit(capability, command())
    restarted = SqliteIssueRunLedger(tmp_path / "state" / "issue_run_ledger.sqlite")
    assert restarted.pending_receipts()[0].receipt == receipt
    entry = restarted.entries_for_run(run.identity)[0]
    entry.raw_path.write_bytes(b"replacement")
    with pytest.raises(CompletionIntakeError, match="hash"):
        restarted.entries_for_run(run.identity)
    with pytest.raises(CompletionIntakeError, match="hash"):
        SqliteIssueRunLedger(tmp_path / "state" / "issue_run_ledger.sqlite")


def test_orphan_after_rename_is_repaired_without_losing_receipt(tmp_path):
    ledger, run, capability, owner, _, _ = setup(tmp_path)
    db = tmp_path / "state" / "issue_run_ledger.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER interrupt_entry BEFORE INSERT ON completion_intake_entries BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
        )
    with pytest.raises(CompletionIntakeError, match="unavailable"):
        owner.submit(capability, command())
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER interrupt_entry")
    restarted = SqliteIssueRunLedger(db)
    receipt = restarted.submit(capability, command()).receipt
    assert restarted.entries_for_run(run.identity)[0].receipt == receipt
    assert len(restarted.pending_receipts()) == 1


def test_validator_mismatch_and_failed_validation_never_grant_authority(tmp_path):
    ledger, _, capability, owner, wc, runner = setup(tmp_path)
    receipt = owner.submit(capability, command(completion()))
    runner.run.return_value = CommandResult(
        returncode=1, stdout="failed", stderr="details", timed_out=False
    )
    owner.drain()
    attestation = ledger.validation_for_receipt(receipt.entry_id)
    assert attestation is not None and not attestation.passed
    second = owner.submit(capability, command(completion(), "next"))
    wc.get_head_sha.side_effect = ["a" * 40, "a" * 40, "b" * 40]
    with pytest.raises(CompletionIntakeError, match="changed"):
        owner.drain()
    assert ledger.validation_for_receipt(second.entry_id) is None
    assert len(ledger.pending_receipts()) == 1


def test_closure_rejects_late_submission_while_accepted_validation_drains(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    ledger, _, capability, owner, _, runner = setup(tmp_path)
    accepted = owner.submit(capability, command(completion()))
    validating, finish = Event(), Event()

    def validate(*args, **kwargs):
        validating.set()
        assert finish.wait(5)
        return CommandResult(returncode=0, stdout="passed", stderr="", timed_out=False)

    runner.run.side_effect = validate
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(owner.close_and_drain, 42)
        try:
            assert validating.wait(5)
            with pytest.raises(IntakeClosed):
                owner.submit(capability, command(completion(), "too-late"))
            assert owner.submit(capability, command(completion())) == accepted
        finally:
            finish.set()
        assert future.result(timeout=5)[0].receipt == accepted
    assert ledger.validation_for_receipt(accepted.entry_id).passed


def test_attestation_orphan_repairs_and_never_reexecutes_validation(tmp_path):
    ledger, _, capability, owner, _, runner = setup(tmp_path)
    receipt = owner.submit(capability, command(completion()))
    db = tmp_path / "state" / "issue_run_ledger.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER interrupt_validation BEFORE INSERT ON completion_validation_attestations BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
        )
    with pytest.raises(CompletionIntakeError, match="unavailable"):
        owner.drain()
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER interrupt_validation")
    owner.drain()
    assert runner.run.call_count == 1
    assert ledger.validation_for_receipt(receipt.entry_id).passed


@pytest.mark.parametrize(
    "raw", [b"null", b"[]", b'"text"', b'{"outcome":"completed"}', b"\xff"]
)
def test_rejected_payload_classes_never_hide_corrected_receipt(tmp_path, raw):
    ledger, run, capability, owner, _, _ = setup(tmp_path)
    rejected = owner.submit(capability, command(raw))
    corrected = owner.submit(capability, command(completion(), "corrected"))
    assert owner.receipt_for_run(run) == corrected
    assert ledger.entry_for_receipt(rejected.entry_id).raw_path.read_bytes() == raw
    assert ledger.validation_for_receipt(rejected.entry_id) is None


def test_failed_validator_cannot_be_overruled_by_agent_validation_json(tmp_path):
    _, run, capability, owner, _, runner = setup(tmp_path)
    raw = json.loads(completion())
    raw["validation"] = {"passed": True, "head_sha": "a" * 40}
    receipt = owner.submit(capability, command(json.dumps(raw).encode()))
    runner.run.return_value = CommandResult(
        returncode=1, stdout="failed", stderr="", timed_out=False
    )
    owner.drain()
    assert owner.read_receipt(receipt, run).requests_publication
    with pytest.raises(CompletionIntakeError, match="validation failed"):
        owner.require_publication_ready(receipt, run)


def test_receipt_cannot_be_rebound_to_different_allocated_assets(tmp_path):
    ledger, run, capability, owner, _, _ = setup(tmp_path)
    receipt = owner.submit(capability, command(completion()))
    different = run_record(tmp_path / "other", "run-2")
    ledger.record_run(42, different)
    owner.drain()
    with pytest.raises(CompletionIntakeError, match="allocated run"):
        owner.read_receipt(receipt, different.run)


def test_terminal_release_refused_when_validator_attestation_cannot_commit(tmp_path):
    from tests.unit.test_review_exchange_lifecycle import _FakeSessionManager

    ledger, _, capability, owner, _, _ = setup(tmp_path)
    receipt = owner.submit(capability, command(completion()))
    sessions = _FakeSessionManager({"issue-42", "rework-42"})
    db = tmp_path / "state" / "issue_run_ledger.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER interrupt_terminal BEFORE INSERT ON completion_validation_attestations BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
        )
    with pytest.raises(CompletionIntakeError):
        runtime_owners(completion_intake=owner, pair_registry=None, job_supervisor=None, session_manager=sessions).terminate(42, "test-terminal")
    assert sessions.stopped == []
    assert ledger.pending_receipts()[0].receipt == receipt
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER interrupt_terminal")
    with pytest.raises(IntakeClosed):
        owner.submit(capability, command(completion(), "late"))
    runtime_owners(completion_intake=owner, pair_registry=None, job_supervisor=None, session_manager=sessions).terminate(42, "test-terminal")
    assert sessions.stopped == ["issue-42", "rework-42"]
    assert ledger.validation_for_receipt(receipt.entry_id).passed


def test_capability_file_keeps_launch_commands_free_of_secret_bytes(tmp_path):
    ledger, run, capability, _, _, _ = setup(tmp_path)
    import shlex

    artifact = ledger.submission_capability_file(run)
    export = (
        "ISSUE_ORCHESTRATOR_COMPLETION_CAPABILITY=$(cat "
        + shlex.quote(str(artifact.path))
        + ")"
    )
    assert capability not in export
    assert artifact.path.read_text() == capability
    assert artifact.path.stat().st_mode & 0o777 == 0o600
    assert not artifact.path.is_relative_to(run.worktree_path)


def test_background_restart_consumes_pending_receipt_without_filename_selection(
    tmp_path,
):
    ledger, run, capability, _, wc, runner = setup(tmp_path)
    receipt = ledger.submit(capability, command(completion())).receipt
    # Simulate a restart with no in-memory enqueue and an invalid canonical file.
    (run.worktree_path / "completion.json").write_bytes(b"wrong canonical")
    restarted = SqliteIssueRunLedger(tmp_path / "state" / "issue_run_ledger.sqlite")
    jobs = Mock(spec=BackgroundJobRunner)
    jobs.drain_completed.return_value = []
    callbacks = []
    jobs.submit.side_effect = lambda key, callback: callbacks.append(callback) or True
    workspace = Mock(spec=CompletionValidationWorkspace)
    workspace.checkout.return_value = run.worktree_path
    validator = ConfiguredCompletionEvidenceValidator(
        wc, runner, workspace, command="configured-check", timeout_seconds=30
    )
    service = CompletionEvidenceIntakeService(
        restarted, validator, Mock(spec=HistoricalIntakeHandler), jobs
    )
    service.pump()
    assert len(callbacks) == 1
    callbacks[0]()
    assert restarted.pending_receipts() == ()
    assert restarted.validation_for_receipt(receipt.entry_id).passed


def test_processed_attestation_column_corruption_refuses_restart_and_terminal_drain(
    tmp_path,
):
    ledger, _, capability, owner, _, _ = setup(tmp_path)
    owner.submit(capability, command(completion()))
    owner.drain()
    db = tmp_path / "state" / "issue_run_ledger.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER completion_validation_attestations_immutable")
        conn.execute(
            "UPDATE completion_validation_attestations SET head_sha=?", ("b" * 40,)
        )
    with pytest.raises(CompletionIntakeError, match="fields differ"):
        SqliteIssueRunLedger(db)
    with pytest.raises(CompletionIntakeError, match="fields differ"):
        owner.close_and_drain(42)


def test_failed_attestation_routes_controller_retry_before_any_requested_action(
    tmp_path,
):
    from issue_orchestrator.control.session_controller import SessionController
    from issue_orchestrator.domain.models import SessionStatus
    from issue_orchestrator.domain.session_key import TaskKind
    from issue_orchestrator.execution.session_output_adapter import (
        FileSystemSessionOutput,
    )
    from issue_orchestrator.observation.observation import SessionObservationResult
    from issue_orchestrator.infra.config import Config
    from issue_orchestrator.ports import EventSink
    from issue_orchestrator.ports.agent_callback_endpoint import AgentCallbackEndpoint
    from tests.run_allocation_helpers import make_completion_processor

    ledger, run, capability, owner, wc, runner = setup(tmp_path)
    runner.run.return_value = CommandResult(
        returncode=1, stdout="failed", stderr="test failure", timed_out=False
    )
    receipt = owner.submit(capability, command(completion()))
    output = FileSystemSessionOutput()
    effects = Mock()
    processor = make_completion_processor(
        completion_intake=owner,
        session_output=output,
        agent_callback_endpoint=Mock(spec=AgentCallbackEndpoint),
        label_adapter=effects,
        pr_adapter=effects,
        git_adapter=wc,
        config=Config(),
        event_bus=None,
    )
    controller = SessionController(
        processor,
        Mock(spec=EventSink),
        output,
        wc,
        command_runner=runner,
        validation_cmd="configured-check",
        max_validation_retries=1,
    )
    decision = controller.decide_outcome(
        SessionObservationResult.terminated(runtime_minutes=1),
        run.worktree_path,
        42,
        "test issue",
        run.session_name,
        session_run_assets=run,
        task_kind=TaskKind.CODE,
    )
    assert decision.status is SessionStatus.NEEDS_VALIDATION_RETRY
    assert decision.validation_passed is False
    assert effects.mock_calls == []
    assert ledger.validation_for_receipt(receipt.entry_id).passed is False
    with pytest.raises(IntakeClosed):
        owner.submit(capability, command(completion(), "late"))
    # The existing retry allocator creates a fresh lifetime with its own capability.
    corrected = run_record(tmp_path, "retry-run")
    ledger.record_run(42, corrected)
    corrected_capability = ledger.submission_capability(corrected.run)
    runner.run.return_value = CommandResult(
        returncode=0, stdout="passed", stderr="", timed_out=False
    )
    correction = owner.submit(corrected_capability, command(completion(), "correction"))
    owner.drain()
    owner.require_publication_ready(correction, corrected.run)
    assert corrected_capability != capability


@pytest.mark.parametrize("operation", ["fsync", "rename"])
def test_interrupted_atomic_envelope_does_not_ack_and_exact_retry_preserves_bytes(
    tmp_path, monkeypatch, operation
):
    import os

    ledger, run, capability, owner, _, _ = setup(tmp_path)
    with monkeypatch.context() as fault:
        fault.setattr(
            os, operation, Mock(side_effect=OSError("simulated custody interruption"))
        )
        with pytest.raises(OSError, match="interruption"):
            owner.submit(capability, command())
    assert ledger.entries_for_run(run.identity) == ()
    staged = list(
        (tmp_path / "state" / "completion-intake").glob(".staging-*/raw.json")
    )
    assert len(staged) == 1 and staged[0].read_bytes() == command().raw_bytes
    restarted = SqliteIssueRunLedger(tmp_path / "state" / "issue_run_ledger.sqlite")
    received = restarted.submit(capability, command())
    assert restarted.submit(capability, command()).receipt == received.receipt
    assert received.raw_path.read_bytes() == staged[0].read_bytes()


def test_resume_processing_and_terminal_capture_share_owner_lifetime(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from issue_orchestrator.control.completion_processor import CompletionProcessor
    from issue_orchestrator.execution.session_output_adapter import (
        FileSystemSessionOutput,
    )
    from issue_orchestrator.ports.repository_host import RepositoryHost
    from tests.callback_endpoint_helpers import ready_callback_endpoint
    from tests.run_allocation_helpers import make_completion_processor

    ledger, run, capability, owner, wc, _ = setup(tmp_path)
    raw = json.loads(completion())
    raw["requested_actions"] = []
    receipt = owner.submit(capability, command(json.dumps(raw).encode()))
    started, finish, closing = Event(), Event(), Event()

    def current_branch(_worktree):
        started.set()
        assert finish.wait(5)
        return "feature"

    wc.get_current_branch.side_effect = current_branch
    repository = Mock(spec=RepositoryHost)
    processor: CompletionProcessor = make_completion_processor(
        completion_intake=owner,
        session_output=FileSystemSessionOutput(),
        agent_callback_endpoint=ready_callback_endpoint(),
        label_adapter=repository,
        pr_adapter=repository,
        git_adapter=wc,
        event_bus=None,
    )

    def close():
        closing.set()
        return owner.close_and_drain(42)

    with ThreadPoolExecutor(max_workers=2) as pool:
        processing = pool.submit(
            owner.resume_receipt, capability, receipt, 42, "test", processor
        )
        assert started.wait(5)
        closed = pool.submit(close)
        assert closing.wait(5)
        assert not closed.done()
        finish.set()
        assert processing.result(timeout=5).success
        assert closed.result(timeout=5)
    with pytest.raises(IntakeUnauthorized):
        owner.resume_receipt(capability, receipt, 42, "test", processor)
    assert ledger.entry_for_receipt(receipt.entry_id).raw_path.exists()


@pytest.mark.parametrize("configured", [False, True])
def test_production_bootstrap_requires_validation_even_when_review_gate_disabled(
    tmp_path, monkeypatch, configured
):
    from issue_orchestrator.entrypoints import bootstrap
    from issue_orchestrator.infra.config import Config

    config = Config()
    config.repo_root = tmp_path
    config.review_exchange_require_validation = False
    config.validation.quick.cmd = "configured-check" if configured else None

    class ReachedStartup(Exception):
        pass

    def first_startup_effect():
        raise ReachedStartup

    monkeypatch.setattr(bootstrap, "install_gh_guard", first_startup_effect)
    if configured:
        with pytest.raises(ReachedStartup):
            bootstrap.build_orchestrator(
                config, validated_work_liveness=Mock()
            )
    else:
        with pytest.raises(
            ValueError, match="Completion intake requires validation.quick.cmd"
        ):
            bootstrap.build_orchestrator(
                config, validated_work_liveness=Mock()
            )

from tests.runtime_lifecycle_helpers import runtime_owners



def test_retained_preparation_keeps_exact_role_and_bytes_after_worktree_removal(tmp_path):
    import shutil
    from issue_orchestrator.domain.validated_work_capture import candidate_evidence

    ledger, run, capability, owner, _, _ = setup(tmp_path)
    receipt = owner.submit(capability, command(completion()))
    owner.close_and_drain(42)
    candidate = ledger.prepare_candidate(receipt.entry_id, ledger.recorded_run(run))
    assert candidate is not None
    evidence = candidate_evidence(candidate, issue_number=42, head=candidate.validation.head_sha,
                                  branch_verified=True, captured_at="2026-09-07T00:00:00Z")
    shutil.rmtree(run.worktree_path)
    reopened = SqliteIssueRunLedger(tmp_path / "state" / "issue_run_ledger.sqlite")
    recovered = reopened.prepare_evidence(evidence)
    assert recovered == candidate
    assert recovered.role == ledger.role_for_receipt(receipt.entry_id)
    assert reopened.evidence_receive_sequence(evidence) == recovered.entry.receive_sequence
    assert not run.worktree_path.exists()


@pytest.mark.parametrize("damage", ["issue", "branch", "target", "run", "actions"])
def test_retained_preparation_rejects_identity_substitution(tmp_path, damage):
    from issue_orchestrator.domain.validated_work_capture import candidate_evidence

    ledger, run, capability, owner, _, _ = setup(tmp_path)
    receipt = owner.submit(capability, command(completion()))
    owner.close_and_drain(42)
    candidate = ledger.prepare_candidate(receipt.entry_id, ledger.recorded_run(run))
    evidence = candidate_evidence(candidate, issue_number=42, head=candidate.validation.head_sha,
                                  branch_verified=True, captured_at="2026-09-07T00:00:00Z")
    identity = evidence.identity
    if damage in {"issue", "branch", "target"}:
        changes = {"issue": {"issue_number": 43}, "branch": {"branch_name": "other"},
                   "target": {"validated_head_sha": "b" * 40}}[damage]
        identity = replace(identity, key=replace(identity.key, **changes))
    elif damage == "run":
        identity = replace(identity, run_identity=replace(identity.run_identity, run_id="another-run"))
    else:
        identity = replace(identity, requested_actions=(RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR))
    with pytest.raises(CompletionIntakeError, match="exact owner"):
        ledger.prepare_evidence(replace(evidence, identity=identity))
