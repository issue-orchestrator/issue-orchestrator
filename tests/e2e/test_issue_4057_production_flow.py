"""Production-realistic E2E flow for issue #4057 parity.

This test intentionally avoids script stub agents and runs the orchestrator
process lifecycle with real coding/review agents, via-local-loop review
exchange, and real push/PR publish.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

import httpx
import pytest

from issue_orchestrator.domain.event_taxonomy import (
    REVIEW_START_CLUSTER_EVENT_NAMES,
    REVIEW_TERMINAL_CLUSTER_EVENT_NAMES,
)
from issue_orchestrator.contracts.ui_openapi_models import (
    CompletedCodingAttemptPayload,
    CompletionRecordEvidencePayload,
    DashboardTimelineContainerPayload,
    IssueDetailPayload,
    IssueLifecyclePayload,
    ReviewApprovedPayload,
    ReviewTranscriptAvailablePayload,
    SessionRecordingAvailablePayload,
    ValidationPassedPayload,
)
from issue_orchestrator.infra.config import AgentConfig
from issue_orchestrator.testing.support.test_data import close_issue
from tests.e2e.conftest import e2e_label, find_free_port
from tests.e2e.flows import E2EFlow, start_orchestrator_runtime

CODING_AGENT_TIMEOUT_MINUTES = 75
REVIEW_AGENT_TIMEOUT_MINUTES = 35
E2E_TIMEOUT_MINUTES = 150
FOLLOW_UP_FILE_PATH = "/tmp/follow-up-issues.jsonl"
ISSUE_4057_TARGET_TEST = "tests/unit/test_dashboard_view_model.py"
ISSUE_4057_VALIDATION_CMD = f"pytest {ISSUE_4057_TARGET_TEST} -q"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.live,
    pytest.mark.asyncio,
    pytest.mark.timeout(E2E_TIMEOUT_MINUTES * 60),
]


def build_issue_4057_prompt() -> str:
    return (
        "Work on issue #{issue_number}: {issue_title}. "
        f"This session is time-bounded to {CODING_AGENT_TIMEOUT_MINUTES} minutes, so finish the core task first. "
        "This is a production-flow E2E control-path check, not an open-ended feature build. "
        f"Open only {ISSUE_4057_TARGET_TEST}. "
        "Add exactly one new regression test named "
        "`test_normalize_status_reason_drops_none_and_blank_values` that proves "
        '`_normalize_status_reason(None)` and `_normalize_status_reason("   ")` both return `None`. '
        "Do NOT edit production files, generated contracts, schemas, `.gitignore`, or any other tests. "
        "Do NOT edit src/issue_orchestrator/entrypoints/cli_tools/agent_done.py, "
        "src/issue_orchestrator/entrypoints/cli_tools/provider_runner.py, or "
        "src/issue_orchestrator/entrypoints/cli_tools/setup_wizard.py. "
        "Do NOT modify tests in tests/unit/test_worktree.py, tests/unit/test_cli.py, "
        "or tests/unit/test_completion_processor.py. "
        "Do not spend time in unrelated orchestration, validation, or session-plumbing files. "
        f"Run `{ISSUE_4057_VALIDATION_CMD}` once your focused change is ready. "
        f"For this session, run final validation with `{ISSUE_4057_VALIDATION_CMD}` only. "
        "If you discover unrelated ancillary work, do not fix it in this session. "
        f"Write it to {FOLLOW_UP_FILE_PATH} and pass `--follow-up-file {FOLLOW_UP_FILE_PATH}` to coding-done completed. "
        "Do not look up or reference other issue numbers. "
        "Follow repo-specific/prompts/simple-fix.md exactly. "
        "Commit your changes, use coding-done to report outcome and include validation artifacts, then exit with /exit."
    )


def build_issue_4057_body() -> str:
    return (
        "Production-parity focused E2E run.\n\n"
        "Requirements:\n"
        "- Follow repo-specific/prompts/simple-fix.md\n"
        "- Treat this as a control-flow verification, not a feature implementation task\n"
        f"- Open only {ISSUE_4057_TARGET_TEST}\n"
        "- Add exactly one regression test named test_normalize_status_reason_drops_none_and_blank_values\n"
        '- Assert _normalize_status_reason(None) is None and _normalize_status_reason("   ") is None\n'
        "- Do not edit production files, generated contracts, schemas, .gitignore, or any other tests\n"
        f"- Run {ISSUE_4057_VALIDATION_CMD} before completion\n"
        f"- Final validation must run through {ISSUE_4057_VALIDATION_CMD}\n"
        f"- Record unrelated ancillary work in {FOLLOW_UP_FILE_PATH} and pass --follow-up-file instead of broadening scope\n"
        "- Complete via coding-done and exit\n"
    )


ISSUE_4057_PROMPT = build_issue_4057_prompt()


def _seed_ref_for_local_issue_worktrees(repo_root: Path) -> str | None:
    """Seed fresh issue worktrees from the current committed branch state during local iteration."""
    if os.environ.get("CI"):
        return None
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


async def _wait_for_session_manifest(
    web_port: int,
    issue_number: int,
    *,
    timeout_s: float,
    previous_run_dir: Path | None = None,
    required_artifacts: tuple[str, ...] = (),
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    last_status = "no response"
    last_payload: dict[str, object] | None = None

    async with httpx.AsyncClient(timeout=20.0) as client:
        while time.monotonic() < deadline:
            response = await client.get(
                f"http://localhost:{web_port}/api/session/manifest/{issue_number}"
            )
            last_status = f"{response.status_code}: {response.text[:200]}"
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict):
                    last_payload = payload
                    run_dir_value = payload.get("run_dir")
                    manifest = payload.get("manifest")
                    artifacts = (
                        manifest.get("artifacts")
                        if isinstance(manifest, dict)
                        else None
                    )
                    if isinstance(run_dir_value, str) and run_dir_value:
                        run_dir = Path(run_dir_value)
                        if previous_run_dir is not None and run_dir == previous_run_dir:
                            await asyncio.sleep(1.0)
                            continue
                        if required_artifacts and not (
                            isinstance(artifacts, dict)
                            and all(name in artifacts for name in required_artifacts)
                        ):
                            await asyncio.sleep(1.0)
                            continue
                        return payload
            await asyncio.sleep(1.0)

    raise TimeoutError(
        f"Timed out waiting for session manifest for issue {issue_number} "
        f"(required_artifacts={required_artifacts}, previous_run_dir={previous_run_dir}). "
        f"Last status: {last_status}. Last payload: {last_payload}"
    )


async def _wait_for_file(
    path: Path, *, timeout_s: float = 180.0, non_empty: bool = False
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists() and (not non_empty or path.stat().st_size > 0):
            return
        await asyncio.sleep(1.0)
    suffix = " (non-empty)" if non_empty else ""
    raise AssertionError(f"Missing expected file{suffix}: {path}")


async def _assert_stage_artifacts(
    run_dir: Path,
    *,
    completion_file_names: list[str],
    require_validation: bool,
) -> None:
    await _wait_for_file(run_dir / "terminal-recording.jsonl", non_empty=True)
    for name in completion_file_names:
        await _wait_for_file(run_dir / name, non_empty=True)
    if require_validation:
        await _wait_for_file(run_dir / "validation-record.json")
        stdout_path = run_dir / "validation-stdout.log"
        stderr_path = run_dir / "validation-stderr.log"
        output_path = run_dir / "validation-output.log"
        if not (
            (stdout_path.exists() and stdout_path.stat().st_size > 0)
            or (stderr_path.exists() and stderr_path.stat().st_size > 0)
            or (output_path.exists() and output_path.stat().st_size > 0)
        ):
            raise AssertionError(
                "Expected non-empty validation output log in run dir "
                f"{run_dir} (checked validation-stdout.log, validation-stderr.log, validation-output.log)"
            )


async def _assert_review_stage_artifacts(
    run_dir: Path, *, require_validation: bool
) -> None:
    """Review-exchange runs are protocol-driven and may not emit reviewer completion files."""
    await _wait_for_file(run_dir / "terminal-recording.jsonl", non_empty=True)
    await _wait_for_file(run_dir / "review-exchange" / "summary.json", non_empty=True)
    await _wait_for_file(run_dir / "review-exchange" / "round-001.json", non_empty=True)
    if require_validation:
        await _wait_for_file(run_dir / "validation-record.json")


async def _assert_live_terminal_recording_stream(
    *,
    issue_number: int,
    issue_key: str,
    run_dir: Path,
    web_port: int,
    watcher,
    timeout_s: float = 180.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    total_event_values: list[int] = []
    size_values: list[int] = []
    non_empty_samples = 0
    validation_observed = False

    async with httpx.AsyncClient(timeout=20.0) as client:
        while time.monotonic() < deadline:
            response = await client.get(
                f"http://localhost:{web_port}/api/session/terminal-recording/{issue_number}",
                params={"run_dir": str(run_dir), "offset": 0, "limit": 0},
            )
            if response.status_code == 200:
                payload = response.json()
                total_events = int(payload.get("total_events") or 0)
                events = payload.get("events") or []
                if isinstance(events, list) and any(
                    event.get("event_type") == "output"
                    for event in events
                    if isinstance(event, dict)
                ):
                    non_empty_samples += 1
                total_event_values.append(total_events)

            terminal_recording = run_dir / "terminal-recording.jsonl"
            if terminal_recording.exists():
                size_values.append(terminal_recording.stat().st_size)
            validation_output = run_dir / "validation-output.log"
            if validation_output.exists():
                validation_observed = True

            issue_view = watcher.view.issues.get(issue_key)
            in_progress = bool(issue_view and "in-progress" in issue_view.labels)
            if in_progress and len(total_event_values) >= 3:
                event_growth = max(total_event_values) > min(total_event_values)
                size_growth = len(size_values) >= 2 and max(size_values) > min(
                    size_values
                )
                if non_empty_samples >= 2 and (event_growth or size_growth):
                    return
            if (
                validation_observed
                and non_empty_samples == 0
                and len(total_event_values) >= 5
            ):
                raise AssertionError(
                    "terminal recording remained empty even after validation artifacts were present: "
                    f"event_samples={total_event_values[-8:]} size_samples={size_values[-8:]} run_dir={run_dir}"
                )
            if (
                not in_progress
                and non_empty_samples == 0
                and len(total_event_values) >= 5
            ):
                raise AssertionError(
                    "terminal recording remained empty after issue left in-progress state: "
                    f"event_samples={total_event_values[-8:]} size_samples={size_values[-8:]} run_dir={run_dir}"
                )

            try:
                await asyncio.wait_for(watcher._notify.wait(), timeout=2.0)  # noqa: SLF001
            except asyncio.TimeoutError:
                pass
            watcher._notify.clear()  # noqa: SLF001

    raise AssertionError(
        "terminal recording did not demonstrate near-real-time live updates while coding was in-progress: "
        f"samples={len(total_event_values)} non_empty_samples={non_empty_samples} "
        f"event_samples={total_event_values[-8:]} size_samples={size_values[-8:]} run_dir={run_dir}"
    )


async def _fetch_issue_detail(web_port: int, issue_number: int) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"http://localhost:{web_port}/api/issue-detail/{issue_number}"
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _steps_from_issue_detail(payload: dict[str, object]) -> list[dict[str, object]]:
    runs = payload.get("runs")
    assert isinstance(runs, list) and runs, "Expected issue-detail runs"
    latest = runs[-1]
    assert isinstance(latest, dict), "Expected latest run dict"
    cycles = latest.get("cycles")
    assert isinstance(cycles, list) and cycles, "Expected cycle list"
    first_cycle = cycles[0]
    assert isinstance(first_cycle, dict), "Expected first cycle dict"
    steps = first_cycle.get("steps")
    assert isinstance(steps, list) and steps, "Expected timeline steps"
    return [step for step in steps if isinstance(step, dict)]


def _issue_lifecycle_from_detail(
    payload: dict[str, object],
    *,
    issue_number: int,
) -> IssueLifecyclePayload:
    detail = IssueDetailPayload.model_validate(payload)
    lifecycle = detail.lifecycle
    assert isinstance(lifecycle, DashboardTimelineContainerPayload), (
        "Expected issue-detail semantic dashboard lifecycle"
    )

    issue_lifecycles = lifecycle.current.issue_lifecycles
    matches = [
        lifecycle_item
        for lifecycle_item in issue_lifecycles
        if lifecycle_item.issue_number == issue_number
    ]
    assert len(matches) == 1, (
        f"Expected exactly one lifecycle for issue #{issue_number}; "
        f"found {len(matches)} in {issue_lifecycles}"
    )
    return matches[0]


def _assert_issue_lifecycle_contains_approved_review_cycle(
    payload: dict[str, object],
    *,
    issue_number: int,
    coding_run_dir: Path,
) -> None:
    issue_lifecycle = _issue_lifecycle_from_detail(
        payload,
        issue_number=issue_number,
    )
    cycles = issue_lifecycle.cycles
    assert cycles, "Expected semantic issue cycles"

    approved_cycles = []
    for cycle in cycles:
        if isinstance(cycle.coder, CompletedCodingAttemptPayload) and isinstance(
            cycle.review,
            ReviewApprovedPayload,
        ):
            approved_cycles.append(cycle)

    assert approved_cycles, (
        "Expected at least one semantically approved lifecycle cycle with "
        "completed coding and approved review"
    )
    approved_cycle = approved_cycles[-1]
    assert approved_cycle.outcome == "approved"

    coder = approved_cycle.coder
    review = approved_cycle.review
    assert isinstance(coder, CompletedCodingAttemptPayload)
    assert isinstance(review, ReviewApprovedPayload)
    assert isinstance(coder.validation, ValidationPassedPayload)
    assert isinstance(coder.completion_record, CompletionRecordEvidencePayload)
    assert isinstance(coder.session_recording, SessionRecordingAvailablePayload)
    assert coder.session_recording.run_dir == str(coding_run_dir)
    assert isinstance(review.session_recording, SessionRecordingAvailablePayload)
    assert isinstance(review.transcript, ReviewTranscriptAvailablePayload)


@pytest.mark.gh_activity_limit(test_gh_activity_limit=550, system_gh_activity_limit=220)
async def test_4057_production_real_agents_publish_gate_and_diagnostics(
    repo_name: str,
    e2e_project_root: Path,
    e2e_session_config,
):
    """Run production-like #4057 lifecycle with local review loop and validate diagnostics."""
    dry_run = os.environ.get("E2E_DRY_RUN_PUSH") == "1"
    if dry_run:
        pytest.skip(
            "Production-parity flow requires real PR creation (E2E_DRY_RUN_PUSH=false)"
        )

    run_suffix = str(int(time.time()))
    isolated_label = e2e_label(f"isolated-4057-{run_suffix}")
    issue_tag = e2e_label(f"real-4057-case-{run_suffix}")
    review_label = e2e_label(f"needs-review-4057-{run_suffix}")
    reviewed_label = e2e_label(f"reviewed-4057-{run_suffix}")

    config = copy.deepcopy(e2e_session_config)
    config.control_api_port = find_free_port()
    config.web_port = find_free_port()
    config.filtering.label = isolated_label
    config.state_file = Path(f"/tmp/io-e2e-state-{run_suffix}.json")
    config.e2e_pr_labels = [isolated_label]
    config.max_concurrent_sessions = 1
    config.session_timeout_minutes = CODING_AGENT_TIMEOUT_MINUTES
    config.queue_refresh_seconds = 10
    config.worktree_base_branch_override = "main"
    worktree_seed_ref = _seed_ref_for_local_issue_worktrees(e2e_project_root)
    config.worktree_seed_ref = worktree_seed_ref
    # Ensure each ephemeral issue worktree starts clean; inherited dirty state
    # from prior attempts can otherwise derail validation with unrelated failures.
    config.setup_worktree = [
        "make worktree-setup",
        "git reset --hard HEAD",
        "git clean -fd",
        'HOOKS_DIR="$(git rev-parse --git-path hooks)" && printf \'#!/usr/bin/env bash\\nexit 0\\n\' > "$HOOKS_DIR/pre-push.project" && chmod +x "$HOOKS_DIR/pre-push.project"',
    ]
    config.code_review_agent = "agent:reviewer"
    config.code_review_label = review_label
    config.code_reviewed_label = reviewed_label
    config.validation.quick.cmd = ISSUE_4057_VALIDATION_CMD
    config.validation.quick.timeout_seconds = 20 * 60
    config.validation.publish.cmd = ISSUE_4057_VALIDATION_CMD
    config.validation.publish.timeout_seconds = 20 * 60
    config.review_exchange_mode = "via-local-loop"
    config.review_exchange_require_validation = True
    config.agents = {
        "agent:backend": AgentConfig(
            prompt_path=e2e_project_root
            / "repo-specific"
            / "prompts"
            / "simple-fix.md",
            provider="claude-code",
            model="opus",
            timeout_minutes=CODING_AGENT_TIMEOUT_MINUTES,
            ai_system="claude-code",
            permission_mode="bypassPermissions",
            initial_prompt=ISSUE_4057_PROMPT,
        ),
        "agent:reviewer": AgentConfig(
            prompt_path=e2e_project_root / "repo-specific" / "prompts" / "reviewer.md",
            provider="claude-code",
            model="opus",
            timeout_minutes=REVIEW_AGENT_TIMEOUT_MINUTES,
            ai_system="claude-code",
            permission_mode="bypassPermissions",
            initial_prompt=(
                "Review PR #{pr_number} for issue #{issue_number}: {issue_title}. "
                "Follow repo-specific/prompts/reviewer.md. "
                "Respect validation artifacts in the run directory."
            ),
        ),
    }

    runtime = None
    flow: E2EFlow | None = None
    issue = None
    issue_number: int | None = None
    pr_number: int | None = None

    try:
        from tests.e2e.conftest import OrchestratorProcess

        # Prevent startup from restoring unrelated stale subprocess sessions.
        state_dir = e2e_project_root / ".issue-orchestrator" / "state"
        for stale in (
            state_dir / "session_registry.sqlite",
            state_dir / "subprocess_sessions.sqlite",
            state_dir / "subprocess_sessions.json",
            state_dir / "subprocess_sessions.json.bak",
        ):
            if stale.exists():
                stale.unlink()
        stale_dir = state_dir / "subprocess_sessions"
        if stale_dir.exists():
            shutil.rmtree(stale_dir)

        orchestrator = OrchestratorProcess(config, e2e_project_root)
        runtime = await start_orchestrator_runtime(
            orchestrator,
            config.control_api_port,
            max_issues=0,
        )
        flow = E2EFlow(
            repo=repo_name,
            watcher=runtime.watcher,
            filter_label=isolated_label,
            fail_on_blocked_failed=True,
        )

        issue_title = "[M4-057] E2E: add dashboard status-normalization regression test [production parity]"
        issue, issue_number = flow.create_issue(
            issue_title,
            ["agent:backend", issue_tag, isolated_label],
            body=build_issue_4057_body(),
        )
        # issue from create_issue uses the stable external_id (e.g. "M4-057"),
        # which now matches the key format used by all watcher events.
        await flow.issue_seen(issue, timeout_s=120)
        await flow.session_started(issue, timeout_s=10 * 60)

        coding_manifest = await _wait_for_session_manifest(
            config.web_port,
            issue_number,
            timeout_s=120,
            required_artifacts=("terminal_recording",),
        )
        coding_run_dir = Path(str(coding_manifest["run_dir"]))
        logger.info("[4057] Coding manifest resolved. run_dir=%s", coding_run_dir)

        detail_during_coding = await _fetch_issue_detail(config.web_port, issue_number)
        coding_steps = _steps_from_issue_detail(detail_during_coding)
        has_coding_log_action = any(
            any(
                isinstance(action, dict)
                and action.get("type") == "open_agent_log"
                and action.get("run_dir") == str(coding_run_dir)
                for action in (step.get("actions") or [])
                if isinstance(action, dict)
            )
            for step in coding_steps
        )
        assert has_coding_log_action, (
            "Expected run-scoped open_agent_log action during coding"
        )
        logger.info("[4057] UI assertions OK. Starting live log stream check...")

        await _assert_live_terminal_recording_stream(
            issue_number=issue_number,
            issue_key=issue.stable_id(),
            run_dir=coding_run_dir,
            web_port=config.web_port,
            watcher=runtime.watcher,
        )
        logger.info("[4057] Live log stream OK. Waiting for review manifest...")

        review_manifest = await _wait_for_session_manifest(
            config.web_port,
            issue_number,
            timeout_s=25 * 60,
            previous_run_dir=coding_run_dir,
            required_artifacts=("review_exchange_summary", "validation_record"),
        )
        review_run_dir = Path(str(review_manifest["run_dir"]))
        logger.info("[4057] Review manifest resolved. run_dir=%s", review_run_dir)
        logger.info("[4057] Checking coding stage artifacts...")
        await _assert_stage_artifacts(
            coding_run_dir,
            completion_file_names=["completion-record.json"],
            require_validation=True,
        )
        logger.info("[4057] Coding artifacts OK. Checking review exchange summary...")
        review_summary_path = review_run_dir / "review-exchange" / "summary.json"
        await _wait_for_file(review_summary_path, non_empty=True)
        review_summary = json.loads(review_summary_path.read_text(encoding="utf-8"))
        status = str(review_summary.get("status") or "")
        logger.info("[4057] Review summary status=%s", status)
        assert status == "ok", (
            "Review exchange did not finish successfully "
            f"(status={status!r}, payload={review_summary})"
        )
        logger.info("[4057] Checking review stage artifacts...")
        await _assert_review_stage_artifacts(review_run_dir, require_validation=True)
        # Check the raw terminal recording for protocol errors.
        review_log = review_run_dir / "terminal-recording.jsonl"
        if review_log.exists():
            review_content = review_log.read_text(errors="replace").lower()
            assert "protocol error" not in review_content, (
                f"Detected protocol error in review exchange output for {review_run_dir}"
            )
        logger.info("[4057] Review artifacts OK. Waiting for pr_created...")

        pr_number = await flow.pr_created(issue, timeout_s=35 * 60)
        assert pr_number > 0
        logger.info("[4057] PR #%d created. Checking diagnostics...", pr_number)

        diagnostics = None
        diagnostics_deadline = time.monotonic() + (25 * 60)
        while time.monotonic() < diagnostics_deadline:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    f"http://localhost:{config.web_port}/api/dialog/session-diagnostics/{issue_number}",
                    params={"run_dir": str(coding_run_dir)},
                )
            if response.status_code == 200:
                payload = response.json()
                actions = payload.get("actions")
                if isinstance(actions, list):
                    labels = {
                        str(action.get("label"))
                        for action in actions
                        if isinstance(action, dict)
                    }
                    if {"Open Validation Record", "Open Validation Output"}.issubset(
                        labels
                    ):
                        diagnostics = payload
                        break
            await asyncio.sleep(2)

        assert diagnostics is not None, (
            "Session diagnostics never exposed both validation actions for coding run "
            f"(run_dir={coding_run_dir})"
        )
        logger.info("[4057] Diagnostics found. Checking validation paths...")

        actions = diagnostics.get("actions")
        assert isinstance(actions, list)
        action_map = {
            str(action.get("label")): action
            for action in actions
            if isinstance(action, dict) and action.get("label")
        }
        validation_record_path = Path(str(action_map["Open Validation Record"]["path"]))
        validation_output_path = Path(str(action_map["Open Validation Output"]["path"]))
        assert validation_record_path.exists(), (
            f"Validation record missing: {validation_record_path}"
        )
        assert validation_output_path.exists(), (
            f"Validation output missing: {validation_output_path}"
        )
        assert validation_output_path.stat().st_size > 0, (
            f"Validation output empty: {validation_output_path}"
        )
        logger.info("[4057] Validation paths OK. Checking issue detail timeline...")

        detail_after_review = await _fetch_issue_detail(config.web_port, issue_number)
        steps_after_review = _steps_from_issue_detail(detail_after_review)
        _assert_issue_lifecycle_contains_approved_review_cycle(
            detail_after_review,
            issue_number=issue_number,
            coding_run_dir=coding_run_dir,
        )
        # Review lifecycle check against the collapsed Story view.
        #
        # The backend always emits a deterministic cluster of
        # review-start events (review.started -> review_exchange.started
        # -> review_exchange.round_started) and a cluster of terminal
        # events (review_exchange.round_completed ->
        # review_exchange.completed -> review.approved /
        # review.changes_requested). The /api/issue-detail endpoint runs
        # in view=user by default and collapses each cluster to exactly
        # one representative row.
        #
        # So the real contract the test should guard is: the collapsed
        # timeline contains at least one row from each cluster. We reuse
        # the exact frozensets the view-model collapser uses
        # (domain.event_taxonomy), so this assertion cannot drift from
        # the view's definition of the cluster.
        observed_events = [step.get("event") for step in steps_after_review]
        review_started = any(
            e in REVIEW_START_CLUSTER_EVENT_NAMES for e in observed_events
        )
        review_terminated = any(
            e in REVIEW_TERMINAL_CLUSTER_EVENT_NAMES for e in observed_events
        )
        assert review_started and review_terminated, (
            "Expected the collapsed issue detail timeline to contain at "
            "least one row from the review-start cluster "
            f"({sorted(REVIEW_START_CLUSTER_EVENT_NAMES)}) and at least "
            "one row from the review-terminal cluster "
            f"({sorted(REVIEW_TERMINAL_CLUSTER_EVENT_NAMES)}). "
            f"Observed events: {observed_events}"
        )

        has_diagnostics_action = any(
            any(
                isinstance(action, dict)
                and action.get("type") == "open_session_diagnostics"
                and action.get("run_dir") in {str(coding_run_dir), str(review_run_dir)}
                for action in (step.get("actions") or [])
                if isinstance(action, dict)
            )
            for step in steps_after_review
        )
        assert has_diagnostics_action, (
            "Expected run-scoped diagnostics action in timeline steps"
        )
        logger.info("[4057] ALL ASSERTIONS PASSED!")

    finally:
        if flow and pr_number:
            try:
                flow.close_pr(pr_number)
            except Exception:
                pass
        if flow and issue_number is not None:
            try:
                close_issue(
                    repo_name,
                    issue_number,
                    "Closed by production-parity 4057 e2e cleanup",
                )
            except Exception:
                pass
        if runtime is not None:
            await runtime.close()
