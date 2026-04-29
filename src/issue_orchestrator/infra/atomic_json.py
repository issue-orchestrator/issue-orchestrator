"""Atomic JSON write primitive for crash-safe artifact persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2) -> None:
    """Write JSON atomically so concurrent readers never see a torn file.

    Creates a sibling tempfile (same directory as ``path``), writes the
    encoded payload, then ``os.replace``s it into place. ``os.replace``
    is only atomic within a single filesystem, so the tempfile MUST be a
    sibling — placing it on a different mount would silently fall back
    to copy-then-unlink, defeating atomicity. On any failure, the
    tempfile is unlinked.

    A ``kill -9`` between ``mkstemp`` and ``os.replace`` can leave an
    orphan tempfile. In this orchestrator the worktree containing the
    target is itself cleaned up when the issue finishes, so orphans die
    with the worktree. Callers that need explicit reaping (e.g. the
    review-exchange transcript dir which long-lived processes write
    into) provide their own sweeper — see
    ``review_exchange_loop.sweep_atomic_write_tempfiles``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=indent)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(encoded)
        os.replace(tmp_path_str, path)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except FileNotFoundError:
            pass
        raise
