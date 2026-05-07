from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.adapters.sidecar_attempt_store import SidecarAttemptStore
from issue_orchestrator.domain.attempt import Attempt, AttemptKey
from issue_orchestrator.domain.issue_key import GitHubIssueKey

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _key(issue: str = "6130", sha: str = SHA_A) -> AttemptKey:
    return AttemptKey(GitHubIssueKey("BruceBGordon/issue-orchestrator", issue), sha)


def test_sidecar_attempt_store_round_trips(tmp_path: Path) -> None:
    store = SidecarAttemptStore(tmp_path)
    attempt = Attempt(
        key=_key(),
        reroute_budget_used=3,
        validation_record_path=".issue-orchestrator/sessions/run/validation-record.json",
    )

    store.upsert(attempt)
    restored = store.for_key(attempt.key)

    assert restored is not None
    assert restored.key.issue_scope == attempt.key.issue_scope
    assert restored.key.issue_stable_id == attempt.key.issue_stable_id
    assert restored.key.head_sha == attempt.key.head_sha
    assert restored.reroute_budget_used == 3
    assert restored.validation_record_path == attempt.validation_record_path


def test_sidecar_attempt_store_supersedes_only_target_issue(tmp_path: Path) -> None:
    store = SidecarAttemptStore(tmp_path)
    first = Attempt(_key("6130", SHA_A))
    second = Attempt(_key("6130", SHA_B))
    other = Attempt(_key("6131", SHA_C))
    for attempt in (first, second, other):
        store.upsert(attempt)

    removed = store.supersede_issue(first.key.issue_key)

    assert removed == 2
    assert store.for_key(first.key) is None
    assert store.for_key(second.key) is None
    assert store.for_key(other.key) is not None


def test_sidecar_attempt_store_fails_fast_on_key_mismatch(tmp_path: Path) -> None:
    store = SidecarAttemptStore(tmp_path)
    key = _key()
    store.upsert(Attempt(key))
    path = next((tmp_path / ".issue-orchestrator" / "attempts").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["head_sha"] = "f" * 40
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="key mismatch"):
        store.for_key(key)
