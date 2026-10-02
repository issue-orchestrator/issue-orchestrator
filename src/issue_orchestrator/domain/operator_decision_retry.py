"""What a replay of an approved decision's op does, by its retry's record (#7593).

The decision table for ``OperatorDecisionExecutor``: the durable state of the
item's retry (see ``ports/operator_decision_retries``) and whether the item is
open and unblocked now decide one step. Pure, so every combination is tested
without an executor.
"""

from __future__ import annotations

from enum import Enum


class DecisionRetryState(Enum):
    #: The retry was about to be attempted; whether it committed is unknown.
    BEGUN = "begun"
    #: The operator retry committed.
    COMMITTED = "committed"


class DecisionReplayStep(Enum):
    #: No retry was begun: refuse, or file, post and retry.
    CARRY_OUT = "carry_out"
    #: The retry committed: only finish the applied marker.
    FINISH = "finish"
    #: The engine stopped mid-retry and the item waits again (or closed): a
    #: retry that never committed cannot be told from a block raised after one
    #: that did, so nothing is retried and the proposal stays for the operator.
    HAND_BACK = "hand_back"


def decision_replay_step(
    prior: DecisionRetryState | None, *, item_open_and_unblocked: bool
) -> DecisionReplayStep:
    return _STEPS[(prior, item_open_and_unblocked)]


_STEPS: dict[tuple[DecisionRetryState | None, bool], DecisionReplayStep] = {
    (None, True): DecisionReplayStep.CARRY_OUT,
    (None, False): DecisionReplayStep.CARRY_OUT,
    (DecisionRetryState.COMMITTED, True): DecisionReplayStep.FINISH,
    (DecisionRetryState.COMMITTED, False): DecisionReplayStep.FINISH,
    # An open, unblocked item after an interrupted retry is the retry's own work.
    (DecisionRetryState.BEGUN, True): DecisionReplayStep.FINISH,
    (DecisionRetryState.BEGUN, False): DecisionReplayStep.HAND_BACK,
}
