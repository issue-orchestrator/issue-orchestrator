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
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ..contracts.improver_tournament import AnswerKey, AnswerKeyItem, require_slug
from ..domain.improver_tournament import parse_sealed_key

KEYS_DIRNAME = "keys"
#: Who may never write a key item.
_NOT_A_KEY_AUTHOR = frozenset({"improver", "the improver", "improver-agent"})


class AnswerKeyError(ValueError):
    pass


class FileAnswerKeyStore:
    def __init__(self, root: Path) -> None:
        self._root = root / KEYS_DIRNAME

    def path(self, snapshot_id: str) -> Path:
        return self._root / f"{require_slug(snapshot_id, 'a snapshot id')}.json"

    def get(self, snapshot_id: str) -> AnswerKey:
        path = self.path(snapshot_id)
        if not path.is_file():
            raise AnswerKeyError(f"no answer key for snapshot {snapshot_id!r}")
        return AnswerKey.model_validate_json(path.read_text(encoding="utf-8"))

    def seed_sealed(self, snapshot_id: str, markdown: str, *, sealed_at: datetime, added_by: str) -> AnswerKey:
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
        if item.source != "hindsight":
            raise AnswerKeyError("only hindsight items are added; the sealed key is seeded once")
        _author(item.added_by)
        with self._locked():
            key = self.get(snapshot_id)
            if any(existing.id == item.id for existing in key.items):
                raise AnswerKeyError(f"key item {item.id!r} exists")
            updated = key.model_copy(update={"items": (*key.items, item)})
            os.replace(self._temporary(updated), self.path(snapshot_id))
        return updated

    def confirm(self, snapshot_id: str, item_id: str, *, by: str) -> AnswerKey:
        _author(by)
        with self._locked():
            key = self.get(snapshot_id)
            if not any(i.id == item_id for i in key.items):
                raise AnswerKeyError(f"no key item {item_id!r}")
            updated = key.model_copy(update={"items": tuple(
                i.model_copy(update={"status": "confirmed"}) if i.id == item_id else i for i in key.items
            )})
            os.replace(self._temporary(updated), self.path(snapshot_id))
        return updated

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


def _author(name: str) -> str:
    if name.strip().casefold() in _NOT_A_KEY_AUTHOR:
        raise AnswerKeyError("the improver never writes its own answer key")
    return name


__all__ = ["KEYS_DIRNAME", "AnswerKeyError", "FileAnswerKeyStore"]
