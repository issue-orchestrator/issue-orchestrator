"""The replay decision table of an approved decision's op (#7593 review r2/r3)."""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.operator_decision_retry import (
    DecisionReplayStep,
    DecisionRetryState,
    decision_replay_step,
)


@pytest.mark.parametrize(
    ("prior", "unblocked", "step"),
    [
        (None, True, DecisionReplayStep.CARRY_OUT),
        (None, False, DecisionReplayStep.CARRY_OUT),
        (DecisionRetryState.COMMITTED, True, DecisionReplayStep.FINISH),
        # A block raised after a committed retry is never cleared by a replay.
        (DecisionRetryState.COMMITTED, False, DecisionReplayStep.FINISH),
        (DecisionRetryState.BEGUN, True, DecisionReplayStep.FINISH),
        # Interrupted mid-retry and blocked again: cannot tell, so hand back.
        (DecisionRetryState.BEGUN, False, DecisionReplayStep.HAND_BACK),
    ],
)
def test_every_replay_has_one_step(prior, unblocked, step) -> None:
    assert decision_replay_step(prior, item_open_and_unblocked=unblocked) is step
