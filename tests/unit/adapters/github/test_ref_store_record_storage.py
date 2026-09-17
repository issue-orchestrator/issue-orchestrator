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
    API_MESSAGE_CAP,
    MAX_RECORD_BYTES,
    RECORD_FORMAT_MARKER,
    RECORD_PATH,
    GitRefCasStore,
    RecordTooLargeError,
    RecordTruncatedError,
)
from tests.registry_records import (
    PORCHPIN_BYTES_PER_PATTERN,
    PORCHPIN_PATTERN_COUNT,
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


class TestFormatDiscrimination:
    """Which format a commit is in is DECLARED, never guessed (round 1 F1).

    A pre-#7272 commit reused the default branch's root tree, so "does a
    ``record.json`` path exist" answers a different question than "is this
    record in the tree". A repository that keeps a root ``record.json`` would
    have had that unrelated file read back as its registry.
    """

    def test_a_legacy_commit_over_a_tree_that_has_a_record_json_reads_the_message(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        legacy = '{"entries":[],"version":2}'
        decoy = '{"entries":[{"signature":"not-the-registry"}],"version":2}'
        client.seed_legacy_message_record(
            f"{REF_PREFIX}/tech-lead-patterns", legacy, tree_paths={RECORD_PATH: decoy}
        )

        snapshot = store.read("tech-lead-patterns")

        assert snapshot is not None
        assert snapshot.record == legacy

    def test_a_declared_tree_record_missing_its_blob_is_an_error(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        """Never a silent fall-back to the message the commit disowned."""
        ref = f"{REF_PREFIX}/tech-lead-patterns"
        client.seed_record(ref, registry_record(1))
        commit = client.commits[client.refs[ref]]
        client.trees[commit["tree"]["sha"]]["tree"] = []

        with pytest.raises(ValueError, match="carries no record.json"):
            store.read("tech-lead-patterns")

    def test_the_marker_is_written_on_every_record_commit(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        store.create("tech-lead-patterns", registry_record(1))
        snapshot = store.read("tech-lead-patterns")
        assert snapshot is not None
        store.update(snapshot, registry_record(2))

        messages = [
            client.commits[sha]["message"]
            for sha in (snapshot.commit_sha, client.refs[f"{REF_PREFIX}/tech-lead-patterns"])
        ]

        assert all(RECORD_FORMAT_MARKER in message for message in messages)


class TestTruncatedLegacyRecords:
    """A record the API cut off is refused, never returned (round 1 F3).

    Returning the prefix is what let a consumer treat a partial record as a
    whole one: a claim payload cut inside its newest block still parses, and
    the reader gets the PREVIOUS claim as though it were current.
    """

    def test_a_legacy_record_at_the_cap_is_refused(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        client.seed_legacy_message_record(
            f"{REF_PREFIX}/tech-lead-patterns",
            registry_record(PORCHPIN_PATTERN_COUNT),
        )

        with pytest.raises(RecordTruncatedError, match="partial record"):
            store.read("tech-lead-patterns")

    def test_the_fake_truncates_the_way_the_api_does(
        self, client: FakeGitHubRefClient
    ) -> None:
        """The behaviour the refusal above depends on, asserted directly.

        Without this, deleting the fake's truncation branch would leave every
        other test in this file green while the defect walked back in.
        """
        oversized = registry_record(PORCHPIN_PATTERN_COUNT)
        assert len(oversized) > API_MESSAGE_CAP, "the fixture is too small"
        sha = client.seed_legacy_message_record(
            f"{REF_PREFIX}/tech-lead-patterns", oversized
        )

        returned = client.get_git_commit(sha)["message"]

        assert len(returned) == API_MESSAGE_CAP
        assert returned != oversized
        assert oversized.startswith(returned[:-1])


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

        The commit says the record is there, so a listing that does not show it
        is a broken read, never an older format.
        """
        client.seed_record(
            f"{REF_PREFIX}/tech-lead-patterns", registry_record(1)
        )
        commit_sha = client.refs[f"{REF_PREFIX}/tech-lead-patterns"]
        tree = client.trees[client.commits[commit_sha]["tree"]["sha"]]
        tree["tree"] = []
        tree["truncated"] = True

        with pytest.raises(ValueError, match="GitHub truncated the listing"):
            store.read("tech-lead-patterns")


class TestDelete:
    """A deleter holding a stale snapshot must not remove a successor's ref.

    GitHub's Git Data API has no conditional delete, so this cannot be a true
    compare-and-swap; it re-reads and compares, which narrows the window rather
    than closing it. An unconditional delete had no window at all -- it always
    removed whatever was there, including a claim someone else had just taken
    over (round 1 F2).
    """

    def test_deleting_the_snapshot_it_was_given_succeeds(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        store.create("tech-lead-patterns", registry_record(1))
        snapshot = store.read("tech-lead-patterns")
        assert snapshot is not None

        assert store.delete(snapshot) is True
        assert f"{REF_PREFIX}/tech-lead-patterns" not in client.refs

    def test_a_ref_that_moved_on_is_refused_not_deleted(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        store.create("tech-lead-patterns", registry_record(1))
        stale = store.read("tech-lead-patterns")
        assert stale is not None
        successor = registry_record(2)
        assert store.update(stale, successor) is True

        assert store.delete(stale) is False

        survivor = store.read("tech-lead-patterns")
        assert survivor is not None and survivor.record == successor

    def test_a_ref_already_gone_is_reported_released(
        self, store: GitRefCasStore, client: FakeGitHubRefClient
    ) -> None:
        """Nothing to remove is the outcome the caller asked for."""
        store.create("tech-lead-patterns", registry_record(1))
        snapshot = store.read("tech-lead-patterns")
        assert snapshot is not None
        del client.refs[f"{REF_PREFIX}/tech-lead-patterns"]

        assert store.delete(snapshot) is True
