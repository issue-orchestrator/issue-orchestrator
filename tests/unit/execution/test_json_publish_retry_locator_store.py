from __future__ import annotations

import json

import pytest

from issue_orchestrator.domain.publish_retry import PublishRetryLocators
from issue_orchestrator.execution import json_publish_retry_locator_store as store_mod
from issue_orchestrator.execution.json_publish_retry_locator_store import (
    CorruptPublishRetryLocatorStoreError,
    JsonPublishRetryLocatorStore,
)

BRANCH = "4057-scratch-1"


def _locators(make_session, *, issue_number: int = 4057) -> PublishRetryLocators:
    session = make_session(
        issue_number=issue_number,
        issue_title="UI: Surface provider status",
        branch_name=BRANCH,
    )
    return PublishRetryLocators(
        issue_number=issue_number,
        issue_title=session.issue.title,
        session_key=session.key.stable_id(),
        worktree_path=str(session.worktree_path),
        branch_name=session.branch_name,
        completion_path=session.completion_path,
        run_assets=session.run_assets,
        agent_label=session.agent_label,
        pr_number=None,
    )


def test_save_get_roundtrip_survives_reopen(make_session, tmp_path) -> None:
    path = tmp_path / "state" / "publish_retry_locators.json"
    JsonPublishRetryLocatorStore(path).save(_locators(make_session))

    reopened = JsonPublishRetryLocatorStore(path).get(4057)

    assert reopened is not None
    assert reopened.branch_name == BRANCH


def test_corrupt_store_raises_instead_of_degrading_to_empty(tmp_path) -> None:
    path = tmp_path / "publish_retry_locators.json"
    path.write_text("{not valid json")

    with pytest.raises(CorruptPublishRetryLocatorStoreError):
        JsonPublishRetryLocatorStore(path)


def test_non_object_store_raises(tmp_path) -> None:
    path = tmp_path / "publish_retry_locators.json"
    path.write_text(json.dumps([1, 2, 3]))

    with pytest.raises(CorruptPublishRetryLocatorStoreError):
        JsonPublishRetryLocatorStore(path)


def test_malformed_entry_raises_instead_of_hiding_retry(tmp_path) -> None:
    """A well-formed store object with a malformed per-issue entry is corruption.

    Degrading it to "no locators" would silently hide Retry Publish for a
    genuinely publish-failed issue — the exact failure this store must avoid.
    """
    path = tmp_path / "publish_retry_locators.json"
    # Valid JSON object, but the entry is missing required locator fields.
    path.write_text(json.dumps({"4057": {"issue_number": 4057}}))

    with pytest.raises(CorruptPublishRetryLocatorStoreError):
        JsonPublishRetryLocatorStore(path)


def test_failed_persist_preserves_previous_state(make_session, tmp_path, monkeypatch) -> None:
    """A write failure must not lose the previously durable retry state."""
    path = tmp_path / "publish_retry_locators.json"
    store = JsonPublishRetryLocatorStore(path)
    store.save(_locators(make_session, issue_number=4057))

    def _boom(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store_mod, "atomic_write_json", _boom)

    with pytest.raises(OSError):
        store.save(_locators(make_session, issue_number=999))

    # In-memory index still serves the original entry, and the new one never
    # partially landed. A fresh reader sees exactly the pre-failure file.
    assert store.get(4057) is not None
    assert store.get(999) is None
    reopened = JsonPublishRetryLocatorStore(path)
    assert reopened.get(4057) is not None
    assert reopened.get(999) is None


def test_writes_are_atomic_no_partial_file(make_session, tmp_path) -> None:
    """The persisted file is always valid JSON (atomic replace, never torn)."""
    store_dir = tmp_path / "state"
    path = store_dir / "publish_retry_locators.json"
    store = JsonPublishRetryLocatorStore(path)
    store.save(_locators(make_session))

    # Readable as a whole object; no sibling tempfiles left behind by the
    # atomic write (mkstemp uses a ".{name}." prefix + ".tmp" suffix).
    assert isinstance(json.loads(path.read_text()), dict)
    tempfiles = [p for p in store_dir.iterdir() if p.name != path.name]
    assert tempfiles == []


def test_clear_is_persisted(make_session, tmp_path) -> None:
    path = tmp_path / "publish_retry_locators.json"
    store = JsonPublishRetryLocatorStore(path)
    store.save(_locators(make_session))

    store.clear(4057)

    assert store.get(4057) is None
    assert JsonPublishRetryLocatorStore(path).get(4057) is None
