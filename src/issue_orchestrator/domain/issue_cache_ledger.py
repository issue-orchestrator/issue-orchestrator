"""One serial order for the engine's cached issues (#8113).

The engine caches the in-scope issues (``OrchestratorState.cached_scope_issues``
and ``cached_queue_issues``), and three kinds of writer change them, from
whichever thread they run on:

* a refresh installs what a repository read of the issue list returned;
* an upsert installs one issue a repository read returned;
* the repository adapter reports a label write the engine just made.

The network does not order a read against the write it races. A refresh can
read an issue, a label write can land and reach the cache, and only then the
refresh commits the issue as it was read, putting back the label the write
removed. A lock around each mutation cannot prevent that, because the read
happened before the write.

So the ledger is the cache's one serial point, and it also keeps the order
across the read. Every mutation runs under its lock. A read whose result will
be committed opens a fetch *before* it reads. A label write recorded while a
fetch is open is kept until that fetch closes. Committing the fetch first
re-applies, in order, every write recorded after it opened, so what it read
can never undo a later verified write. A write recorded while no fetch is open
needs no keeping: any fetch opened afterwards reads after it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class LabelWrite:
    """A label write the engine made and the repository host verified."""

    issue_number: int
    label: str
    present: bool


@dataclass(frozen=True)
class IssueFetchEpoch:
    """How many label writes the ledger had recorded when a fetch opened."""

    sequence: int


class IssueCacheLedger:
    """The serial order of every change to one state's cached issues."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sequence = 0
        self._open_fetches: Counter[int] = Counter()
        self._writes: list[tuple[int, LabelWrite]] = []

    @contextmanager
    def serialised(self) -> Generator[None]:
        """Hold the cache for one mutation. Reentrant, so one mutation may compose another."""
        with self._lock:
            yield

    def open_fetch(self) -> IssueFetchEpoch:
        """A read is about to start: keep every write recorded from now until it closes."""
        with self._lock:
            self._open_fetches[self._sequence] += 1
            return IssueFetchEpoch(self._sequence)

    def close_fetch(self, epoch: IssueFetchEpoch) -> None:
        """The read was committed or abandoned: its writes need no longer be kept for it."""
        with self._lock:
            if self._open_fetches[epoch.sequence] <= 0:
                raise ValueError(f"fetch opened at write {epoch.sequence} is not open")
            self._open_fetches[epoch.sequence] -= 1
            if not self._open_fetches[epoch.sequence]:
                del self._open_fetches[epoch.sequence]
            oldest_open = min(self._open_fetches, default=self._sequence)
            self._writes = [(sequence, write) for sequence, write in self._writes if sequence > oldest_open]

    def record(self, write: LabelWrite) -> None:
        """A write landed. Kept only while a fetch that opened before it is still open."""
        with self._lock:
            self._sequence += 1
            if self._open_fetches:
                self._writes.append((self._sequence, write))

    def writes_since(self, epoch: IssueFetchEpoch) -> tuple[LabelWrite, ...]:
        """The writes recorded after ``epoch``, oldest first: what its read may predate."""
        with self._lock:
            return tuple(write for sequence, write in self._writes if sequence > epoch.sequence)

    @property
    def kept_writes(self) -> int:
        """How many writes are being kept for open fetches."""
        with self._lock:
            return len(self._writes)


__all__ = ["IssueCacheLedger", "IssueFetchEpoch", "LabelWrite"]
