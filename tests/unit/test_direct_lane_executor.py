"""Direct-subprocess backend against the shared lane contract."""

from __future__ import annotations

import pytest

from issue_orchestrator.adapters.direct_lane_executor import (
    DirectLaneExecutor,
    DirectLaneTerminationPolicy,
)
from issue_orchestrator.ports.lane_executor import LaneExecutor
from tests.unit.lane_executor_contract import LaneExecutorContract

# First flush (135s) + observation (45s) + conclusion (60s) = 240s of backstops,
# and a timeout that fires first would replace the contract's own diagnosis with
# one that names nothing -- the failure #7264 was filed about (#7264 r9).
pytestmark = pytest.mark.timeout(300)


class TestDirectLaneExecutorContract(LaneExecutorContract):
    def build_executor(self) -> LaneExecutor:
        return DirectLaneExecutor(DirectLaneTerminationPolicy(2.0))


def test_direct_backend_queue_wait_is_exactly_zero(tmp_path: object) -> None:
    """No scheduler, no queue: the direct backend starts the lane the
    moment it is asked to, and says so with an exact 0.0 — never a
    measured near-zero that would imply a queue exists."""
    import sys
    from pathlib import Path

    from issue_orchestrator.domain.lane_execution import (
        LaneCommand,
        LaneCompleted,
        LaneDeadline,
        LaneResources,
        LaneWorkKey,
    )

    assert isinstance(tmp_path, Path)
    outcome = DirectLaneExecutor(DirectLaneTerminationPolicy(2.0)).run(
        LaneCommand(
            work_key=LaneWorkKey("direct.queue-wait"),
            arguments=(sys.executable, "-c", "pass"),
            working_directory=tmp_path.resolve(),
            deadline=LaneDeadline(60.0),
        ),
        LaneResources(request_cpus=1),
    )
    assert type(outcome) is LaneCompleted
    assert outcome.queue_wait_seconds == 0.0
