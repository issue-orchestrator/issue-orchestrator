"""E2E regression for double review-exchange ownership.

This test uses deterministic script agents, but it drives the real E2E
orchestrator process, GitHub issue/PR flow, worktree/session creation,
``coding-done`` contract, persistent review exchange, and run-scoped artifacts.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from issue_orchestrator.testing.asyncdsl import OrchestratorWatcher
from tests.e2e.conftest import OrchestratorProcess, e2e_label, find_free_port
from tests.e2e.flows import E2EFlow, start_orchestrator_runtime


_AGENT_SCRIPT = (
    Path(__file__).resolve().parent / "fixtures" / "scripts" / "double-review-agent.py"
)
_CODER_LABEL = "agent:e2e-double"
_REVIEWER_LABEL = "agent:e2e-double-reviewer"
_ISSUE_TITLE = "[M0-730] [E2E-DOUBLE-REVIEW] Review exchange double cycle"
_HEX_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.timeout(600)
@pytest.mark.gh_activity_limit(test_gh_activity_limit=650, system_gh_activity_limit=160)
async def test_local_review_exchange_runs_double_review_cycle_with_typed_run_assets(
    repo_name: str,
    e2e_project_root: Path,
    e2e_session_config: Config,
):
    """Round 1 requests changes, coder rework calls coding-done, round 2 approves."""
    isolated_label = e2e_label("double-review")
    config = _double_review_config(
        e2e_session_config,
        e2e_project_root=e2e_project_root,
        isolated_label=isolated_label,
    )
    runtime = await start_orchestrator_runtime(
        OrchestratorProcess(config, e2e_project_root),
        config.control_api_port,
        max_issues=5,
        extra_args=["--label", isolated_label],
    )
    flow = E2EFlow(
        repo=repo_name,
        watcher=runtime.watcher,
        filter_label=isolated_label,
        fail_on_blocked_failed=True,
    )
    try:
        issue, issue_number = flow.create_issue(
            _ISSUE_TITLE,
            [_CODER_LABEL, isolated_label],
            body=(
                "Exercise local review exchange with round 1 changes_requested, "
                "coder rework, and round 2 approval."
            ),
        )
        await flow.issue_seen(issue, timeout_s=90)
        await flow.session_started(issue, timeout_s=90)

        completed = await _wait_for_exchange_completed(
            runtime.watcher,
            issue_number=issue_number,
            timeout_s=360,
        )
        pr_number = await flow.pr_created(issue, timeout_s=120)
        assert pr_number > 0
        await flow.pr_has_any_label(
            issue,
            labels=[config.code_reviewed_label],
            timeout_s=120,
        )
        await flow.pr_has_any_label(
            issue,
            labels=[config.get_label_needs_human()],
            timeout_s=180,
        )
        await _assert_post_publish_escalation_stays_single(
            runtime.watcher,
            issue_number=issue_number,
            pr_number=pr_number,
            timeout_s=75,
        )

        payload = completed.get("payload", {})
        _assert_exchange_completed_payload(payload, issue_number=issue_number)

        run_dir = Path(str(payload["run_dir"]))
        await _assert_double_review_run_artifacts(
            run_dir,
            issue_number=issue_number,
            completed_payload=payload,
        )
    finally:
        flow.cleanup_created_issues()
        await runtime.close()


def _double_review_config(
    base_config: Config,
    *,
    e2e_project_root: Path,
    isolated_label: str,
) -> Config:
    config = copy.deepcopy(base_config)
    config.control_api_port = find_free_port()
    config.web_port = find_free_port()
    config.filtering.label = isolated_label
    config.e2e_pr_labels = [isolated_label]
    config.max_concurrent_sessions = 1
    config.queue_refresh_seconds = 10
    config.review_exchange_mode = "via-local-loop"
    config.review_exchange_require_validation = True
    config.review_exchange_max_rounds = 3
    config.review_exchange_max_no_progress = 2
    config.code_review_agent = _REVIEWER_LABEL
    config.code_review_label = "needs-code-review"
    config.code_reviewed_label = "code-reviewed"
    config.session_timeout_minutes = 4
    config.validation.quick.cmd = "true"
    config.validation.quick.timeout_seconds = 30
    config.validation.publish.cmd = "true"
    config.validation.publish.timeout_seconds = 30

    command = _agent_command(_AGENT_SCRIPT)
    prompt_path = e2e_project_root / "tests" / "e2e" / "fixtures" / "prompts" / "simple_task.md"
    config.agents = {
        _CODER_LABEL: AgentConfig(
            prompt_path=prompt_path,
            timeout_minutes=3,
            model="sonnet",
            command=command,
            meta_agent="claude-code",
            ai_system="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
            reviewer=_REVIEWER_LABEL,
        ),
        _REVIEWER_LABEL: AgentConfig(
            prompt_path=prompt_path,
            timeout_minutes=3,
            model="sonnet",
            command=command,
            meta_agent="claude-code",
            ai_system="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
        ),
    }
    return config


def _agent_command(script: Path) -> str:
    return (
        f"{shlex.quote(sys.executable)} -u {shlex.quote(str(script))} "
        "'{initial_prompt}'"
    )


async def _wait_for_exchange_completed(
    watcher: OrchestratorWatcher,
    *,
    issue_number: int,
    timeout_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for event in reversed(watcher.view.global_events):
            payload = event.get("payload", {}) or {}
            if (
                event.get("type") == EventName.REVIEW_EXCHANGE_COMPLETED.value
                and str(payload.get("issue_number")) == str(issue_number)
                and payload.get("rounds") == 2
                and payload.get("status") == "ok"
                and payload.get("cached") is not True
            ):
                return event
        for issue_view in watcher.view.issues.values():
            labels = set(issue_view.labels) | set(issue_view.pr.labels)
            if "blocked-failed" in labels:
                raise AssertionError(
                    f"Issue {issue_number} hit blocked-failed while waiting "
                    "for double review exchange completion"
                )
        try:
            await asyncio.wait_for(watcher._notify.wait(), timeout=2.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001

    raise TimeoutError(
        f"Timed out waiting for double review exchange completion on issue {issue_number}. "
        f"Recent events: {list(watcher.view.global_events)[-20:]}"
    )


async def _assert_post_publish_escalation_stays_single(
    watcher: OrchestratorWatcher,
    *,
    issue_number: int,
    pr_number: int,
    timeout_s: float,
) -> None:
    await _wait_for_single_review_escalation_event(
        watcher,
        issue_number=issue_number,
        pr_number=pr_number,
        timeout_s=30,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        events = _review_escalation_events(
            watcher,
            issue_number=issue_number,
            pr_number=pr_number,
        )
        if len(events) > 1:
            raise AssertionError(
                "Expected post-publish escalation to be idempotent after "
                f"needs-human; saw {len(events)} REVIEW_ESCALATED events: {events}"
            )
        try:
            await asyncio.wait_for(watcher._notify.wait(), timeout=2.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001


async def _wait_for_single_review_escalation_event(
    watcher: OrchestratorWatcher,
    *,
    issue_number: int,
    pr_number: int,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        events = _review_escalation_events(
            watcher,
            issue_number=issue_number,
            pr_number=pr_number,
        )
        if len(events) == 1:
            return
        if len(events) > 1:
            raise AssertionError(
                f"Expected one REVIEW_ESCALATED event; saw {len(events)}: {events}"
            )
        try:
            await asyncio.wait_for(watcher._notify.wait(), timeout=2.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001
    raise AssertionError(
        "Timed out waiting for REVIEW_ESCALATED event after needs-human label "
        f"on issue {issue_number} / PR {pr_number}. Recent events: "
        f"{list(watcher.view.global_events)[-20:]}"
    )


def _review_escalation_events(
    watcher: OrchestratorWatcher,
    *,
    issue_number: int,
    pr_number: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for event in watcher.view.global_events:
        payload = event.get("payload", {}) or {}
        if (
            event.get("type") == EventName.REVIEW_ESCALATED.value
            and payload.get("issue_number") == issue_number
            and payload.get("pr_number") == pr_number
        ):
            events.append(event)
    return events


def _assert_exchange_completed_payload(
    payload: dict[str, Any],
    *,
    issue_number: int,
) -> None:
    assert payload["issue_number"] == issue_number
    assert isinstance(payload["session_name"], str)
    assert payload["session_name"].strip()
    assert payload["rounds"] == 2
    assert payload["status"] == "ok"
    assert payload["reason"] == "reviewer_ok"
    assert payload.get("cached") is not True
    assert payload["review_decision_verdict"] == "approved"
    assert payload["review_nit_policy"] == "surface"
    assert payload["review_abstraction_status"] == "no_issues"
    assert Path(str(payload["run_dir"])).is_absolute()


async def _assert_double_review_run_artifacts(
    run_dir: Path,
    *,
    issue_number: int,
    completed_payload: dict[str, Any],
) -> None:
    summary_path = run_dir / "review-exchange" / "summary.json"
    await _wait_for_file(summary_path, non_empty=True)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert set(summary) == {
        "artifacts",
        "completed_rounds",
        "head_sha",
        "reason",
        "response_text",
        "status",
        "timestamp",
        "validation_passed",
    }
    assert summary["completed_rounds"] == 2
    assert summary["status"] == "ok"
    assert summary["reason"] == "reviewer_ok"
    assert summary["response_text"] == "Round 2 approves the rework"
    assert summary["validation_passed"] is True
    _assert_iso_timestamp(summary["timestamp"])
    _assert_full_sha(summary["head_sha"])

    expected_artifacts = _expected_review_artifacts(run_dir, round_index=2)
    assert summary["artifacts"] == expected_artifacts
    assert completed_payload["artifacts"] == expected_artifacts

    validation_record = run_dir / "validation-record.json"
    await _wait_for_file(validation_record, non_empty=True)
    validation_payload = json.loads(validation_record.read_text(encoding="utf-8"))
    assert set(validation_payload) == {
        "command",
        "ended_at",
        "exit_code",
        "head_sha",
        "passed",
        "schema_version",
        "stderr_path",
        "stdout_path",
        "suite",
        "timed_out",
        "started_at",
    }
    assert validation_payload["schema_version"] == 1
    assert validation_payload["suite"] == "agent_gate"
    assert validation_payload["head_sha"] == summary["head_sha"]
    assert validation_payload["passed"] is True
    assert validation_payload["exit_code"] == 0
    assert validation_payload["command"] == "true"
    assert validation_payload["timed_out"] is False
    _assert_iso_timestamp(validation_payload["started_at"])
    _assert_iso_timestamp(validation_payload["ended_at"])
    assert Path(validation_payload["stdout_path"]) == (
        Path(".issue-orchestrator")
        / "sessions"
        / run_dir.name
        / "validation-stdout.log"
    )
    assert Path(validation_payload["stderr_path"]) == (
        Path(".issue-orchestrator")
        / "sessions"
        / run_dir.name
        / "validation-stderr.log"
    )
    await _wait_for_file(_worktree_from_run_dir(run_dir) / validation_payload["stdout_path"])
    await _wait_for_file(_worktree_from_run_dir(run_dir) / validation_payload["stderr_path"])

    turns = run_dir / "review-exchange" / "turns"
    await _assert_turn_packet(
        turns / "round-1-reviewer.packet.json",
        {
            "issue_number": issue_number,
            "issue_title": _ISSUE_TITLE,
            "round_index": 1,
            "role": "reviewer",
            "require_validation": True,
            "run_dir": str(run_dir),
            "prompt_files": {"validation_record": str(validation_record)},
        },
    )
    await _assert_turn_packet(
        turns / "round-1-coder.packet.json",
        {
            "issue_number": issue_number,
            "issue_title": _ISSUE_TITLE,
            "round_index": 1,
            "role": "coder",
            "require_validation": True,
            "run_dir": str(run_dir),
            "reviewer_feedback": (
                "# Review Report\n\n"
                "## Blocking Findings\n\n"
                "### F1. Rework required\n\n"
                "The first review round intentionally requests a focused "
                "rework pass so the E2E exercises the exchange handoff.\n\n"
                "## Final Abstraction Pass\n\n"
                "Final abstraction pass: no issues found.\n"
            ),
        },
    )
    await _assert_turn_packet(
        turns / "round-2-reviewer.packet.json",
        {
            "issue_number": issue_number,
            "issue_title": _ISSUE_TITLE,
            "round_index": 2,
            "role": "reviewer",
            "require_validation": True,
            "run_dir": str(run_dir),
            "prompt_files": {"validation_record": str(validation_record)},
            "last_coder_text": "Applied E2E rework round 1",
            "last_reviewer_text": "Round 1 intentionally requires rework",
        },
    )
    await _assert_turn_result(
        turns / "round-1-reviewer-attempt-1.result.json",
        {
            "kind": "changes_requested",
            "response_text": "Round 1 intentionally requires rework",
            "getting_closer": True,
        },
    )
    await _assert_turn_result(
        turns / "round-1-coder-attempt-1.result.json",
        {
            "kind": "ok",
            "response_text": "Applied E2E rework round 1",
            "getting_closer": True,
        },
    )
    await _assert_turn_result(
        turns / "round-2-reviewer-attempt-1.result.json",
        {
            "kind": "ok",
            "response_text": "Round 2 approves the rework",
            "getting_closer": True,
        },
    )
    await _assert_review_artifact_pair(turns, round_index=1, verdict="changes_requested")
    await _assert_review_artifact_pair(turns, round_index=2, verdict="approved")

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["issue_number"] == issue_number
    assert manifest["session_name"] == completed_payload["session_name"]
    assert Path(manifest["run_dir"]) == run_dir
    assert Path(manifest["validation_record_path"]) == validation_record
    assert Path(manifest["review_exchange_dir"]) == run_dir / "review-exchange"
    assert Path(manifest["review_exchange_summary_path"]) == summary_path
    assert Path(manifest["validation_stdout"]) == run_dir / "validation-stdout.log"
    assert Path(manifest["validation_stderr"]) == run_dir / "validation-stderr.log"
    coder_recording = run_dir / "coder" / "terminal-recording.jsonl"
    reviewer_recording = run_dir / "reviewer" / "terminal-recording.jsonl"
    assert Path(manifest["coder_recording"]) == coder_recording
    assert Path(manifest["reviewer_recording"]) == (
        reviewer_recording
    )
    await _wait_for_file(coder_recording, non_empty=True)
    await _wait_for_file(reviewer_recording, non_empty=True)
    await _assert_chapters(run_dir / "coder" / "chapters.json", role="coder")
    await _assert_chapters(run_dir / "reviewer" / "chapters.json", role="reviewer")
    assert Path(manifest["persistent_pair_dir"]).is_dir()
    assert Path(manifest["coder_recording_pair"]).is_file()
    assert Path(manifest["reviewer_recording_pair"]).is_file()
    assert manifest["artifacts"]["validation_record"]["path"] == str(validation_record)
    assert manifest["artifacts"]["review_exchange_summary"]["path"] == str(summary_path)


async def _assert_turn_packet(path: Path, expected: dict[str, Any]) -> None:
    await _wait_for_file(path, non_empty=True)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == expected


async def _assert_turn_result(path: Path, expected: dict[str, Any]) -> None:
    await _wait_for_file(path, non_empty=True)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {"getting_closer", "kind", "response_text"}
    assert payload == expected


async def _assert_review_artifact_pair(
    turns_dir: Path,
    *,
    round_index: int,
    verdict: str,
) -> None:
    report = turns_dir / f"round-{round_index}-reviewer-attempt-1.review-report.md"
    decision_path = (
        turns_dir / f"round-{round_index}-reviewer-attempt-1.review-decision.json"
    )
    await _wait_for_file(report, non_empty=True)
    await _wait_for_file(decision_path, non_empty=True)
    report_text = report.read_text(encoding="utf-8")
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    assert set(decision) == {
        "abstraction_review",
        "blocking_findings",
        "nits",
        "nit_policy",
        "report_path",
        "report_sha256",
        "response_text",
        "risk",
        "schema_version",
        "tests_reviewed",
        "verdict",
    }
    assert decision["schema_version"] == 1
    assert decision["verdict"] == verdict
    assert decision["risk"] == "low"
    assert decision["nits"] == []
    assert decision["nit_policy"] == "surface"
    assert decision["tests_reviewed"] == ["E2E deterministic double review fixture"]
    assert decision["abstraction_review"] == {"status": "no_issues", "findings": []}
    assert decision["report_path"] == str(report)
    _assert_sha256(decision["report_sha256"])
    if verdict == "changes_requested":
        assert decision["response_text"] == "Round 1 intentionally requires rework"
        assert decision["blocking_findings"] == [
            {
                "id": "F1",
                "title": "Rework required",
                "rationale": "Exercise the coder rework handoff.",
            }
        ]
        assert "F1" in report_text
        assert "Final abstraction pass: no issues found." in report_text
    else:
        assert decision["response_text"] == "Round 2 approves the rework"
        assert decision["blocking_findings"] == []
        assert "No blocking findings remain after the rework turn." in report_text


async def _assert_chapters(path: Path, *, role: str) -> None:
    await _wait_for_file(path, non_empty=True)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "chapters",
        "exchange_run_id",
        "issue_number",
        "role",
        "schema_version",
    }
    assert payload["schema_version"] == 1
    assert payload["role"] == role
    assert isinstance(payload["issue_number"], int)
    assert isinstance(payload["exchange_run_id"], str)
    assert payload["exchange_run_id"].strip()
    chapters = payload["chapters"]
    assert isinstance(chapters, list)
    assert chapters
    for chapter in chapters:
        assert set(chapter) == {
            "cycle_index",
            "label",
            "recorded_at",
            "recording_event_index",
            "section",
        }
        assert isinstance(chapter["cycle_index"], int)
        assert chapter["cycle_index"] > 0
        assert chapter["section"] in {"prompt", "feedback", "timeout"}
        assert isinstance(chapter["recording_event_index"], int)
        assert chapter["recording_event_index"] >= 0
        _assert_iso_timestamp(chapter["recorded_at"])
        assert isinstance(chapter["label"], str)
        assert chapter["label"].strip()


def _expected_review_artifacts(
    run_dir: Path,
    *,
    round_index: int,
) -> list[dict[str, str]]:
    turns = run_dir / "review-exchange" / "turns"
    return [
        {
            "type": "review_report",
            "label": "Review report",
            "value": str(
                turns / f"round-{round_index}-reviewer-attempt-1.review-report.md"
            ),
            "render_mode": "markdown",
        },
        {
            "type": "review_decision",
            "label": "Decision JSON",
            "value": str(
                turns / f"round-{round_index}-reviewer-attempt-1.review-decision.json"
            ),
            "render_mode": "json",
        },
    ]


def _assert_full_sha(value: Any) -> None:
    assert isinstance(value, str)
    assert _HEX_SHA_RE.match(value), value


def _assert_sha256(value: Any) -> None:
    assert isinstance(value, str)
    assert re.fullmatch(r"[0-9a-f]{64}", value), value


def _assert_iso_timestamp(value: Any) -> None:
    assert isinstance(value, str)
    datetime.fromisoformat(value)


def _worktree_from_run_dir(run_dir: Path) -> Path:
    assert run_dir.parent.name == "sessions"
    assert run_dir.parent.parent.name == ".issue-orchestrator"
    return run_dir.parent.parent.parent


async def _wait_for_file(
    path: Path,
    *,
    timeout_s: float = 180.0,
    non_empty: bool = False,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists() and (not non_empty or path.stat().st_size > 0):
            return
        await asyncio.sleep(1.0)
    suffix = " (non-empty)" if non_empty else ""
    raise AssertionError(f"Missing expected file{suffix}: {path}")
