"""JSON sidecar implementation of :class:`AttemptStore`."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..domain.attempt import Attempt, AttemptKey
from ..domain.issue_key import IssueKey
from ..infra.atomic_json import atomic_write_json

_SAFE_PART_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class SidecarAttemptStore:
    """Persist attempts under ``.issue-orchestrator/attempts`` in a worktree."""

    def __init__(self, worktree: Path) -> None:
        self._base_dir = worktree / ".issue-orchestrator" / "attempts"

    def for_key(self, key: AttemptKey) -> Attempt | None:
        path = self._path_for(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Attempt sidecar is unreadable: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Attempt sidecar must contain an object: {path}")
        attempt = Attempt.from_dict(payload)
        if (
            attempt.key.issue_scope != key.issue_scope
            or attempt.key.issue_stable_id != key.issue_stable_id
            or attempt.key.head_sha != key.head_sha
        ):
            raise ValueError(f"Attempt sidecar key mismatch: {path}")
        return attempt

    def upsert(self, attempt: Attempt) -> None:
        atomic_write_json(self._path_for(attempt.key), attempt.to_dict())

    def supersede_issue(self, issue_key: IssueKey) -> int:
        if not self._base_dir.exists():
            return 0
        issue_prefix = f"{_issue_part(issue_key)}--"
        removed = 0
        for path in self._base_dir.glob(f"{issue_prefix}*.json"):
            if not path.is_file():
                continue
            path.unlink()
            removed += 1
        return removed

    def _path_for(self, key: AttemptKey) -> Path:
        issue_part = _issue_part(key.issue_key)
        sha_part = key.head_sha
        return self._base_dir / f"{issue_part}--{sha_part}.json"


def _issue_part(issue_key: IssueKey) -> str:
    return _safe_part(f"{issue_key.scope()}--{issue_key.stable_id()}")


def _safe_part(value: str) -> str:
    safe = _SAFE_PART_RE.sub("-", value.strip())
    if not safe:
        raise ValueError("attempt path component must be non-empty")
    return safe
