"""JSON-file implementation of :class:`PublishRetryLocatorStore`.

One tiny file at ``.issue-orchestrator/state/publish_retry_locators.json`` holds
a dict keyed by issue number. Only publish-failed issues have an entry, and the
entry is removed as soon as the retry succeeds, so the file stays small.

Durability is the whole point of this store: it is what keeps a publish-failed
issue retryable across an orchestrator restart. So it must not lose or hide
retry state on a crash:

- Writes go through :func:`atomic_write_json` (sibling tempfile + ``os.replace``)
  so a crash or ``kill -9`` mid-write can never leave a torn file. The
  in-memory index is only updated *after* the durable write succeeds, so a
  failed persist preserves the previous on-disk and in-memory state instead of
  silently dropping it.
- A file that exists but is unreadable or not valid JSON is treated as
  corruption and raised loudly (:class:`CorruptPublishRetryLocatorStoreError`)
  rather than silently degrading to "no locators". Silently returning an empty
  set would hide the Retry Publish action for every publish-failed issue with
  only a log warning — exactly the durability loss this store exists to prevent.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from ..domain.publish_retry import PublishRetryLocators
from ..infra.atomic_json import atomic_write_json

logger = logging.getLogger(__name__)


class CorruptPublishRetryLocatorStoreError(RuntimeError):
    """Raised when the on-disk locator file exists but cannot be read as JSON.

    Surfacing this loudly is deliberate: the file is the durable source of
    truth for which issues are still publish-retryable. Degrading a corrupt
    file to an empty set would silently strip that recovery affordance.
    """


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
            raise CorruptPublishRetryLocatorStoreError(
                f"Publish-retry locator store at {self._store_path} is unreadable "
                f"or corrupt ({exc}). Refusing to start with silently lost retry "
                "state; inspect and repair or remove the file to continue."
            ) from exc
        if not isinstance(data, dict):
            raise CorruptPublishRetryLocatorStoreError(
                f"Publish-retry locator store at {self._store_path} is not a JSON "
                f"object (found {type(data).__name__}); repair or remove the file."
            )
        return data

    def _persist(self, entries: dict[str, dict]) -> None:
        atomic_write_json(self._store_path, entries)

    def save(self, locators: PublishRetryLocators) -> None:
        with self._lock:
            # Build a candidate, persist it durably, and only then commit it to
            # the in-memory index. If the durable write fails, both the file and
            # the index keep their previous contents.
            candidate = dict(self._entries)
            candidate[str(locators.issue_number)] = locators.to_dict()
            self._persist(candidate)
            self._entries = candidate

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
            if str(issue_number) not in self._entries:
                return
            candidate = dict(self._entries)
            candidate.pop(str(issue_number), None)
            self._persist(candidate)
            self._entries = candidate
