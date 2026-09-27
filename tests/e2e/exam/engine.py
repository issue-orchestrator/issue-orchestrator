"""Run an exam engine: its config, its process, and its control surfaces.

The checkout it runs from is :mod:`tests.e2e.exam.engine_checkout`.
"""

from __future__ import annotations

import copy
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.config import Config

from tests.e2e.exam.engine_checkout import EngineCheckout
from tests.e2e.exam.agents import (
    CODER_LABEL,
    HELD_CODER_LABEL,
    REVIEWER_LABEL,
    TECH_LEAD_LABEL,
    shim_command,
)
from tests.e2e.fixtures import OrchestratorProcess, fetch_gh_audit_report, find_free_port
from tests.e2e.fixtures.orchestrator_process import merge_config_overlay
from tests.e2e.fixtures.inflight_tracker import control_api_headers
from tests.e2e.flows import OrchestratorRuntime, start_orchestrator_runtime

logger = logging.getLogger(__name__)

TECH_LEAD_PROMPT = Path("repo-specific") / "prompts" / "tech-lead.md"

#: Agent and session limit for work held mid-flight (Case U): past the base
#: engine's run-up, its stop and the candidate's restart window.
HELD_SESSION_TIMEOUT_MINUTES = 45

#: Settings every exam engine runs with, under each case's own overlay.
#: Session interactions answer the startup screens real agents open on — the
#: rules #7299 fixed (Claude Code's "Quick safety check" with "No, exit"
#: highlighted, Codex's "Folder access"). They are off by default; with them
#: off, the exam's first real tech lead sat 35 minutes on the trust dialog of
#: its fresh /tmp worktree with no event and no log line.
EXAM_BASE_OVERLAY: Mapping[str, Any] = {
    "execution": {"session_interactions": {"enabled": True}},
}


def exam_config(
    base: Config,
    *,
    checkout: EngineCheckout,
    run_label: str,
    reviewer_exchange_fault: str,
    tech_lead_model: str | None = None,
    release_file: Path | None = None,
) -> Config:
    """The e2e session config, pointed at the checkout, with exam agents.

    With ``release_file``, work is held mid-flight until the file exists:
    every review waits, and ``HELD_CODER_LABEL`` is a coder that waits before
    committing (``CODER_LABEL`` still codes at once, so its PR can reach a
    held review).
    """
    config = copy.deepcopy(base)
    config.repo_root = checkout.root
    config.control_api_port = find_free_port()
    config.web_port = find_free_port()
    config.filtering.label = run_label
    config.e2e_pr_labels = [run_label, "io-e2e-test-data"]
    config.max_concurrent_sessions = 2
    config.queue_refresh_seconds = 30
    # A held session must outlive the base engine's stop and the candidate's
    # restart window; nothing else in the exam needs more than ten minutes.
    held_minutes = HELD_SESSION_TIMEOUT_MINUTES if release_file is not None else None
    config.session_timeout_minutes = held_minutes or 10
    config.code_review_agent = REVIEWER_LABEL
    config.tech_lead_review_agent = TECH_LEAD_LABEL if tech_lead_model else None
    prompt = checkout.root / "tests" / "e2e" / "fixtures" / "prompts" / "simple_task.md"
    config.agents = {
        CODER_LABEL: AgentConfig(
            prompt_path=prompt,
            timeout_minutes=3,
            model="sonnet",
            command=shim_command("coder"),
            meta_agent="claude-code",
            ai_system="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
            reviewer=REVIEWER_LABEL,
        ),
        REVIEWER_LABEL: AgentConfig(
            prompt_path=prompt,
            timeout_minutes=held_minutes or 1,
            model="sonnet",
            command=shim_command(
                "reviewer", exchange_fault=reviewer_exchange_fault, hold_until=release_file
            ),
            meta_agent="claude-code",
            ai_system="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
        ),
    }
    if release_file is not None:
        config.agents[HELD_CODER_LABEL] = AgentConfig(
            prompt_path=prompt,
            timeout_minutes=HELD_SESSION_TIMEOUT_MINUTES,
            model="sonnet",
            command=shim_command("coder", hold_until=release_file),
            meta_agent="claude-code",
            ai_system="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
            reviewer=REVIEWER_LABEL,
        )
    if tech_lead_model:
        config.agents[TECH_LEAD_LABEL] = AgentConfig(
            prompt_path=checkout.root / TECH_LEAD_PROMPT,
            timeout_minutes=45,
            model=tech_lead_model,
            provider="claude-code",
            ai_system="claude-code",
            # Production's tech-lead agent (the operator's io/porchpin configs).
            provider_args={"effort": "xhigh", "permission_mode": "bypassPermissions"},
            initial_prompt=(
                "Tech Lead review for issue #{issue_number}: {issue_title}. Follow the"
                " instructions in {prompt}. When done, use coding-done to report completion."
            ),
        )
    return config


