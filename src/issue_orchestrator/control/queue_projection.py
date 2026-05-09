"""Queue projection for computing and caching the in-scope issue snapshot.

This module extracts queue computation logic from the orchestrator,
following the principle that UI projections should be separate from
core orchestration logic.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..events import EventName
from ..ports.issue import Issue
from ..ports.repository_host import RepositoryHostError
from ..domain.models import OrchestratorState
from ..ports.event_sink import EventSink,  make_trace_event
from .queue_cache import QueueCache

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.repository_host import RepositoryHost

logger = logging.getLogger(__name__)


@dataclass
class QueueChange:
    """Represents a change in the issue queue."""
    added: list[Issue]
    removed: list[int]  # issue numbers
    total: int


class QueueProjection:
    """Projects the current in-scope issue cache from orchestrator state and repository.

    This class encapsulates the logic for computing the in-scope issue snapshot,
    detecting changes, and emitting events. It separates UI/projection
    concerns from core orchestration.
    """

    def __init__(
        self,
        config: "Config",
        repository_host: "RepositoryHost",
        events: EventSink,
        queue_cache_store: "QueueCacheStore | None" = None,
    ):
        self._config = config
        self._repository_host = repository_host
        self._events = events
        self._queue_cache_store = queue_cache_store

    def compute_queue(self, state: OrchestratorState) -> list[Issue]:
        """Compute the current in-scope issue snapshot.

        Args:
            state: Current orchestrator state

        Returns:
            List of in-scope issues used for queue and blocked projections
        """
        del state  # Scope refresh is repository-backed; runtime-only filters happen after fetch.
        from ..infra.audit import fetch_all_issues
        return fetch_all_issues(self._config, self._repository_host)

    def update_and_emit(self, state: OrchestratorState) -> QueueChange | None:
        """Update the queue cache and emit event if changed.

        Args:
            state: Current orchestrator state (will be mutated to update cached_queue_issues)

        Returns:
            QueueChange if queue changed, None otherwise
        """
        try:
            scope_issues = self.compute_queue(state)
            prior_issues = state.cached_scope_issues if state.cached_scope_issues else state.cached_queue_issues
            old_numbers = {i.number for i in prior_issues}
            new_numbers = {i.number for i in scope_issues}

            added_numbers = new_numbers - old_numbers
            removed_numbers = old_numbers - new_numbers

            # Capture stable issue keys before the cache is replaced (needed for
            # removal events — the Issue objects won't be available afterwards).
            old_key_by_number = {i.number: i.key.stable_id() for i in prior_issues}

            # Update state through queue cache abstraction.
            queue_cache = QueueCache(self._config, state, self._queue_cache_store)
            queue_cache.replace_from_refresh(scope_issues)
            if self._queue_cache_store is not None:
                queue_cache.save_snapshot()

            # Clear failed_this_cycle on cache refresh - GitHub now has the blocked-failed labels
            if state.failed_this_cycle:
                logger.info(
                    "[REFRESH] Clearing failed_this_cycle: %s (labels now synced from GitHub)",
                    state.failed_this_cycle,
                )
                state.failed_this_cycle.clear()

            if added_numbers or removed_numbers:
                added = [i for i in scope_issues if i.number in added_numbers]
                change = QueueChange(added=added, removed=list(removed_numbers), total=len(scope_issues))

                # Emit structured event with issue_key for consistent keying
                self._events.publish(make_trace_event(EventName.QUEUE_CHANGED, {
                    "added": [
                        {"number": i.number, "title": i.title, "issue_key": i.key.stable_id()}
                        for i in change.added
                    ],
                    "removed": [
                        {"number": num, "issue_key": old_key_by_number.get(num, str(num))}
                        for num in change.removed
                    ],
                    "total": change.total,
                }))
                logger.info("Queue changed: %d added, %d removed, %d total",
                           len(change.added), len(change.removed), change.total)
                return change

            return None

        except RepositoryHostError as e:
            logger.warning("Failed to update queue cache from repository host: %s", e)
            raise
        except Exception as e:
            logger.warning("Failed to update queue cache: %s", e)
            return None
