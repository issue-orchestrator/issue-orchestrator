"""Durable scheduler ownership tests; no providers or live pool required."""

import json
from pathlib import Path
import subprocess

import pytest

from issue_orchestrator.adapters.condor.contained_validation import CondorContainedValidationRunner
from issue_orchestrator.adapters.condor.tools import CondorTools
from issue_orchestrator.domain.lane_execution import LaneExecutorError
from issue_orchestrator.ports.contained_validation import ContainedValidationCommand, ContainedValidationPending


def _tools() -> CondorTools:
    return CondorTools(
        Path("/tools/condor_submit"), Path("/tools/condor_rm"),
        Path("/tools/condor_q"), Path("/tools/condor_config_val"),
        Path("/tools/condor_status"),
    )


def _command(tmp_path: Path) -> ContainedValidationCommand:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    return ContainedValidationCommand(
        operation_id="0123456789abcdef0123456789abcdef",
        arguments=("/bin/true",), working_directory=workspace,
        evidence_directory=evidence, environment={"PATH": "/usr/bin"},
        timeout_seconds=60,
    )


def _terminal_event() -> str:
    return (
        "000 (001.000.000) 2026-09-07 20:00:00 Job submitted from host: <local>\n...\n"
        "001 (001.000.000) 2026-09-07 20:00:01 Job executing on host: <local>\n...\n"
        "005 (001.000.000) 2026-09-07 20:00:02 Job terminated.\n"
        "\t(1) Normal termination (return value 0)\n...\n"
    )


def _running_event() -> str:
    return (
        "000 (001.000.000) 2026-09-07 20:00:00 Job submitted from host: <local>\n...\n"
        "001 (001.000.000) 2026-09-07 20:00:01 Job executing on host: <local>\n...\n"
    )


def _configure(monkeypatch, history: Path) -> None:
    def read_configuration(self, *query, timeout_seconds=30.0):
        value = "True\n" if query == ("IO_EXECENV_CGROUP_CONTAINMENT",) else f"{history}\n"
        return subprocess.CompletedProcess(query, 0, value, "")
    monkeypatch.setattr(CondorTools, "read_configuration", read_configuration)


def _finish(command: ContainedValidationCommand, history: Path) -> None:
    run = command.evidence_directory / "contained-job"
    (run / "lane.events").write_text(_terminal_event())
    (run / "lane.out").write_text("done")
    (run / "lane.err").write_text("")
    (history / "history.1.0").write_text(
        f'IssueOrchestratorOperationId = "{command.operation_id}"\n'
    )


def test_lost_submit_ack_is_reconciled_by_identity_without_duplicate_submission(monkeypatch, tmp_path):
    tools, command = _tools(), _command(tmp_path)
    history = tmp_path / "history"
    history.mkdir()
    _configure(monkeypatch, history)
    submissions = 0

    def invoke(self, arguments, timeout_seconds=30.0, *, environment=None):
        nonlocal submissions
        if arguments[0] == str(self.submit):
            submissions += 1
            _finish(command, history)
            raise LaneExecutorError("reply lost")
        return subprocess.CompletedProcess(arguments, 0, "[]", "")
    monkeypatch.setattr(CondorTools, "invoke", invoke)

    with pytest.raises(ContainedValidationPending):
        CondorContainedValidationRunner(tools).run(command)
    result = CondorContainedValidationRunner(tools).run(command)
    assert result.returncode == 0 and result.stdout == "done"
    assert submissions == 1
    assert json.loads((command.evidence_directory / "containment.json").read_text())["phase"] == "finished"


def test_restart_resumes_a_known_job_after_coordinator_death(monkeypatch, tmp_path):
    tools, command = _tools(), _command(tmp_path)
    history = tmp_path / "history"
    history.mkdir()
    _configure(monkeypatch, history)
    submissions = 0

    def invoke(self, arguments, timeout_seconds=30.0, *, environment=None):
        nonlocal submissions
        if arguments[0] == str(self.submit):
            submissions += 1
            run = command.evidence_directory / "contained-job"
            (run / "lane.events").write_text(_running_event())
            return subprocess.CompletedProcess(arguments, 0, "1.0", "")
        return subprocess.CompletedProcess(arguments, 0, '[{"ClusterId":1,"ProcId":0}]', "")
    monkeypatch.setattr(CondorTools, "invoke", invoke)
    monkeypatch.setattr("issue_orchestrator.adapters.condor.contained_validation.time.sleep",
                        lambda _: (_ for _ in ()).throw(SystemExit("coordinator killed")))
    with pytest.raises(SystemExit):
        CondorContainedValidationRunner(tools).run(command)
    assert json.loads((command.evidence_directory / "containment.json").read_text())["phase"] == "submitted"

    _finish(command, history)
    monkeypatch.setattr("issue_orchestrator.adapters.condor.contained_validation.time.sleep", lambda _: None)
    monkeypatch.setattr(CondorTools, "invoke",
        lambda self, arguments, timeout_seconds=30.0, environment=None:
            subprocess.CompletedProcess(arguments, 0, "[]", ""))
    assert CondorContainedValidationRunner(tools).run(command).returncode == 0
    assert submissions == 1


def test_terminal_leader_does_not_release_until_scheduler_family_is_absent(monkeypatch, tmp_path):
    tools, command = _tools(), _command(tmp_path)
    history = tmp_path / "history"
    history.mkdir()
    _configure(monkeypatch, history)
    queries = 0

    def invoke(self, arguments, timeout_seconds=30.0, *, environment=None):
        nonlocal queries
        if arguments[0] == str(self.submit):
            _finish(command, history)
            return subprocess.CompletedProcess(arguments, 0, "1.0", "")
        queries += 1
        body = '[{"ClusterId":1,"ProcId":0}]' if queries == 1 else "[]"
        return subprocess.CompletedProcess(arguments, 0, body, "")
    monkeypatch.setattr(CondorTools, "invoke", invoke)
    monkeypatch.setattr("issue_orchestrator.adapters.condor.contained_validation.time.sleep", lambda _: None)
    assert CondorContainedValidationRunner(tools).run(command).returncode == 0
    assert queries == 2


def test_unavailable_scheduler_preserves_ambiguous_reservation(monkeypatch, tmp_path):
    tools, command = _tools(), _command(tmp_path)
    history = tmp_path / "history"
    history.mkdir()
    _configure(monkeypatch, history)

    def lost(self, arguments, timeout_seconds=30.0, *, environment=None):
        if arguments[0] == str(self.submit):
            raise LaneExecutorError("reply lost")
        raise LaneExecutorError("scheduler offline")
    monkeypatch.setattr(CondorTools, "invoke", lost)
    runner = CondorContainedValidationRunner(tools)
    with pytest.raises(ContainedValidationPending):
        runner.run(command)
    with pytest.raises(ContainedValidationPending):
        CondorContainedValidationRunner(tools).run(command)
    assert runner.reserved(command.evidence_directory)
    assert command.working_directory.is_dir()
