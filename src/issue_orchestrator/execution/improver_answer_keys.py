"""The answer keys a tournament grades against, one per frozen snapshot (#8001).

Keys come from hindsight and are NEVER written by the improver: the sealed
key written before a tournament's results (:meth:`seed_sealed`), and the
problems found later (by the operator, the tech lead or the coordinator)
that the snapshot's evidence already showed (:meth:`add`). Only the key
commands of ``improver tournament`` write here; an improver run has no
handle on this store, and no agent can read it (it is outside every heat's
evidence).

A hindsight item a person has not yet judged is a ``candidate``: it does not
score until :meth:`confirm`.

A hindsight item records ``observable_since``: when its evidence first
existed, and where that is read (#8972). An item first observable after the
snapshot was frozen would score arms as missing what their evidence could
not show, so it is never confirmed on that snapshot (:meth:`confirm` and a
confirmed :meth:`add` refuse it; a candidate is kept, and the command warns),
and a key that scores one is not graded (:meth:`scoring`). It can be moved
to a snapshot frozen later instead (:meth:`move`).
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..contracts.improver_tournament import AnswerKey, AnswerKeyItem, FrozenSnapshot, Observation, require_slug
from ..domain.improver_tournament import parse_sealed_key, unobservable

KEYS_DIRNAME = "keys"
#: Who may never write a key item.
_NOT_A_KEY_AUTHOR = frozenset({"improver", "the improver", "improver-agent"})


class AnswerKeyError(ValueError):
    pass


class FrozenSnapshots(Protocol):
    """Where a key's snapshot is read: when it was frozen."""

    def get(self, snapshot_id: str) -> FrozenSnapshot: ...


