"""A ref-backed record survives a round trip at any size it can reach (#7272).

The record used to live in the commit MESSAGE, and GitHub's Git Data API
truncates that field at exactly 65 536 characters. A record above the cap could
still be WRITTEN -- the create call accepts the whole thing -- and could never
be read back. One repository's registry became unreadable that way while the
write that broke it reported success, so these tests are written at sizes that
cross the old cliff.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.adapters.github.pattern_registry import parse_entries
from issue_orchestrator.adapters.github.ref_store import (
    MAX_RECORD_BYTES,
    RECORD_PATH,
    GitRefCasStore,
    RecordTooLargeError,
)
from tests.registry_records import (
    PORCHPIN_BYTES_PER_PATTERN,
    registry_record,
)

from .fake_git_data import FakeGitHubRefClient

REF_PREFIX = "refs/issue-orchestrator/registry"

#: #7272's acceptance size. The registry that broke held 53.
ACCEPTANCE_PATTERN_COUNT = 200


@pytest.fixture
def client() -> FakeGitHubRefClient:
    return FakeGitHubRefClient()


@pytest.fixture
def store(client: FakeGitHubRefClient) -> GitRefCasStore:
    return GitRefCasStore(client, ref_prefix=REF_PREFIX)  # type: ignore[arg-type]


class TestTheFixtureItself:
    """A comfortable fixture would pass with the #7272 defect still present."""

    def test_the_acceptance_registry_is_past_the_old_cap(self) -> None:
        record = registry_record(ACCEPTANCE_PATTERN_COUNT)

        assert len(record) > FakeGitHubRefClient.MESSAGE_CAP

    def test_entries_are_as_heavy_as_the_registry_that_broke(self) -> None:
        """Pins the density to porchpin's, so nobody shrinks it into vacuity."""
        density = len(registry_record(ACCEPTANCE_PATTERN_COUNT)) / (
            ACCEPTANCE_PATTERN_COUNT
        )

        assert abs(density - PORCHPIN_BYTES_PER_PATTERN) < 0.1 * (
            PORCHPIN_BYTES_PER_PATTERN
        )


class TestRoundTrip:

    def test_two_hundred_patterns_round_trip_through_the_store(
        self, store: GitRefCasStore
    ) -> None:
        record = registry_record(ACCEPTANCE_PATTERN_COUNT)

        assert store.create("tech-lead-patterns", record) is True
        snapshot = store.read("tech-lead-patterns")

        assert snapshot is not None
        assert parse_entries(snapshot.record) == parse_entries(record)

    def test_an_update_past_the_cap_survives_too(self, store: GitRefCasStore) -> None:
        record = registry_record(ACCEPTANCE_PATTERN_COUNT)
        store.create("tech-lead-patterns", registry_record(1))
        snapshot = store.read("tech-lead-patterns")
        assert snapshot is not None

        assert store.update(snapshot, record) is True

        reread = store.read("tech-lead-patterns")
        assert reread is not None and reread.record == record

    def test_the_record_lives_in_the_tree_not_the_message(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        """The commit message is for people; truncating it must cost nothing."""
        store.create("tech-lead-patterns", registry_record(ACCEPTANCE_PATTERN_COUNT))
        commit_sha = client.refs[f"{REF_PREFIX}/tech-lead-patterns"]

        message = client.commits[commit_sha]["message"]
        entries = client.trees[client.commits[commit_sha]["tree"]["sha"]]["tree"]

        assert len(message) < client.MESSAGE_CAP
        assert [entry["path"] for entry in entries] == [RECORD_PATH]


class TestLegacyRecords:
    def test_a_record_written_in_the_message_is_still_readable(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        """An existing ref opens without being migrated first."""
        client.seed_legacy_message_record(
            f"{REF_PREFIX}/tech-lead-patterns", '{"entries":[],"version":2}'
        )

        snapshot = store.read("tech-lead-patterns")

        assert snapshot is not None
        assert snapshot.record == '{"entries":[],"version":2}'

    def test_the_next_write_moves_it_into_the_tree(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        client.seed_legacy_message_record(
            f"{REF_PREFIX}/tech-lead-patterns", '{"entries":[],"version":2}'
        )
        snapshot = store.read("tech-lead-patterns")
        assert snapshot is not None

        record = registry_record(ACCEPTANCE_PATTERN_COUNT)
        store.update(snapshot, record)

        reread = store.read("tech-lead-patterns")
        assert reread is not None and reread.record == record


class TestWriteTimeRefusal:
    def test_a_record_too_large_to_store_is_refused_when_written(
        self, store: GitRefCasStore
    ) -> None:
        """Refused by the WRITER, not discovered by the next reader.

        The whole shape of #7272 was a write that succeeded and could never be
        read, so the one thing this storage must not do is accept a payload it
        cannot hand back.
        """
        with pytest.raises(RecordTooLargeError, match="refusing to write"):
            store.create("tech-lead-patterns", "x" * (MAX_RECORD_BYTES + 1))

    def test_nothing_is_written_when_a_create_is_refused(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        with pytest.raises(RecordTooLargeError):
            store.create("tech-lead-patterns", "x" * (MAX_RECORD_BYTES + 1))

        assert f"{REF_PREFIX}/tech-lead-patterns" not in client.refs
        assert client.blobs == {}

    def test_a_refused_update_leaves_the_stored_record_untouched(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        """A registry that outgrows the bound keeps the last good record."""
        good = registry_record(1)
        store.create("tech-lead-patterns", good)
        snapshot = store.read("tech-lead-patterns")
        assert snapshot is not None
        ref_before = client.refs[f"{REF_PREFIX}/tech-lead-patterns"]

        with pytest.raises(RecordTooLargeError):
            store.update(snapshot, "x" * (MAX_RECORD_BYTES + 1))

        assert client.refs[f"{REF_PREFIX}/tech-lead-patterns"] == ref_before
        reread = store.read("tech-lead-patterns")
        assert reread is not None and reread.record == good


class TestUnreadableTree:
    def test_a_truncated_tree_is_an_error_not_a_legacy_record(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        """A partial listing cannot prove the record is not in the tree.

        Falling back to the commit message there would answer from the wrong
        place, and the wrong place is the one that truncates (#7272).
        """
        client.seed_record(
            f"{REF_PREFIX}/tech-lead-patterns", registry_record(1)
        )
        commit_sha = client.refs[f"{REF_PREFIX}/tech-lead-patterns"]
        tree = client.trees[client.commits[commit_sha]["tree"]["sha"]]
        tree["tree"] = []
        tree["truncated"] = True

        with pytest.raises(ValueError, match="truncated tree"):
            store.read("tech-lead-patterns")
