"""The exam's branch ledger records only what the remote proves (#9523 review F2).

A receipt says: the branch was absent when the run started, and the remote
holds the run's commit on it. Anything less leaves the branch on the remote.
"""

import pytest

from tests.e2e.exam.run_branches import RunBranches, parse_heads
from tests.e2e.fixtures.artifact_cleanup import RemoteBranchReceipt

RUN_ID = "0a1b2c3d4e5f"


class _Remote:
    """A remote's branch heads, changing as the run goes."""

    def __init__(self, **heads: str) -> None:
        self.heads = dict(heads)
        self.reads = 0

    def read(self) -> dict[str, str]:
        self.reads += 1
        return dict(self.heads)


def test_a_branch_the_run_pushed_is_recorded_at_its_commit():
    remote = _Remote(main="m" * 40)
    ledger = RunBranches(remote.read)
    remote.heads["7-exam"] = "a" * 40

    assert ledger.record("7-exam", "a" * 40)
    assert ledger.receipts() == {RemoteBranchReceipt("7-exam", "a" * 40)}


def test_a_branch_that_existed_before_the_run_is_never_recorded():
    """Even when the remote holds the very commit the run offers (the engine
    checked the existing branch out and pushed nothing new)."""
    remote = _Remote(**{"7-exam": "f" * 40})
    ledger = RunBranches(remote.read)

    assert not ledger.record("7-exam", "f" * 40)
    assert ledger.receipts() == frozenset()


@pytest.mark.parametrize(
    "remote_head",
    [None, "b" * 40],
    ids=["never-pushed", "remote-holds-another-commit"],
)
def test_a_push_the_remote_does_not_show_is_not_recorded(remote_head):
    remote = _Remote()
    ledger = RunBranches(remote.read)
    if remote_head is not None:
        remote.heads["7-exam"] = remote_head

    assert not ledger.record("7-exam", "a" * 40)
    assert ledger.receipts() == frozenset()


def test_the_latest_proven_commit_is_the_receipt():
    """Seeded at one commit, reworked by the engine to another."""
    remote = _Remote()
    ledger = RunBranches(remote.read)
    remote.heads["7-exam"] = "a" * 40
    ledger.record("7-exam", "a" * 40)
    remote.heads["7-exam"] = "c" * 40

    assert ledger.record_all({"7-exam": "c" * 40, "8-never": "d" * 40}) == ["7-exam"]
    assert ledger.receipts() == {RemoteBranchReceipt("7-exam", "c" * 40)}


def test_offering_nothing_reads_nothing():
    remote = _Remote()
    ledger = RunBranches(remote.read)

    assert ledger.record_all({}) == []
    assert remote.reads == 1  # the snapshot at the run's start


def test_a_run_unique_branch_created_elsewhere_is_the_runs_at_its_current_head():
    remote = _Remote()
    ledger = RunBranches(remote.read)
    ledger.expect_unique(f"exam-integration-{RUN_ID}", RUN_ID)
    remote.heads[f"exam-integration-{RUN_ID}"] = "i" * 40

    assert ledger.receipts() == {RemoteBranchReceipt(f"exam-integration-{RUN_ID}", "i" * 40)}


def test_a_run_unique_branch_that_never_appeared_or_already_existed_is_not_the_runs():
    remote = _Remote(**{f"exam-integration-{RUN_ID}": "o" * 40})
    ledger = RunBranches(remote.read)
    ledger.expect_unique(f"exam-integration-{RUN_ID}", RUN_ID)
    ledger.expect_unique("exam-integration-ffffffffffff", "ffffffffffff")

    assert ledger.receipts() == frozenset()


def test_only_a_name_carrying_the_run_id_can_be_expected():
    with pytest.raises(ValueError, match="run id"):
        RunBranches(_Remote().read).expect_unique("exam-integration", RUN_ID)


def test_ls_remote_heads_are_parsed_exactly():
    output = "\n".join([
        f"{'a' * 40}\trefs/heads/7-exam-work",
        f"{'b' * 40}\trefs/heads/main",
        f"{'c' * 40}\trefs/tags/v1",
    ])

    assert parse_heads(output) == {"7-exam-work": "a" * 40, "main": "b" * 40}
