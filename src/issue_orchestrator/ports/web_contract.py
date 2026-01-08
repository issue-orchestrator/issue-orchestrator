"""Contract defining what the web layer requires from the orchestrator.

This Protocol ensures MockOrchestratorForWeb and the real Orchestrator
stay in sync. The type checker catches drift, and runtime_checkable
allows isinstance() verification in tests.
"""

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from typing import Any

if TYPE_CHECKING:
    from issue_orchestrator.infra.config import Config
    from issue_orchestrator.domain.models import OrchestratorState
    from issue_orchestrator.events import EventHub


@runtime_checkable
class OrchestratorForWeb(Protocol):
    """What the web layer needs from an orchestrator instance."""

    state: "OrchestratorState"
    config: "Config"
    _shutdown_requested: bool

    def pause(self) -> None:
        """Pause the orchestrator."""
        ...

    def resume(self) -> None:
        """Resume the orchestrator."""
        ...

    def request_shutdown(self, force: bool = False) -> None:
        """Request graceful or forced shutdown."""
        ...

    def request_refresh(self, inflight_stable_ids: set[str] | None = None) -> None:
        """Request immediate issue refresh."""
        ...

    @property
    def event_hub(self) -> "EventHub":
        """Access to event hub for SSE subscriptions."""
        ...

    def get_failure_diagnosis(self, issue_number: int) -> dict[str, Any]:
        """Get failure diagnosis for a session.

        Returns diagnostic info for debugging failed sessions as a dict
        ready for JSON serialization.
        """
        ...
