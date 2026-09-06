"""Non-reentrant record leases and private retained claim ownership."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock, Thread, current_thread
from types import TracebackType

from ..domain.validated_work import require_text
from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_execution import RecordExecutionBusy, RecordExecutionToken
from ..ports.validated_work_execution import RecordExecutionLease
from ..ports.validated_work_store import ValidatedWorkStore


@dataclass
class _Entry:
    lock: Lock = field(default_factory=Lock)
    token: RecordExecutionToken | None = None
    thread: Thread | None = None
    claim: ValidatedWorkClaim | None = None


class _Lease(RecordExecutionLease):
    def __init__(
        self,
        enter: Callable[[], RecordExecutionToken],
        leave: Callable[[], None],
    ) -> None:
        self.__enter = enter
        self.__leave = leave

    def __enter__(self) -> RecordExecutionToken:
        return self.__enter()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.__leave()


class LocalValidatedWorkExecutionOwner:
    """Reserve immediately, authenticate by identity and retain refused releases.

    Registry entries live for this owner's lifetime: removing them would allow
    two locks for the same record. This owner never acquires a durable claim or
    runs background work; callers supply the claim within their admitted lease.
    """

    def __init__(self, store: ValidatedWorkStore) -> None:
        self._store = store
        self._pid = os.getpid()
        self._registry_lock = Lock()
        self._entries: dict[str, _Entry] = {}
        self._active: dict[RecordExecutionToken, tuple[str, _Entry]] = {}

    def try_enter(self, record_id: str) -> RecordExecutionLease | RecordExecutionBusy:
        require_text(record_id, "record_id")
        self._require_process()
        with self._registry_lock:
            entry = self._entries.setdefault(record_id, _Entry())
            if not entry.lock.acquire(blocking=False):
                return RecordExecutionBusy(record_id)
        thread = current_thread()
        token = object.__new__(RecordExecutionToken)
        entered = False
        closed = False

        def enter() -> RecordExecutionToken:
            nonlocal entered
            self._require_process()
            if current_thread() is not thread or entered or closed:
                raise RuntimeError("execution lease cannot be reused or transferred")
            entered = True
            with self._registry_lock:
                entry.token, entry.thread = token, thread
                self._active[token] = (record_id, entry)
            return token

        def leave() -> None:
            nonlocal closed
            self.require_active(token, record_id)
            with self._registry_lock:
                del self._active[token]
                entry.token, entry.thread = None, None
                closed = True
                entry.lock.release()

        return _Lease(enter, leave)

    def _require_process(self) -> None:
        if os.getpid() != self._pid:
            raise RuntimeError("execution owner cannot cross a process boundary")

    def _entry(self, token: RecordExecutionToken) -> tuple[str, _Entry]:
        self._require_process()
        with self._registry_lock:
            active = self._active.get(token)
            if (
                active is None
                or active[1].token is not token
                or active[1].thread is not current_thread()
            ):
                raise RuntimeError("inactive or foreign execution token")
            return active

    def require_active(self, token: RecordExecutionToken, record_id: str) -> None:
        active_record, _ = self._entry(token)
        if active_record != record_id:
            raise RuntimeError("execution token belongs to another record")

    def claim(self, token: RecordExecutionToken) -> ValidatedWorkClaim | None:
        return self._entry(token)[1].claim

    def remember_claim(
        self, token: RecordExecutionToken, claim: ValidatedWorkClaim
    ) -> None:
        record_id, entry = self._entry(token)
        if claim.record_id != record_id:
            raise ValueError("claim belongs to another record")
        if entry.claim is not None and entry.claim is not claim:
            raise RuntimeError("retained claim must be relinquished before replacement")
        entry.claim = claim

    def relinquish(self, token: RecordExecutionToken) -> bool:
        _, entry = self._entry(token)
        if entry.claim is None:
            return True
        if not self._store.relinquish_claim(entry.claim):
            return False
        entry.claim = None
        return True
