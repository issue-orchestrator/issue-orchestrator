"""JSON-file implementation of :class:`NeedsHumanClearStore`.

One tiny file at ``.issue-orchestrator/state/needs_human_label_clears.json``
holds the issue numbers whose orchestrator-owned stale needs-human removal has
not yet committed. An issue is added when a recovered launch's stale-label
removal fails and removed as soon as the retry succeeds, so the file stays
small — usually empty.

Durability is the whole point: this record is the ONLY proof that the
orchestrator itself owns the pending clear, so a restart reconciles exactly the
incomplete removals it initiated and never strips a legitimate operator/session
needs-human it does not own (#6771 round 7). It therefore must not lose or hide
that state on a crash:

- Writes go through :func:`atomic_write_json` (sibling tempfile + ``os.replace``)
  and the in-memory index is only updated *after* the durable write succeeds,
  so a failed persist preserves the previous on-disk and in-memory state
  instead of silently dropping it.
- A file that exists but is unreadable, not valid JSON, or not a list of issue
  numbers is treated as corruption and raised loudly
  (:class:`CorruptNeedsHumanClearStoreError`) rather than silently degrading to
  "nothing owed". Silently returning an empty set would abandon a stale
  needs-human label on a running investigation forever — the exact invariant
  this store exists to protect.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from ..infra.atomic_json import atomic_write_json

logger = logging.getLogger(__name__)


class CorruptNeedsHumanClearStoreError(RuntimeError):
    """Raised when the on-disk file exists but is not a JSON list of issue numbers.

    Surfacing this loudly is deliberate: the file is the durable proof of which
    issues the orchestrator still owes a stale needs-human removal for.
    Degrading a corrupt file to an empty set would silently abandon that
    recovery obligation.
    """


class JsonNeedsHumanClearStore:
    """Persist orchestrator-owed needs-human clears as one JSON array of issue numbers."""

    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path
        self._lock = threading.Lock()
        self._pending: list[int] = self._load()

    def _load(self) -> list[int]:
        if not self._store_path.exists():
            return []
        try:
            data = json.loads(self._store_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CorruptNeedsHumanClearStoreError(
                f"Needs-human clear store at {self._store_path} is unreadable or "
                f"corrupt ({exc}). Refusing to start with silently lost clear "
                "provenance; inspect and repair or remove the file to continue."
            ) from exc
        if not isinstance(data, list):
            raise CorruptNeedsHumanClearStoreError(
                f"Needs-human clear store at {self._store_path} is not a JSON array "
                f"(found {type(data).__name__}); repair or remove the file."
            )
        pending: list[int] = []
        for entry in data:
            # bool is an int subclass; a stray true/false is corruption, not an issue.
            if not isinstance(entry, int) or isinstance(entry, bool):
                raise CorruptNeedsHumanClearStoreError(
                    f"Needs-human clear store at {self._store_path} has a non-integer "
                    f"issue entry {entry!r}; repair or remove the file."
                )
            if entry not in pending:
                pending.append(entry)
        return pending

    def _persist(self, pending: list[int]) -> None:
        atomic_write_json(self._store_path, pending)

    def record(self, issue_number: int) -> None:
        with self._lock:
            if issue_number in self._pending:
                return
            # Persist durably first, then commit the in-memory index; a failed
            # write leaves both the file and the index at their prior contents.
            candidate = self._pending + [issue_number]
            self._persist(candidate)
            self._pending = candidate

    def discard(self, issue_number: int) -> None:
        with self._lock:
            if issue_number not in self._pending:
                return
            candidate = [n for n in self._pending if n != issue_number]
            self._persist(candidate)
            self._pending = candidate

    def pending_issue_numbers(self) -> list[int]:
        with self._lock:
            return list(self._pending)
