"""Shared mocks for web contract tests."""

from pathlib import Path

from issue_orchestrator.domain.models import AgentConfig, OrchestratorState
from issue_orchestrator.events import EventHub
from issue_orchestrator.infra.config import Config


class _MockDeps:
    """Minimal deps stub for web mock orchestrators."""

    provider_resilience = None


class MockOrchestratorForWeb:
    """Minimal orchestrator mock that satisfies the web contract protocol."""

    def __init__(self) -> None:
        self.state = OrchestratorState(
            active_sessions=[],
            session_history=[],
            completed_today=[],
            paused=False,
            priority_queue=[],
            startup_status="complete",
            startup_message="",
            cached_queue_issues=[],
            pending_reviews=[],
            dependency_problems={},
        )
        self.config = self._create_mock_config()
        self.deps = _MockDeps()
        self._shutdown_requested = False
        self._event_hub = EventHub()

    @property
    def event_hub(self) -> EventHub:
        return self._event_hub

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested

    @shutdown_requested.setter
    def shutdown_requested(self, value: bool) -> None:
        self._shutdown_requested = value

    def _create_mock_config(self) -> Config:
        config = Config()
        config.repo = "test/repo"
        config.max_concurrent_sessions = 3
        config.queue_refresh_seconds = 600
        config.ui_mode = "web"
        config.web_port = 8080
        config.config_path = Path("/tmp/config.yaml")
        config.repo_root = Path("/tmp/repo")
        config.worktree_base = Path("/tmp")
        config.filtering.label = None
        config.filter_milestone = None
        config.agents = {
            "agent:web": AgentConfig(
                prompt_path=Path("/tmp/prompt.txt"),
                model="sonnet",
                timeout_minutes=45,
            )
        }
        return config

    def pause(self) -> None:
        self.state.paused = True

    def resume(self) -> None:
        self.state.paused = False

    def request_shutdown(self, force: bool = False) -> None:
        _ = force
        self._shutdown_requested = True

    def request_refresh(self, inflight_stable_ids: set[str] | None = None) -> None:
        _ = inflight_stable_ids

    def get_failure_diagnosis(self, issue_number: int) -> dict[str, object]:
        return {
            "issue_number": issue_number,
            "ai_system": "unknown",
            "permission_mode": "default",
            "worktree_path": None,
            "log_path": None,
            "log_exists": False,
            "log_context": None,
            "history_status": None,
            "history_reason": None,
            "warnings": [],
            "suggestions": [],
        }
