"""Merge-held PR status for the Tech lead page (#7763): bounded, cached reads."""

from __future__ import annotations

from unittest.mock import MagicMock

from issue_orchestrator.control.merge_hold_status import MergeHoldStatuses
from issue_orchestrator.ports.pull_request_tracker import PRInfo, StatusCheckRollupRead


def _pr(number: int, *, state: str = "open", labels=("needs-human",), mergeable: str = "blocked") -> PRInfo:
    return PRInfo(number, f"PR {number}", "u", "b", "", state, list(labels), mergeable_state=mergeable)


def _reader(host, clock):
    return MergeHoldStatuses(host=host, needs_human_label="needs-human", clock=clock)


def test_reads_mergeability_and_the_decisive_rollup_once_per_ttl() -> None:
    host = MagicMock()
    host.get_pr.return_value = _pr(40)
    host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(capability="ok", state="FAILURE")
    now = [0.0]
    reader = _reader(host, lambda: now[0])

    first = reader.read([40])[40]
    reader.read([40])  # within the TTL: no new reads
    now[0] = 301.0
    reader.read([40])

    assert (first.held, first.mergeability, first.checks) == (True, "blocked", "FAILURE")
    assert host.get_pr.call_count == 2
    assert host.read_pr_status_check_rollup.call_count == 2


def test_a_clean_or_dirty_pr_never_pays_for_the_rollup() -> None:
    host = MagicMock()
    host.get_pr.return_value = _pr(40, mergeable="dirty")

    status = _reader(host, lambda: 0.0).read([40])[40]

    assert status.mergeability == "dirty"
    host.read_pr_status_check_rollup.assert_not_called()


def test_a_merged_or_released_pr_is_no_longer_held() -> None:
    host = MagicMock()
    host.get_pr.side_effect = lambda n: _pr(n, state="merged") if n == 1 else _pr(n, labels=())

    statuses = _reader(host, lambda: 0.0).read([1, 2])

    assert not statuses[1].held and not statuses[2].held


def test_a_failed_read_is_reported_not_hidden() -> None:
    host = MagicMock()
    host.get_pr.side_effect = RuntimeError("502")

    status = _reader(host, lambda: 0.0).read([40])[40]

    assert status.held and status.read_error == "502"