class FileAnswerKeyStore:
    def __init__(self, root: Path, snapshots: FrozenSnapshots) -> None:
        self._root = root / KEYS_DIRNAME
        self._snapshots = snapshots

    def path(self, snapshot_id: str) -> Path:
        return self._root / f"{require_slug(snapshot_id, 'a snapshot id')}.json"

    def get(self, snapshot_id: str) -> AnswerKey:
        path = self.path(snapshot_id)
        if not path.is_file():
            raise AnswerKeyError(f"no answer key for snapshot {snapshot_id!r}")
        return AnswerKey.model_validate_json(path.read_text(encoding="utf-8"))

    def seed_sealed(self, snapshot_id: str, markdown: str, *, sealed_at: datetime, added_by: str) -> AnswerKey:
        # A key grades one frozen snapshot: it is seeded only once that snapshot is.
        self._snapshots.get(snapshot_id)
        key = parse_sealed_key(markdown, snapshot_id=snapshot_id, added_at=sealed_at, added_by=_author(added_by))
        with self._locked():
            # Published only if no key is there: a sealed key is never replaced.
            temporary = self._temporary(key)
            try:
                os.link(temporary, self.path(snapshot_id))
            except FileExistsError:
                raise AnswerKeyError(f"snapshot {snapshot_id!r} already has a key; add hindsight items to it") from None
            finally:
                os.unlink(temporary)
        return key

    def add(self, item: AnswerKeyItem, *, snapshot_id: str) -> AnswerKey:
        """Add a hindsight item. A confirmed one must be observable at the
        snapshot's time; a candidate that is not is kept (see :meth:`unobservable`)."""
        if item.source != "hindsight":
            raise AnswerKeyError("only hindsight items are added; the sealed key is seeded once")
        if item.observable_since is None:
            raise AnswerKeyError(f"hindsight item {item.id!r} records observable_since: when its evidence first existed")
        _author(item.added_by)
        if item.status == "confirmed":
            self._require_observable(item, snapshot_id)
        with self._locked():
            key = self.get(snapshot_id)
            if any(existing.id == item.id for existing in key.items):
                raise AnswerKeyError(f"key item {item.id!r} exists")
            return self._write(key.model_copy(update={"items": (*key.items, item)}))

    def confirm(self, snapshot_id: str, item_id: str, *, by: str) -> AnswerKey:
        _author(by)
        with self._locked():
            key = self.get(snapshot_id)
            item = _item(key, item_id)
            self._require_observable(item, snapshot_id)
            return self._write(_replaced(key, item.model_copy(update={"status": "confirmed"})))

    def observe(self, snapshot_id: str, item_id: str, observation: Observation, *, by: str) -> AnswerKey:
        """Record (or correct) when a hindsight item's evidence first existed."""
        _author(by)
        with self._locked():
            key = self.get(snapshot_id)
            item = _item(key, item_id)
            if item.source != "hindsight":
                raise AnswerKeyError(f"key item {item_id!r} is sealed: its snapshot is its evidence")
            observed = item.model_copy(update={"observable_since": observation})
            if item.status == "confirmed":
                self._require_observable(observed, snapshot_id, remedy="move it to a snapshot frozen later")
            return self._write(_replaced(key, observed))

    def move(self, snapshot_id: str, item_id: str, *, to: str, by: str) -> AnswerKey:
        """Attach a hindsight item to the snapshot ``to`` instead (frozen once
        it was observable), as a candidate there: it is judged anew against
        that snapshot's evidence. Returns ``to``'s key."""
        _author(by)
        if to == snapshot_id:
            raise AnswerKeyError(f"key item {item_id!r} is already on snapshot {snapshot_id!r}")
        with self._locked():
            source, target = self.get(snapshot_id), self.get(to)
            item = _item(source, item_id)
            if item.source != "hindsight":
                raise AnswerKeyError(f"key item {item_id!r} is sealed with snapshot {snapshot_id!r}")
            self._require_observable(item, to)
            if any(existing.id == item_id for existing in target.items):
                raise AnswerKeyError(f"key item {item_id!r} exists on snapshot {to!r}")
            moved = item.model_copy(update={"status": "candidate"})
            # The target first: a crash between the writes leaves it on both, never on neither.
            updated = self._write(target.model_copy(update={"items": (*target.items, moved)}))
            self._write(source.model_copy(update={"items": tuple(i for i in source.items if i.id != item_id)}))
        return updated

    def unobservable(self, snapshot_id: str) -> dict[str, str]:
        """Each of the key's items the snapshot could not show: item id -> why."""
        return self._unobservable(self.get(snapshot_id))

    def scoring(self, snapshot_id: str) -> AnswerKey:
        """The key a tournament grades against: every item it scores was
        observable at the snapshot's time, or it is not graded at all."""
        key = self.get(snapshot_id)
        reasons = self._unobservable(key)
        refused = [reasons[i.id] for i in key.scored if i.id in reasons]
        if refused:
            raise AnswerKeyError(
                f"snapshot {snapshot_id!r}'s key scores items its evidence could not show: " + "; ".join(refused)
                + " (record observable_since with `key observe`, or move the item to a later snapshot with `key move`)"
            )
        return key

    def _unobservable(self, key: AnswerKey) -> dict[str, str]:
        frozen_at = self._snapshots.get(key.snapshot_id).taken_at
        reasons = {item.id: unobservable(item, frozen_at) for item in key.items}
        return {item_id: reason for item_id, reason in reasons.items() if reason is not None}

    def _require_observable(self, item: AnswerKeyItem, snapshot_id: str, *, remedy: str = "") -> None:
        reason = unobservable(item, self._snapshots.get(snapshot_id).taken_at)
        if reason is not None:
            hint = remedy or "keep it a candidate, or move it to a snapshot frozen later"
            raise AnswerKeyError(f"not confirmed on snapshot {snapshot_id!r}: {reason}; {hint}")

    def _write(self, key: AnswerKey) -> AnswerKey:
        os.replace(self._temporary(key), self.path(key.snapshot_id))
        return key

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Every key write, one at a time (a read-modify-write never loses one)."""
        self._root.mkdir(parents=True, exist_ok=True)
        with open(self._root / ".lock", "w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _temporary(self, key: AnswerKey) -> str:
        handle, temporary = tempfile.mkstemp(dir=self._root, prefix=".key-")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(key.model_dump_json(indent=2) + "\n")
        return temporary


def _item(key: AnswerKey, item_id: str) -> AnswerKeyItem:
    for item in key.items:
        if item.id == item_id:
            return item
    raise AnswerKeyError(f"no key item {item_id!r} on snapshot {key.snapshot_id!r}")


def _replaced(key: AnswerKey, item: AnswerKeyItem) -> AnswerKey:
    return key.model_copy(update={"items": tuple(item if i.id == item.id else i for i in key.items)})


def _author(name: str) -> str:
    if name.strip().casefold() in _NOT_A_KEY_AUTHOR:
        raise AnswerKeyError("the improver never writes its own answer key")
    return name


__all__ = ["KEYS_DIRNAME", "AnswerKeyError", "FileAnswerKeyStore", "FrozenSnapshots"]