class ExamEngine:
    """One running engine plus its control surfaces."""

    def __init__(
        self,
        config: Config,
        checkout: EngineCheckout,
        *,
        overlay: Mapping[str, Any],
    ) -> None:
        self.config = config
        self.checkout = checkout
        self.process = OrchestratorProcess(
            config,
            checkout.root,
            source_root=checkout.root,
            config_overlay=merge_config_overlay(EXAM_BASE_OVERLAY, overlay),
        )
        self._runtime: OrchestratorRuntime | None = None

    def write_config(self) -> Path:
        """Write the exact config file :meth:`start` launches the engine with."""
        return self.process.write_e2e_config()

    @property
    def runtime(self) -> OrchestratorRuntime:
        if self._runtime is None:
            raise RuntimeError("engine not started")
        return self._runtime

    async def start(self) -> OrchestratorRuntime:
        run_label = self.config.filtering.label
        if not run_label:
            # Without its run label the engine would work every open issue.
            raise RuntimeError("exam engine config has no filtering.label (the run label)")
        try:
            self._runtime = await start_orchestrator_runtime(
                self.process,
                self.config.control_api_port,
                max_issues=10,
                extra_args=["--label", run_label],
            )
        except BaseException:
            # The process may be up even though startup failed (e.g. the
            # control API never became ready). It is ours: stop it before the
            # caller removes the checkout it runs from.
            self.process.stop()
            raise
        # Keep every event for the report, not the watcher's default 200.
        self._runtime.watcher.view.set_diag_limits(100_000)
        return self._runtime

    async def close(self) -> None:
        if self._runtime is not None:
            await self._runtime.close()
        elif self.process.is_running():
            self.process.stop()

    def is_running(self) -> bool:
        return self.process.is_running()

    def active_session_issues(self) -> tuple[tuple[str, int], ...]:
        """``(session_name, issue_number)`` of every session the engine runs now."""
        status = self._get_json(self.config.control_api_port, "/api/status")
        sessions = status.get("sessions")
        if not isinstance(sessions, list):
            raise RuntimeError(f"/api/status has no sessions list: {status!r}")
        return tuple(
            (str(entry["session_name"]), int(entry["issue_number"])) for entry in sessions
        )

    def progress_events(self) -> int:
        """Events about some work item. Tick, plan and fetch events carry no
        ``issue_key`` and fire every tick, so they never count as progress."""
        return sum(1 for event in self.runtime.watcher.view.global_events if event.get("issue_key"))

    def event_history(self) -> list[dict[str, Any]]:
        """Every event this process has buffered, from its first.

        The watcher only sees events published after it connects; a
        restart's startup work happens before that.
        """
        payload = self._get_json(self.config.control_api_port, "/api/events_since?after=0")
        events = payload.get("events")
        if not isinstance(events, list):
            raise RuntimeError(f"/api/events_since has no events list: {payload!r}")
        return events

    def gh_audit_report(self) -> dict[str, Any]:
        """This process's GitHub calls so far, by command."""
        report = fetch_gh_audit_report(self.config.control_api_port)
        if report is None:
            raise RuntimeError("the engine returned no gh_audit report")
        return report

    def pending_work(self) -> int:
        """Reviews and reworks the engine has queued but not launched."""
        status = self._get_json(self.config.control_api_port, "/api/status")
        total = 0
        for key in ("pending_reviews", "pending_reworks"):
            count = status.get(key)
            if isinstance(count, bool) or not isinstance(count, int):
                raise RuntimeError(f"/api/status {key} is not an int: {status!r}")
            total += count
        return total

    def active_sessions(self) -> int:
        status = self._get_json(self.config.control_api_port, "/api/status")
        value = status.get("active_sessions")
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"/api/status active_sessions is not an int: {status!r}")
        return value

    def _get_json(self, port: int, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"http://localhost:{port}{path}", headers=control_api_headers()
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
