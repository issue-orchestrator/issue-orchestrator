"""Queue projection for computing and caching available issues.

This module extracts queue computation logic from the orchestrator,
following the principle that UI projections should be separate from
core orchestration logic.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..events import EventName
from ..ports.issue import Issue
from ..models import OrchestratorState
from ..ports.event_sink import EventSink, TraceEvent

if TYPE_CHECKING:
    from ..config import Config
    from ..ports.repository_host import RepositoryHost

logger = logging.getLogger(__name__)


@dataclass
class QueueChange:
    """Represents a change in the issue queue."""
    added: list[Issue]
    removed: list[int]  # issue numbers
    total: int


class QueueProjection:
    """Projects the current queue state from orchestrator state and repository.

    This class encapsulates the logic for computing the issue queue,
    detecting changes, and emitting events. It separates UI/projection
    concerns from core orchestration.
    """

    def __init__(self, config: "Config", repository_host: "RepositoryHost", events: EventSink):
        self._config = config
        self._repository_host = repository_host
        self._events = events

    def compute_queue(self, state: OrchestratorState) -> list[Issue]:
        """Compute the current issue queue.

        Args:
            state: Current orchestrator state

        Returns:
            List of issues available in the queue
        """
        from ..audit import get_queue_issues
        return get_queue_issues(self._config, state, issue_tracker=self._repository_host)

    def update_and_emit(self, state: OrchestratorState) -> QueueChange | None:
        """Update the queue cache and emit event if changed.

        Args:
            state: Current orchestrator state (will be mutated to update cached_queue_issues)

        Returns:
            QueueChange if queue changed, None otherwise
        """
        try:
            queue_issues = self.compute_queue(state)
            old_numbers = {i.number for i in state.cached_queue_issues}
            new_numbers = {i.number for i in queue_issues}

            added_numbers = new_numbers - old_numbers
            removed_numbers = old_numbers - new_numbers

            # Update state
            state.cached_queue_issues = queue_issues

            if added_numbers or removed_numbers:
                added = [i for i in queue_issues if i.number in added_numbers]
                change = QueueChange(added=added, removed=list(removed_numbers), total=len(queue_issues))

                # Emit structured event
                self._events.publish(TraceEvent(EventName.QUEUE_CHANGED, {
                    "added": [{"number": i.number, "title": i.title} for i in change.added],
                    "removed": [{"number": num} for num in change.removed],
                    "total": change.total,
                }))
                logger.info("Queue changed: %d added, %d removed, %d total",
                           len(change.added), len(change.removed), change.total)
                return change

            return None

        except Exception as e:
            logger.warning("Failed to update queue cache: %s", e)
            return None
