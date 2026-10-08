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
from ..domain.improver_tournament import (
    NotMovable,
    Unobservable,
    is_same_move,
    moved_item,
    parse_sealed_key,
    unobservable,
    unscoreable,
)

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
        self._require_scoreable(item, snapshot_id)
        with self._locked():
            key = self.get(snapshot_id)
            if any(existing.id == item.id for existing in key.items):
                raise AnswerKeyError(f"key item {item.id!r} exists")
            return self._write(key.model_copy(update={"items": (*key.items, item)}))

    def confirm(self, snapshot_id: str, item_id: str, *, by: str) -> AnswerKey:
        _author(by)
        with self._locked():
            key = self.get(snapshot_id)
            confirmed = _item(key, item_id).model_copy(update={"status": "confirmed"})
            self._require_scoreable(confirmed, snapshot_id)
            return self._write(_replaced(key, confirmed))

    def observe(self, snapshot_id: str, item_id: str, observation: Observation, *, by: str) -> AnswerKey:
        """Record (or correct) when a hindsight item's evidence first existed.
        A confirmed item observable only later is moved instead (:meth:`move`)."""
        _author(by)
        with self._locked():
            key = self.get(snapshot_id)
            observed = _hindsight(key, item_id).model_copy(update={"observable_since": observation})
            self._require_scoreable(observed, snapshot_id)
            return self._write(_replaced(key, observed))

    def move(
        self, snapshot_id: str, item_id: str, *, to: str, by: str, observation: Observation | None = None
    ) -> AnswerKey:
        """Attach a hindsight item to the snapshot ``to`` instead (one frozen
        once it was observable), as a candidate there: it is judged anew
        against that snapshot's evidence. ``observation`` records (or
        corrects) when it was observable as it moves. Returns ``to``'s key.

        A move interrupted between its two writes leaves the item on both;
        the same move again finishes it."""
        _author(by)
        if to == snapshot_id:
            raise AnswerKeyError(f"key item {item_id!r} is already on snapshot {snapshot_id!r}")
        with self._locked():
            source, target = self.get(snapshot_id), self.get(to)
            try:
                moved = moved_item(_hindsight(source, item_id), frozen_at=self._frozen_at(to), observation=observation)
            except NotMovable as refused:
                raise AnswerKeyError(f"not moved to snapshot {to!r}: {refused}") from refused
            already = next((i for i in target.items if i.id == item_id), None)
            if already is None:
                # The target first: a crash between the writes leaves it on both, never on neither.
                target = self._write(target.model_copy(update={"items": (*target.items, moved)}))
            elif not is_same_move(already, moved):
                raise AnswerKeyError(f"snapshot {to!r} has another key item {item_id!r}")
            self._write(source.model_copy(update={"items": tuple(i for i in source.items if i.id != item_id)}))
        return target

    def unobservable(self, snapshot_id: str) -> tuple[Unobservable, ...]:
        """Each of the key's items the snapshot could not show, and why."""
        key = self.get(snapshot_id)
        frozen_at = self._frozen_at(snapshot_id)
        return tuple(
            Unobservable(item.id, reason) for item in key.items
            if (reason := unobservable(item, frozen_at)) is not None
        )

    def scoring(self, snapshot_id: str) -> AnswerKey:
        """The key a tournament grades against: every item it scores was
        observable at the snapshot's time, or it is not graded at all."""
        key = self.get(snapshot_id)
        frozen_at = self._frozen_at(snapshot_id)
        refused = [reason for item in key.items if (reason := unscoreable(item, frozen_at)) is not None]
        if refused:
            raise AnswerKeyError(
                f"snapshot {snapshot_id!r}'s key scores items its evidence could not show: " + "; ".join(refused)
                + " (record observable_since with `key observe`, or attach the item to a later snapshot with `key move`)"
            )
        return key

    def _require_scoreable(self, item: AnswerKeyItem, snapshot_id: str) -> None:
        reason = unscoreable(item, self._frozen_at(snapshot_id))
        if reason is not None:
            raise AnswerKeyError(
                f"not confirmed on snapshot {snapshot_id!r}: {reason}; it stays a candidate there,"
                " or `key move` attaches it to a snapshot frozen later"
            )

    def _frozen_at(self, snapshot_id: str) -> datetime:
        return self._snapshots.get(snapshot_id).taken_at

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


def _hindsight(key: AnswerKey, item_id: str) -> AnswerKeyItem:
    item = _item(key, item_id)
    if item.source != "hindsight":
        raise AnswerKeyError(f"key item {item_id!r} is sealed with snapshot {key.snapshot_id!r}: the snapshot is its evidence")
    return item


def _replaced(key: AnswerKey, item: AnswerKeyItem) -> AnswerKey:
    return key.model_copy(update={"items": tuple(item if i.id == item.id else i for i in key.items)})


def _author(name: str) -> str:
    if name.strip().casefold() in _NOT_A_KEY_AUTHOR:
        raise AnswerKeyError("the improver never writes its own answer key")
    return name


__all__ = ["KEYS_DIRNAME", "AnswerKeyError", "FileAnswerKeyStore", "FrozenSnapshots"]
