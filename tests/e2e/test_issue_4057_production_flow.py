"""Production-realistic E2E flow for issue #4057 parity.

This test intentionally avoids script stub agents and runs the orchestrator
process lifecycle with real coding/review agents, via-local-loop review
exchange, and real push/PR publish.
"""

from __future__ import annotations

import asyncio
import copy
import os
import shutil
import time
from pathlib import Path

import httpx
import pytest

from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import AgentConfig
from issue_orchestrator.testing.support.test_data import close_issue
from tests.e2e.conftest import e2e_label, find_free_port
from tests.e2e.flows import E2EFlow, issue_key_for_number, start_orchestrator_runtime

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.live,
    pytest.mark.asyncio,
    pytest.mark.timeout(45 * 60),
]

ISSUE_4057_PROMPT = (
    "Work on issue #{issue_number}: {issue_title}. "
    "Stay strictly focused on this issue implementation; do NOT refactor unrelated areas. "
    "For this issue, limit code edits to src/issue_orchestrator/view_models/dashboard.py "
    "and tests/unit/test_dashboard_view_model.py unless absolutely required for correctness. "
    "Do NOT edit src/issue_orchestrator/entrypoints/cli_tools/agent_done.py, "
    "src/issue_orchestrator/entrypoints/cli_tools/provider_runner.py, or "
    "src/issue_orchestrator/entrypoints/cli_tools/setup_wizard.py. "
    "Do NOT modify tests in tests/unit/test_worktree.py, tests/unit/test_cli.py, "
    "or tests/unit/test_completion_processor.py. "
    "For this session, run validation with `make validate-quick` "
    "(do not run `make validate`). "
    "If the provider circuit breaker status is already correctly surfaced and covered by tests, "
    "make no code changes: run `make validate-quick` and complete with agent-done. "
    "Prefer the smallest possible diff; do not add broad refactors or extra coverage beyond this issue. "
    "If any unrelated validation failure appears, do not chase it; continue with the current issue-focused changes only. "
    "Do not look up or reference other issue numbers. "
    "Follow repo-specific/prompts/simple-fix.md exactly. "
    "Use agent-done to report outcome and include validation artifacts. "
    "When finished, exit with /exit."
)
async def _wait_for_run_event(
    watcher,
    *,
    issue_number: int,
    event_name: EventName,
    timeout_s: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    needle = event_name.value
    issue_id = str(issue_number)

    while time.monotonic() < deadline:
        for event in watcher.view.global_events:
            if event.get("type") != needle:
                continue
            payload = event.get("payload") or {}
            if str(payload.get("issue_number")) != issue_id:
                continue
            run_dir = payload.get("run_dir")
            if isinstance(run_dir, str) and run_dir:
                return payload
        try:
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001

    raise TimeoutError(f"Timed out waiting for {needle} with run_dir for issue {issue_number}")


async def _wait_for_issue_event(
    watcher,
    *,
    issue_number: int,
    event_name: EventName,
    timeout_s: float,
    predicate=None,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    needle = event_name.value
    issue_id = str(issue_number)

    while time.monotonic() < deadline:
        for event in watcher.view.global_events:
            if event.get("type") != needle:
                continue
            payload = event.get("payload") or {}
            if str(payload.get("issue_number")) != issue_id:
                continue
            if predicate is not None and not predicate(payload):
                continue
            return payload
        try:
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001

    raise TimeoutError(f"Timed out waiting for {needle} for issue {issue_number}")


async def _wait_for_file(path: Path, *, timeout_s: float = 180.0, non_empty: bool = False) -> None:
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
    await _wait_for_file(run_dir / "ui-session.log", non_empty=True)
    await _wait_for_file(run_dir / "provider-runner" / "stdout.log", non_empty=True)
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


async def _assert_review_stage_artifacts(run_dir: Path, *, require_validation: bool) -> None:
    """Review-exchange runs are protocol-driven and may not emit reviewer completion files."""
    await _wait_for_file(run_dir / "ui-session.log", non_empty=True)
    await _wait_for_file(run_dir / "provider-runner" / "stdout.log", non_empty=True)
    await _wait_for_file(run_dir / "review-exchange" / "summary.json", non_empty=True)
    await _wait_for_file(run_dir / "review-exchange" / "round-001.json", non_empty=True)
    if require_validation:
        await _wait_for_file(run_dir / "validation-record.json")


async def _assert_live_ui_session_log_stream(
    *,
    issue_number: int,
    run_dir: Path,
    web_port: int,
    watcher,
    timeout_s: float = 180.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    total_lines_values: list[int] = []
    size_values: list[int] = []
    non_empty_samples = 0
    validation_observed = False

    async with httpx.AsyncClient(timeout=20.0) as client:
        while time.monotonic() < deadline:
            response = await client.get(
                f"http://localhost:{web_port}/api/log/local/{issue_number}",
                params={"run_dir": str(run_dir), "offset": 0, "limit": 0},
            )
            if response.status_code == 200:
                payload = response.json()
                total_lines = int(payload.get("total_lines") or 0)
                lines = payload.get("lines") or []
                if isinstance(lines, list) and any(str(line).strip() for line in lines):
                    non_empty_samples += 1
                total_lines_values.append(total_lines)

            ui_log = run_dir / "ui-session.log"
            if ui_log.exists():
                size_values.append(ui_log.stat().st_size)
            validation_output = run_dir / "validation-output.log"
            if validation_output.exists():
                validation_observed = True

            issue_view = watcher.view.issues.get(str(issue_number))
            in_progress = bool(issue_view and "in-progress" in issue_view.labels)
            if in_progress and len(total_lines_values) >= 3:
                line_growth = max(total_lines_values) > min(total_lines_values)
                size_growth = len(size_values) >= 2 and max(size_values) > min(size_values)
                if non_empty_samples >= 2 and (line_growth or size_growth):
                    return
            if validation_observed and non_empty_samples == 0 and len(total_lines_values) >= 5:
                raise AssertionError(
                    "ui-session.log remained empty even after validation artifacts were present: "
                    f"line_samples={total_lines_values[-8:]} size_samples={size_values[-8:]} run_dir={run_dir}"
                )
            if not in_progress and non_empty_samples == 0 and len(total_lines_values) >= 5:
                raise AssertionError(
                    "ui-session.log remained empty after issue left in-progress state: "
                    f"line_samples={total_lines_values[-8:]} size_samples={size_values[-8:]} run_dir={run_dir}"
                )

            try:
                await asyncio.wait_for(watcher._notify.wait(), timeout=2.0)  # noqa: SLF001
            except asyncio.TimeoutError:
                pass
            watcher._notify.clear()  # noqa: SLF001

    raise AssertionError(
        "ui-session.log did not demonstrate near-real-time live updates while coding was in-progress: "
        f"samples={len(total_lines_values)} non_empty_samples={non_empty_samples} "
        f"line_samples={total_lines_values[-8:]} size_samples={size_values[-8:]} run_dir={run_dir}"
    )


async def _fetch_issue_detail(web_port: int, issue_number: int) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(f"http://localhost:{web_port}/api/issue-detail/{issue_number}")
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


@pytest.mark.gh_activity_limit(test_gh_activity_limit=380, system_gh_activity_limit=140)
async def test_4057_production_real_agents_publish_gate_and_diagnostics(
    repo_name: str,
    e2e_project_root: Path,
    e2e_session_config,
):
    """Run production-like #4057 lifecycle with local review loop and validate diagnostics."""
    dry_run = os.environ.get("E2E_DRY_RUN_PUSH") == "1"
    if dry_run:
        pytest.skip("Production-parity flow requires real PR creation (E2E_DRY_RUN_PUSH=false)")

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
    config.session_timeout_minutes = 75
    config.queue_refresh_seconds = 10
    config.worktree_base_branch_override = "main"
    # Ensure each ephemeral issue worktree starts clean; inherited dirty state
    # from prior attempts can otherwise derail validation with unrelated failures.
    config.setup_worktree = [
        "make worktree-setup",
        "git reset --hard HEAD",
        "git clean -fd",
        "HOOKS_DIR=\"$(git rev-parse --git-path hooks)\" && printf '#!/usr/bin/env bash\\nexit 0\\n' > \"$HOOKS_DIR/pre-push.project\" && chmod +x \"$HOOKS_DIR/pre-push.project\"",
    ]
    config.code_review_agent = "agent:reviewer"
    config.code_review_label = review_label
    config.code_reviewed_label = reviewed_label
    config.validation.cmd = "make validate-quick"
    config.validation.timeout_seconds = 20 * 60
    config.review_exchange_mode = "via-local-loop"
    config.review_exchange_require_validation = True
    config.agents = {
            "agent:backend": AgentConfig(
                prompt_path=e2e_project_root / "repo-specific" / "prompts" / "simple-fix.md",
                provider="claude-code",
                model="sonnet",
                timeout_minutes=60,
                ai_system="claude-code",
                permission_mode="bypassPermissions",
                initial_prompt=ISSUE_4057_PROMPT,
            ),
        "agent:reviewer": AgentConfig(
            prompt_path=e2e_project_root / "repo-specific" / "prompts" / "reviewer.md",
            provider="claude-code",
            model="sonnet",
            timeout_minutes=25,
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

        issue_title = "[M4-057] UI: Surface provider circuit breaker status [production parity e2e]"
        issue, issue_number = flow.create_issue(
            issue_title,
            ["agent:backend", issue_tag, isolated_label],
            body=(
                "Production-parity focused E2E run.\n\n"
                "Requirements:\n"
                "- Follow repo-specific/prompts/simple-fix.md\n"
                "- Limit edits to src/issue_orchestrator/view_models/dashboard.py and tests/unit/test_dashboard_view_model.py unless absolutely required\n"
                "- If behavior is already implemented, do not edit code; just run make validate-quick and complete via agent-done\n"
                "- Complete via agent-done\n"
                "- Validation must run through make validate-quick\n"
            ),
        )
        issue = issue_key_for_number(repo_name, issue_number)
        await flow.issue_seen(issue, timeout_s=120)
        await flow.session_started(issue, timeout_s=10 * 60)

        coding_started = await _wait_for_run_event(
            runtime.watcher,
            issue_number=issue_number,
            event_name=EventName.SESSION_STARTED,
            timeout_s=10 * 60,
        )
        coding_run_dir = Path(str(coding_started["run_dir"]))

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
        assert has_coding_log_action, "Expected run-scoped open_agent_log action during coding"

        await _assert_live_ui_session_log_stream(
            issue_number=issue_number,
            run_dir=coding_run_dir,
            web_port=config.web_port,
            watcher=runtime.watcher,
        )

        review_started = await _wait_for_run_event(
            runtime.watcher,
            issue_number=issue_number,
            event_name=EventName.REVIEW_EXCHANGE_STARTED,
            timeout_s=20 * 60,
        )
        review_run_dir = Path(str(review_started["run_dir"]))
        await _assert_stage_artifacts(
            coding_run_dir,
            completion_file_names=["completion-agent_backend.json", "completion-record.json"],
            require_validation=True,
        )
        review_completed = await _wait_for_issue_event(
            runtime.watcher,
            issue_number=issue_number,
            event_name=EventName.REVIEW_EXCHANGE_COMPLETED,
            timeout_s=30 * 60,
        )
        status = str(review_completed.get("status") or "")
        reason = str(review_completed.get("reason") or "")
        assert status == "ok", (
            "Review exchange did not finish successfully "
            f"(status={status!r}, reason={reason!r}, payload={review_completed})"
        )
        await _assert_review_stage_artifacts(review_run_dir, require_validation=True)
        await _wait_for_file(review_run_dir / "review-exchange" / "summary.json")
        review_stdout = (review_run_dir / "provider-runner" / "stdout.log").read_text(errors="replace").lower()
        assert "protocol error" not in review_stdout, (
            f"Detected protocol error in review exchange output for {review_run_dir}"
        )

        pr_number = await flow.pr_created(issue, timeout_s=35 * 60)
        assert pr_number > 0

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
                    labels = {str(action.get("label")) for action in actions if isinstance(action, dict)}
                    if {"Open Validation Record", "Open Validation Output"}.issubset(labels):
                        diagnostics = payload
                        break
            await asyncio.sleep(2)

        assert diagnostics is not None, (
            "Session diagnostics never exposed both validation actions for coding run "
            f"(run_dir={coding_run_dir})"
        )

        actions = diagnostics.get("actions")
        assert isinstance(actions, list)
        action_map = {
            str(action.get("label")): action
            for action in actions
            if isinstance(action, dict) and action.get("label")
        }
        validation_record_path = Path(str(action_map["Open Validation Record"]["path"]))
        validation_output_path = Path(str(action_map["Open Validation Output"]["path"]))
        assert validation_record_path.exists(), f"Validation record missing: {validation_record_path}"
        assert validation_output_path.exists(), f"Validation output missing: {validation_output_path}"
        assert validation_output_path.stat().st_size > 0, f"Validation output empty: {validation_output_path}"

        detail_after_review = await _fetch_issue_detail(config.web_port, issue_number)
        steps_after_review = _steps_from_issue_detail(detail_after_review)
        has_review_step = any(
            step.get("event") == EventName.REVIEW_EXCHANGE_STARTED.value for step in steps_after_review
        )
        assert has_review_step, "Expected review_exchange.started step in issue detail"

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
        assert has_diagnostics_action, "Expected run-scoped diagnostics action in timeline steps"

    finally:
        if flow and pr_number:
            try:
                flow.close_pr(pr_number)
            except Exception:
                pass
        if flow and issue_number is not None:
            try:
                close_issue(repo_name, issue_number, "Closed by production-parity 4057 e2e cleanup")
            except Exception:
                pass
        if runtime is not None:
            await runtime.close()
