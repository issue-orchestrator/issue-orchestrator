"""Producer, immutable custody and restart agree on diagnostic size limits."""

import json
from hashlib import sha256

import pytest

from issue_orchestrator.domain.completion_custody import CUSTODY_ARTIFACT_LIMIT
from issue_orchestrator.domain.completion_intake import (
    CompletionIntakeError,
    CompletionValidationFailed,
)
from issue_orchestrator.execution.completion_intake_artifacts import (
    CompletionIntakeArtifacts,
)
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.ports.command_runner import CommandResult
from tests.unit.test_completion_evidence_intake import command, completion, setup


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize(
    "size",
    [CUSTODY_ARTIFACT_LIMIT - 1, CUSTODY_ARTIFACT_LIMIT, CUSTODY_ARTIFACT_LIMIT + 1],
)
def test_configured_output_boundary_keeps_complete_evidence_and_allows_correction(
    tmp_path, stream, size
):
    ledger, run, capability, owner, _, runner = setup(tmp_path)
    output = "x" * size
    runner.run.return_value = CommandResult(
        returncode=0,
        stdout=output if stream == "stdout" else "",
        stderr=output if stream == "stderr" else "",
    )
    first = owner.submit(capability, command(completion()))
    owner.drain()
    attestation = ledger.validation_for_receipt(first.entry_id)
    assert attestation is not None
    oversized = size > CUSTODY_ARTIFACT_LIMIT
    assert attestation.passed is not oversized
    result = json.loads(attestation.result_path.read_bytes())
    assert result["passed"] is not oversized
    log = attestation.result_path.parent / f"{stream}.log"
    if oversized:
        assert "custody_failure" in result
        descriptor = json.loads(log.read_bytes())
        assert (
            descriptor["failure"] == "validation output exceeds custody artifact limit"
        )
        chunks = [
            (log.parent / part["path"]).read_bytes() for part in descriptor["parts"]
        ]
        assert all(len(chunk) <= CUSTODY_ARTIFACT_LIMIT for chunk in chunks)
        assert b"".join(chunks) == output.encode()
        assert descriptor["sha256"] == sha256(output.encode()).hexdigest()
        with pytest.raises(CompletionValidationFailed):
            owner.require_publication_ready(first, run)
    else:
        assert log.read_bytes() == output.encode()
        owner.require_publication_ready(first, run)
    assert ledger.pending_receipts() == ()
    reopened = SqliteIssueRunLedger(tmp_path / "state" / "issue_run_ledger.sqlite")
    assert reopened.validation_for_receipt(first.entry_id) == attestation
    # The original owner's normal submission path performs global repair too.
    runner.run.return_value = CommandResult(returncode=0, stdout="corrected", stderr="")
    corrected = owner.submit(capability, command(completion(), "corrected"))
    owner.close_and_drain(42)
    owner.require_publication_ready(corrected, run)
    assert ledger.validation_for_receipt(first.entry_id) == attestation
    assert reopened.validation_for_receipt(corrected.entry_id).passed
    assert (
        SqliteIssueRunLedger(
            tmp_path / "state" / "issue_run_ledger.sqlite"
        ).pending_receipts()
        == ()
    )


@pytest.mark.parametrize("oversized", ["blob", "envelope"])
def test_writer_refuses_unreadable_artifacts_before_immutable_publication(
    tmp_path, oversized
):
    owner = CompletionIntakeArtifacts(tmp_path / "custody")
    data = b"x" * (CUSTODY_ARTIFACT_LIMIT + 1)
    with pytest.raises(CompletionIntakeError, match="custody limit"):
        owner.write(
            "a" * 64,
            {"detail": data.decode() if oversized == "envelope" else "ok"},
            {"stdout.log": data if oversized == "blob" else b"ok"},
        )
    assert owner.envelopes() == ()
    assert list(owner.root.iterdir()) == []
    owner.write("b" * 64, {"kind": "test"}, {"stdout.log": b"corrected"})
    assert owner.read("b" * 64)["kind"] == "test"


def test_oversized_diagnostic_parts_remain_hash_checked_after_restart(tmp_path):
    ledger, _, capability, owner, _, runner = setup(tmp_path)
    runner.run.return_value = CommandResult(
        returncode=0, stdout="x" * (CUSTODY_ARTIFACT_LIMIT + 1), stderr=""
    )
    receipt = owner.submit(capability, command(completion()))
    owner.drain()
    attestation = ledger.validation_for_receipt(receipt.entry_id)
    assert attestation is not None
    (attestation.result_path.parent / "stdout.log.part-00000001").write_bytes(b"y")
    with pytest.raises(CompletionIntakeError, match="hash mismatch"):
        SqliteIssueRunLedger(tmp_path / "state" / "issue_run_ledger.sqlite")


def test_interrupted_oversized_attestation_repairs_complete_failure_on_reopen(tmp_path):
    import sqlite3

    ledger, run, capability, owner, _, runner = setup(tmp_path)
    runner.run.return_value = CommandResult(
        returncode=0, stdout="x" * (CUSTODY_ARTIFACT_LIMIT + 1), stderr=""
    )
    receipt = owner.submit(capability, command(completion()))
    db = tmp_path / "state" / "issue_run_ledger.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER interrupt_attestation BEFORE INSERT ON completion_validation_attestations BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
        )
    with pytest.raises(CompletionIntakeError, match="unavailable"):
        owner.drain()
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER interrupt_attestation")
    reopened = SqliteIssueRunLedger(db)
    attestation = reopened.validation_for_receipt(receipt.entry_id)
    assert attestation is not None and not attestation.passed
    descriptor = json.loads(
        (attestation.result_path.parent / "stdout.log").read_bytes()
    )
    assert b"".join(
        (attestation.result_path.parent / part["path"]).read_bytes()
        for part in descriptor["parts"]
    ) == b"x" * (CUSTODY_ARTIFACT_LIMIT + 1)
    owner.close_and_drain(42)
    with pytest.raises(CompletionValidationFailed):
        owner.require_publication_ready(receipt, run)
    assert reopened.pending_receipts() == ()
