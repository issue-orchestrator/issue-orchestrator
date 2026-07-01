"""JSON-file implementation of :class:`PublishRetryLocatorStore`.

One tiny file at ``.issue-orchestrator/state/publish_retry_locators.json`` holds
a dict keyed by issue number. Only publish-failed issues have an entry, and the
entry is removed as soon as the retry succeeds, so the file stays small.

A corrupt or unreadable file degrades to "no locators" rather than crashing the
orchestrator: retry-publish is a manual recovery action, so losing a locator
means the button is unavailable, not that the engine stops.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from ..domain.publish_retry import PublishRetryLocators

logger = logging.getLogger(__name__)


class JsonPublishRetryLocatorStore:
    """Persist publish-retry locators to a single JSON file keyed by issue."""

    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self._store_path.exists():
            return {}
        try:
            data = json.loads(self._store_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Failed to load publish-retry locators from %s: %s",
                self._store_path,
                exc,
            )
            return {}
        return data if isinstance(data, dict) else {}

    def _persist(self) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        self._store_path.write_text(json.dumps(self._entries, indent=2, default=str))

    def save(self, locators: PublishRetryLocators) -> None:
        with self._lock:
            self._entries[str(locators.issue_number)] = locators.to_dict()
            self._persist()

    def get(self, issue_number: int) -> PublishRetryLocators | None:
        with self._lock:
            raw = self._entries.get(str(issue_number))
        if raw is None:
            return None
        try:
            return PublishRetryLocators.from_dict(raw)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning(
                "Discarding invalid publish-retry locators for issue #%s: %s",
                issue_number,
                exc,
            )
            return None

    def clear(self, issue_number: int) -> None:
        with self._lock:
            if self._entries.pop(str(issue_number), None) is not None:
                self._persist()
