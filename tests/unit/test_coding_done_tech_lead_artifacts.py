"""coding-done enforces the tech-lead artifact contract in-session (#7040).

The non-exchange tech-lead flavors (failure investigation, health review)
complete straight through ``coding-done``. Before this, nothing on that path
looked at the decision artifact pair, so the first and only judgement came
from the orchestrator after the agent had exited — too late to fix a title
seven characters too long, and fatal to the whole investigation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.domain.tech_lead_session import (
    TechLeadAssignment,
    TechLeadSessionFlavor,
)
from issue_orchestrator.entrypoints.cli_tools.coding_done import main as coding_done_main
from issue_orchestrator.infra.env import ENV_PREFIX

REJECTION_BANNER = "TECH-LEAD ARTIFACT CONTRACT VIOLATION"


def _decision(*, finding_title: str) -> dict:
    return {
        "schema_version": 1,
        "summary": "One systemic pattern found.",
        "findings": [
            {
                "id": "T1",
                "title": finding_title,
                "classification": "systemic",
                "evidence": ["board-snapshot.json"],
            }
        ],
        "proposed_actions": [
            {
                "id": "A1",
                "action_type": "post_comment",
                "target_number": 42,
                "body": "Diagnosis: the lease renewer stalled.",
                "finding_ids": ["T1"],
            }
        ],
    }


def _write_manifest(
    run_dir: Path, *, worktree: Path, session_name: str, run_id: str = "run-7"
) -> None:
    """The owner-injected run manifest ``coding-done`` verifies its binding against."""
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": session_name,
                "run_id": run_id,
                "started_at": "2026-08-08T00:00:00Z",
                "worktree": str(worktree),
                "run_dir": str(run_dir),
                "log_path": str(run_dir / "terminal-recording.jsonl"),
            }
        )
    )


@pytest.fixture
def tech_lead_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A managed tech-lead session, cwd'd into its worktree. Returns run_dir."""
    worktree = tmp_path / "scratch"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-7__issue-42"
    data_dir = run_dir / "tech-lead-data"
    data_dir.mkdir(parents=True)
    _write_manifest(run_dir, worktree=worktree, session_name="issue-42")
    TechLeadAssignment(
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        focus_issue_number=42,
        focus_reason="stranded validated work",
    ).write(data_dir / "tech-lead-assignment.json")
    monkeypatch.chdir(worktree)
    for name, value in {
        f"{ENV_PREFIX}SESSION_ID": "issue-42",
        f"{ENV_PREFIX}RUN_DIR": str(run_dir),
        f"{ENV_PREFIX}WORKTREE": str(worktree),
        f"{ENV_PREFIX}COMPLETION_PATH": "completion.json",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(f"{ENV_PREFIX}CONFIG_PATH", raising=False)
    monkeypatch.delenv(f"{ENV_PREFIX}CONFIG_NAME", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_CONFIG_PATH", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_CONFIG_NAME", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_SESSION_ID", raising=False)
    return run_dir


def _write_pair(run_dir: Path, payload: dict, report: str) -> None:
    data_dir = run_dir / "tech-lead-data"
    (data_dir / "tech-lead-decision.json").write_text(json.dumps(payload))
    (data_dir / "tech-lead-report.md").write_text(report)


def _complete() -> None:
    with patch(
        "sys.argv",
        [
            "coding-done",
            "completed",
            "--implementation",
            "Investigated #42",
            "--problems",
            "None",
        ],
    ):
        coding_done_main()


def test_over_long_finding_title_blocks_completion(
    tech_lead_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pair(
        tech_lead_run,
        _decision(finding_title="x" * 338),
        "# Report\n\nT1 leads to A1.\n",
    )

    with pytest.raises(SystemExit) as exit_info:
        _complete()

    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert REJECTION_BANNER in out
    assert "finding T1 title exceeds 300 characters (338)" in out
    # The agent is told exactly which file to edit, and what happens if it
    # exits anyway — the whole reason the check moved in-session.
    assert "tech-lead-decision.json" in out
    assert "No completion was recorded" in out
    assert not (Path.cwd() / "completion.json").exists()


def test_missing_artifact_pair_blocks_completion(
    tech_lead_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        _complete()

    assert exit_info.value.code == 1
    assert "decision missing or empty" in capsys.readouterr().out


@pytest.mark.parametrize("flavor", list(TechLeadSessionFlavor))
def test_valid_pair_completes(
    tech_lead_run: Path, capsys: pytest.CaptureFixture[str], flavor: TechLeadSessionFlavor
) -> None:
    TechLeadAssignment(
        flavor=flavor,
        focus_issue_number=42 if flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION else None,
    ).write(tech_lead_run / "tech-lead-data" / "tech-lead-assignment.json")
    _write_pair(
        tech_lead_run,
        _decision(finding_title="Lease renewer stalled for 20 minutes"),
        "# Report\n\nT1 leads to A1.\n",
    )

    _complete()

    out = capsys.readouterr().out
    assert REJECTION_BANNER not in out
    assert (Path.cwd() / "completion.json").exists()


def test_blocked_status_is_not_gated_on_artifacts(
    tech_lead_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An honest 'I could not do this' must stay reachable.

    The orchestrator's own contract check applies only to a COMPLETED
    outcome; gating the escape hatch too would trap a tech-lead agent that
    genuinely cannot produce a decision.
    """
    with patch(
        "sys.argv",
        [
            "coding-done",
            "blocked",
            "--reason",
            "Evidence worktree was already deleted",
            "--attempted",
            "Read the run dirs named in the evidence map",
        ],
    ):
        coding_done_main()

    out = capsys.readouterr().out
    assert REJECTION_BANNER not in out
    assert (Path.cwd() / "completion.json").exists()


def test_ordinary_coding_session_is_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No launch-time assignment means no artifact pair is owed."""
    worktree = tmp_path / "work"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-7__issue-7"
    run_dir.mkdir(parents=True)
    _write_manifest(run_dir, worktree=worktree, session_name="issue-7")
    monkeypatch.chdir(worktree)
    monkeypatch.setenv(f"{ENV_PREFIX}SESSION_ID", "issue-7")
    monkeypatch.setenv(f"{ENV_PREFIX}RUN_DIR", str(run_dir))
    monkeypatch.setenv(f"{ENV_PREFIX}WORKTREE", str(worktree))
    monkeypatch.setenv(f"{ENV_PREFIX}COMPLETION_PATH", "completion.json")
    for name in (
        f"{ENV_PREFIX}CONFIG_PATH",
        f"{ENV_PREFIX}CONFIG_NAME",
        "ORCHESTRATOR_CONFIG_PATH",
        "ORCHESTRATOR_CONFIG_NAME",
        "ORCHESTRATOR_SESSION_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    with patch(
        "sys.argv",
        ["coding-done", "completed", "--implementation", "Fixed it", "--problems", "None"],
    ):
        coding_done_main()

    assert REJECTION_BANNER not in capsys.readouterr().out
    assert (Path.cwd() / "completion.json").exists()


def test_standalone_invocation_without_run_dir_is_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX) or name.startswith("ORCHESTRATOR_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(f"{ENV_PREFIX}COMPLETION_PATH", "completion.json")

    with patch(
        "sys.argv",
        ["coding-done", "completed", "--implementation", "Local run", "--problems", "None"],
    ):
        coding_done_main()

    assert REJECTION_BANNER not in capsys.readouterr().out


# --- the run binding a managed completion must prove (#7040 F4) ------------
#
# Deriving the run directory from the raw env var made a missing, stale, or
# cross-session injection silently mean "no run" — and therefore "no tech-lead
# artifact check" for any repo without a quick-validation command.


def _expect_managed_rejection(capsys: pytest.CaptureFixture[str], expected: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        _complete()
    assert exit_info.value.code == 1
    assert expected in capsys.readouterr().err
    assert not (Path.cwd() / "completion.json").exists()


def test_managed_completion_without_a_run_dir_is_rejected(
    tech_lead_run: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pair(tech_lead_run, _decision(finding_title="Fine"), "# Report\n\nT1, A1.\n")
    monkeypatch.delenv(f"{ENV_PREFIX}RUN_DIR")

    _expect_managed_rejection(capsys, "RUN_DIR is required")


def test_managed_completion_with_a_nonexistent_run_dir_is_rejected(
    tech_lead_run: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pair(tech_lead_run, _decision(finding_title="Fine"), "# Report\n\nT1, A1.\n")
    monkeypatch.setenv(f"{ENV_PREFIX}RUN_DIR", str(tech_lead_run.parent / "gone"))

    _expect_managed_rejection(capsys, "RUN_DIR does not exist")


def test_managed_completion_with_another_worktrees_run_is_rejected(
    tech_lead_run: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run dir belonging to a different worktree is not this session's run."""
    other_worktree = tmp_path / "other"
    other_run = other_worktree / ".issue-orchestrator" / "sessions" / "run-7__issue-42"
    other_run.mkdir(parents=True)
    _write_manifest(other_run, worktree=other_worktree, session_name="issue-42")
    monkeypatch.setenv(f"{ENV_PREFIX}RUN_DIR", str(other_run))

    _expect_managed_rejection(capsys, "belongs to worktree")


def test_managed_completion_with_another_sessions_run_is_rejected(
    tech_lead_run: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    worktree = tech_lead_run.parent.parent.parent
    other_run = tech_lead_run.parent / "run-7__issue-99"
    other_run.mkdir(parents=True)
    _write_manifest(other_run, worktree=worktree, session_name="issue-99")
    monkeypatch.setenv(f"{ENV_PREFIX}RUN_DIR", str(other_run))

    _expect_managed_rejection(capsys, "belongs to 'issue-99'")


def test_the_artifact_check_runs_without_any_quick_validation_configured(
    tech_lead_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gap F4 names: no ``validation.quick.cmd``, so nothing else ever
    resolved the typed run assets — and the contract went unchecked."""
    _write_pair(tech_lead_run, _decision(finding_title="x" * 338), "# Report\n\nT1, A1.\n")

    with pytest.raises(SystemExit) as exit_info:
        _complete()

    assert exit_info.value.code == 1
    assert REJECTION_BANNER in capsys.readouterr().out


def test_a_managed_blocked_report_needs_no_run_binding(
    tech_lead_run: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An honest failure report stays reachable even with broken run wiring."""
    monkeypatch.delenv(f"{ENV_PREFIX}RUN_DIR")

    with patch(
        "sys.argv",
        ["coding-done", "blocked", "--reason", "No evidence", "--attempted", "Read logs"],
    ):
        coding_done_main()

    assert REJECTION_BANNER not in capsys.readouterr().out
    assert (Path.cwd() / "completion.json").exists()


def test_rejected_pair_can_be_corrected_in_the_same_session(
    tech_lead_run: Path,
) -> None:
    _write_pair(tech_lead_run, _decision(finding_title="x" * 338), "T1 A1")
    rejected = (tech_lead_run / "tech-lead-data" / "tech-lead-decision.json").read_bytes()
    with pytest.raises(SystemExit):
        _complete()
    assert not (Path.cwd() / ".agent-done-marker").exists()
    assert (tech_lead_run / "tech-lead-data" / "tech-lead-decision.json").read_bytes() == rejected
    _write_pair(tech_lead_run, _decision(finding_title="Lease stalled"), "T1 A1")
    _complete()
    record = json.loads((Path.cwd() / "completion.json").read_text())
    assert record["outcome"] == "completed"
    assert (Path.cwd() / ".agent-done-marker").exists()


def test_pair_is_rechecked_if_validation_changes_it(
    tech_lead_run: Path,
) -> None:
    from issue_orchestrator.control.validation import AgentGateResult

    _write_pair(tech_lead_run, _decision(finding_title="Lease stalled"), "T1 A1")

    def validate(*args, **kwargs):
        _write_pair(tech_lead_run, _decision(finding_title="x" * 338), "T1 A1")
        return AgentGateResult(passed=True, reason="passed")

    with patch("issue_orchestrator.entrypoints.cli_tools.coding_done.load_validation_cmd", return_value=("true", 30)), patch(
        "issue_orchestrator.entrypoints.cli_tools.coding_done.run_validation", side_effect=validate
    ), pytest.raises(SystemExit):
        _complete()
    assert not (Path.cwd() / "completion.json").exists()
    assert not (Path.cwd() / ".agent-done-marker").exists()


def test_safe_main_rejects_invalid_utf8_without_ending_the_session(
    tech_lead_run: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from issue_orchestrator.entrypoints.cli_tools.coding_done import safe_main

    _write_pair(tech_lead_run, _decision(finding_title="Lease stalled"), "T1 A1")
    decision_path = tech_lead_run / "tech-lead-data" / "tech-lead-decision.json"
    decision_path.write_bytes(b"\xff")
    with patch("sys.argv", ["coding-done", "completed", "--implementation", "Diagnosis", "--problems", "None"]):
        with pytest.raises(SystemExit) as result:
            safe_main()
        assert result.value.code == 1
        assert REJECTION_BANNER in capsys.readouterr().out
        assert not (Path.cwd() / "completion.json").exists()
        assert not (Path.cwd() / ".agent-done-marker").exists()
        assert decision_path.read_bytes() == b"\xff"
        assert (tech_lead_run / "tech-lead-data" / "tech-lead-report.md").read_text() == "T1 A1"
        _write_pair(tech_lead_run, _decision(finding_title="Lease stalled"), "T1 A1")
        safe_main()
    assert json.loads((Path.cwd() / "completion.json").read_text())["outcome"] == "completed"
