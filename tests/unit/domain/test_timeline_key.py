"""Tests for TimelineKey domain value class."""

import pytest

from issue_orchestrator.domain.timeline_key import TimelineKey


class TestTimelineKeyFactories:
    def test_for_issue(self):
        key = TimelineKey.for_issue(123)
        assert key.namespace == "issue"
        assert key.local_id == 123
        assert key.is_issue
        assert not key.is_e2e_run

    def test_for_e2e_run(self):
        key = TimelineKey.for_e2e_run(42)
        assert key.namespace == "e2e-run"
        assert key.local_id == 42
        assert key.is_e2e_run
        assert not key.is_issue

    def test_for_issue_rejects_zero(self):
        with pytest.raises(ValueError, match="positive"):
            TimelineKey.for_issue(0)

    def test_for_issue_rejects_negative(self):
        with pytest.raises(ValueError, match="positive"):
            TimelineKey.for_issue(-1)

    def test_for_e2e_run_rejects_zero(self):
        with pytest.raises(ValueError, match="positive"):
            TimelineKey.for_e2e_run(0)

    def test_for_e2e_run_rejects_negative(self):
        with pytest.raises(ValueError, match="positive"):
            TimelineKey.for_e2e_run(-5)


class TestStoreKeyEncoding:
    def test_issue_encodes_positive(self):
        assert TimelineKey.for_issue(123).to_store_key() == 123

    def test_e2e_run_encodes_negative(self):
        assert TimelineKey.for_e2e_run(42).to_store_key() == -42

    def test_roundtrip_issue(self):
        original = TimelineKey.for_issue(999)
        decoded = TimelineKey.from_store_key(original.to_store_key())
        assert decoded == original

    def test_roundtrip_e2e_run(self):
        original = TimelineKey.for_e2e_run(7)
        decoded = TimelineKey.from_store_key(original.to_store_key())
        assert decoded == original

    def test_from_store_key_positive(self):
        key = TimelineKey.from_store_key(50)
        assert key.is_issue
        assert key.local_id == 50

    def test_from_store_key_negative(self):
        key = TimelineKey.from_store_key(-50)
        assert key.is_e2e_run
        assert key.local_id == 50

    def test_from_store_key_zero_raises(self):
        with pytest.raises(ValueError, match="reserved"):
            TimelineKey.from_store_key(0)


class TestDisplay:
    def test_stable_id_issue(self):
        assert TimelineKey.for_issue(123).stable_id() == "issue:123"

    def test_stable_id_e2e_run(self):
        assert TimelineKey.for_e2e_run(42).stable_id() == "e2e-run:42"

    def test_str(self):
        assert str(TimelineKey.for_issue(1)) == "issue:1"


class TestFrozen:
    def test_is_hashable(self):
        key = TimelineKey.for_issue(1)
        assert hash(key) is not None
        s = {key}
        assert key in s

    def test_immutable(self):
        key = TimelineKey.for_issue(1)
        with pytest.raises(AttributeError):
            key.local_id = 2  # type: ignore[misc]
