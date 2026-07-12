"""JSON-file implementation of :class:`NeedsHumanClearStore`.

One tiny file at ``.issue-orchestrator/state/needs_human_label_clears.json``
holds the orchestrator-owed stale needs-human clears as a mapping of issue
number to :class:`NeedsHumanClearPhase` (``{"903": "confirmed"}``). An issue is
added ``PENDING`` before a recovered launch creates its terminal, promoted to
``CONFIRMED`` once the launch commits, and removed as soon as the owed removal
commits (or the launch fails), so the file stays small — usually empty.

Durability is the whole point: this record is the ONLY proof that the
orchestrator itself owns the pending clear, so a restart reconciles exactly the
obligations it initiated and never strips a legitimate operator/session
needs-human it does not own (#6771 round 7/8). It therefore must not lose or
hide that state on a crash:

- Writes go through :func:`atomic_write_json` (sibling tempfile + ``os.replace``)
  and the in-memory index is only updated *after* the durable write succeeds,
  so a failed persist preserves the previous on-disk and in-memory state
  instead of silently dropping it.
- A file that exists but is unreadable, not valid JSON, not an object, or holds
  a non-integer issue key or an unknown phase is treated as corruption and
  raised loudly (:class:`CorruptNeedsHumanClearStoreError`) rather than silently
  degrading to "nothing owed". Silently returning an empty set would abandon a
  stale needs-human label on a running investigation forever — the exact
  invariant this store exists to protect.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from ..infra.atomic_json import atomic_write_json
from ..ports.needs_human_clear_store import NeedsHumanClearPhase

logger = logging.getLogger(__name__)


class CorruptNeedsHumanClearStoreError(RuntimeError):
    """Raised when the on-disk file is not a JSON object of issue->phase entries.

    Surfacing this loudly is deliberate: the file is the durable proof of which
    clears the orchestrator still owes. Degrading a corrupt file to an empty set
    would silently abandon that recovery obligation.
    """


class JsonNeedsHumanClearStore:
    """Persist orchestrator-owed needs-human clears as one issue->phase JSON object."""

    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path
        self._lock = threading.Lock()
        self._phases: dict[int, NeedsHumanClearPhase] = self._load()

    def _load(self) -> dict[int, NeedsHumanClearPhase]:
        if not self._store_path.exists():
            return {}
        try:
            data = json.loads(self._store_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CorruptNeedsHumanClearStoreError(
                f"Needs-human clear store at {self._store_path} is unreadable or "
                f"corrupt ({exc}). Refusing to start with silently lost clear "
                "provenance; inspect and repair or remove the file to continue."
            ) from exc
        if not isinstance(data, dict):
            raise CorruptNeedsHumanClearStoreError(
                f"Needs-human clear store at {self._store_path} is not a JSON object "
                f"(found {type(data).__name__}); repair or remove the file."
            )
        phases: dict[int, NeedsHumanClearPhase] = {}
        for key, value in data.items():
            phases[self._parse_key(key)] = self._parse_phase(value)
        return phases

    def _parse_key(self, key: object) -> int:
        try:
            return int(key)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise CorruptNeedsHumanClearStoreError(
                f"Needs-human clear store at {self._store_path} has a non-integer "
                f"issue key {key!r}; repair or remove the file."
            ) from exc

    def _parse_phase(self, value: object) -> NeedsHumanClearPhase:
        try:
            return NeedsHumanClearPhase(value)
        except ValueError as exc:
            raise CorruptNeedsHumanClearStoreError(
                f"Needs-human clear store at {self._store_path} has an unknown clear "
                f"phase {value!r}; repair or remove the file."
            ) from exc

    def _write_and_commit(self, candidate: dict[int, NeedsHumanClearPhase]) -> None:
        # Persist durably first, then commit the in-memory index; a failed write
        # leaves both the file and the index at their prior contents.
        atomic_write_json(
            self._store_path, {str(n): p.value for n, p in candidate.items()}
        )
        self._phases = candidate

    def record_pending(self, issue_number: int) -> None:
        with self._lock:
            # Present in any phase => keep it (never downgrade CONFIRMED).
            if issue_number in self._phases:
                return
            self._write_and_commit(
                {**self._phases, issue_number: NeedsHumanClearPhase.PENDING}
            )

    def confirm(self, issue_number: int) -> None:
        with self._lock:
            if self._phases.get(issue_number) is NeedsHumanClearPhase.CONFIRMED:
                return
            self._write_and_commit(
                {**self._phases, issue_number: NeedsHumanClearPhase.CONFIRMED}
            )

    def withdraw(self, issue_number: int) -> None:
        with self._lock:
            if issue_number not in self._phases:
                return
            self._write_and_commit(
                {n: p for n, p in self._phases.items() if n != issue_number}
            )

    def pending_issue_numbers(self) -> list[int]:
        with self._lock:
            return [
                n for n, p in self._phases.items() if p is NeedsHumanClearPhase.PENDING
            ]

    def confirmed_issue_numbers(self) -> list[int]:
        with self._lock:
            return [
                n for n, p in self._phases.items() if p is NeedsHumanClearPhase.CONFIRMED
            ]
