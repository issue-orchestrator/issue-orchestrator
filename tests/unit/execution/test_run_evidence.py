from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

import pytest

from issue_orchestrator.domain.run_manifest import RunManifest
from issue_orchestrator.execution.run_evidence import (
    RunEvidenceRecorder,
    recorded_junit_xml_paths,
    recorded_validation_junit_xml_paths,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.ports.session_output import ValidationRecord


def _validation_record(
    *,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> ValidationRecord:
    return ValidationRecord(
        schema_version=1,
        suite="publish_gate",
        head_sha="a" * 40,
        passed=True,
        exit_code=0,
        command="pytest",
        started_at="2026-05-07T00:00:00Z",
        ended_at="2026-05-07T00:00:01Z",
        stdout_path=str(stdout_path) if stdout_path else None,
        stderr_path=str(stderr_path) if stderr_path else None,
    )


def _write_junit_xml(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="validation" tests="1">
  <testcase classname="tests.e2e.test_smoke" name="test_smoke" time="0.01" />
</testsuite>
""",
        encoding="utf-8",
    )


def _junit_artifact_key(path: Path) -> str:
    suffix = sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"validation_junit_xml_{suffix}"


def test_run_evidence_records_validation_junit_artifacts(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    session_output = FileSystemSessionOutput()
    run = session_output.start_run(worktree, "coding-1")
    stdout_path, stderr_path = session_output.write_validation_output(
        run.run_dir,
        stdout="ok",
        stderr="",
    )
    record = _validation_record(stdout_path=stdout_path, stderr_path=stderr_path)
    record_path = session_output.write_validation_record(run.run_dir, record)
    junit_path = worktree / "reports" / "junit.xml"
    _write_junit_xml(junit_path)

    RunEvidenceRecorder(session_output).record_validation_evidence(
        run_dir=run.run_dir,
        worktree=worktree,
        record=record,
        record_path=record_path,
        junit_xml_paths=("reports/*.xml",),
    )

    manifest = session_output.read_manifest(run.run_dir)
    assert manifest is not None
    artifacts = manifest["artifacts"]
    assert manifest["validation_record_path"] == str(record_path)
    assert manifest["validation_stdout"] == str(stdout_path)
    assert manifest["validation_stderr"] == str(stderr_path)
    assert artifacts[_junit_artifact_key(junit_path)] == {
        "kind": "junit_xml",
        "path": str(junit_path.resolve()),
        "content_type": "application/xml",
    }
    assert recorded_junit_xml_paths(run.run_dir) == (str(junit_path.resolve()),)
    assert recorded_validation_junit_xml_paths(run.run_dir) == (
        str(junit_path.resolve()),
    )
    assert RunManifest.load(run.run_dir).junit_xml_paths(
        key_prefix="validation_junit_xml_"
    ) == (str(junit_path.resolve()),)


def test_run_evidence_ignores_junit_older_than_validation_record(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    session_output = FileSystemSessionOutput()
    run = session_output.start_run(worktree, "coding-1")
    record = _validation_record()
    junit_path = worktree / "reports" / "stale.xml"
    _write_junit_xml(junit_path)
    stale_ns = 1_767_744_000_000_000_000  # 2026-01-07T00:00:00Z
    os.utime(junit_path, ns=(stale_ns, stale_ns))

    RunEvidenceRecorder(session_output).record_validation_evidence(
        run_dir=run.run_dir,
        worktree=worktree,
        record=record,
        junit_xml_paths=("reports/*.xml",),
    )

    assert recorded_validation_junit_xml_paths(run.run_dir) == ()


def test_run_evidence_preserves_unrelated_artifacts(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    session_output = FileSystemSessionOutput()
    run = session_output.start_run(worktree, "coding-1")
    diagnostic_path = run.run_dir / "diagnostic.json"
    diagnostic_path.write_text("{}", encoding="utf-8")
    manifest = session_output.read_manifest(run.run_dir)
    assert manifest is not None
    artifacts = dict(manifest["artifacts"])
    artifacts.update(
        {
            "diagnostic": {
                "kind": "diagnostic",
                "path": str(diagnostic_path),
                "content_type": "application/json",
            },
            "validation_junit_xml_1": {
                "kind": "junit_xml",
                "path": str(worktree / "reports" / "stale.xml"),
                "content_type": "application/xml",
            },
        }
    )
    session_output.update_manifest(
        run.run_dir,
        {"artifacts": artifacts},
    )
    junit_path = worktree / "reports" / "fresh.xml"
    _write_junit_xml(junit_path)

    RunEvidenceRecorder(session_output).record_validation_evidence(
        run_dir=run.run_dir,
        worktree=worktree,
        record=None,
        junit_xml_paths=("reports/*.xml",),
    )

    manifest = session_output.read_manifest(run.run_dir)
    assert manifest is not None
    artifacts = manifest["artifacts"]
    assert artifacts["diagnostic"]["path"] == str(diagnostic_path)
    assert artifacts[_junit_artifact_key(junit_path)]["path"] == str(
        junit_path.resolve()
    )


def test_run_evidence_tolerates_missing_configured_junit(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    session_output = FileSystemSessionOutput()
    run = session_output.start_run(worktree, "coding-1")

    RunEvidenceRecorder(session_output).record_validation_evidence(
        run_dir=run.run_dir,
        worktree=worktree,
        record=None,
        junit_xml_paths=("reports/*.xml",),
    )

    assert recorded_validation_junit_xml_paths(run.run_dir) == ()


def test_run_evidence_resolves_relative_validation_record_path(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    session_output = FileSystemSessionOutput()
    run = session_output.start_run(worktree, "coding-1")
    record_path = run.run_dir / "validation-record.json"
    record_path.write_text(json.dumps(_validation_record().to_dict()), encoding="utf-8")
    relative_record_path = record_path.relative_to(worktree)

    RunEvidenceRecorder(session_output).record_validation_evidence(
        run_dir=run.run_dir,
        worktree=worktree,
        record=None,
        record_path=relative_record_path,
    )

    manifest = session_output.read_manifest(run.run_dir)
    assert manifest is not None
    assert manifest["validation_record_path"] == str(record_path)


def test_recorded_junit_paths_fail_fast_on_malformed_manifest(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text("{", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        recorded_validation_junit_xml_paths(run_dir)
