"""Every GitHub write the orchestrator owes survives its refusals (#7350).

The liveness owner is the one owner of owed writes: a park's needs-human
block and comment, a block's withdrawal, and the reconciliation pause observed
drift calls for. A typed rate limit defers such a write to the host's reset
without spending its budget; any refusal leaves it owed durably, retried each
planning cycle until it lands -- never reported once and forgotten.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, Mock

from issue_orchestrator.control.action_liveness_escalation import ActionLivenessEscalation
from issue_orchestrator.control.action_results import ActionResult
from issue_orchestrator.control.actions import AddLabelAction
from issue_orchestrator.control.planner_types import Plan
from issue_orchestrator.control.reconciliation import (
    build_expected_for_mutation,
    get_pause_label,
)
from issue_orchestrator.domain.action_liveness import (
    ActionIdentity,
    ActionOutcome,
    LivenessKey,
    LivenessPolicy,
)
from issue_orchestrator.domain.host_rate_limit import HostRateLimit
from issue_orchestrator.execution.action_liveness_store import SQLiteActionLivenessStore
from issue_orchestrator.events import EventName
from tests.unit.control.liveness_doubles import ManualClock, applier_owner, gated

POLICY = LivenessPolicy()
KEY = LivenessKey(ActionIdentity("issue:410", "remove_label:in-progress"), "f" * 32, 410)
LIMITED_FOR = timedelta(hours=3)


class _RateLimitedApplier:
    """GitHub, rate-limited until ``reset``: every write is refused with the
    typed limit (#7303) until then, and lands afterwards."""

    def __init__(self, clock: ManualClock, reset) -> None:
        self._clock, self.reset = clock, reset
        self.refused: list[tuple[str, int]] = []
        self.landed: list[tuple[str, int]] = []

    def apply(self, action):
        kind = type(action).__name__
        number = getattr(action, "issue_number", getattr(action, "number", None))
        if self._clock() < self.reset:
            self.refused.append((kind, number))
            limit = HostRateLimit(resets_at=self.reset, kind="secondary")
            return ActionResult.fail_limited(action, "secondary rate limit", limit)
        self.landed.append((kind, number))
        return ActionResult.ok(action)


def _owner(tmp_path, applier, clock):
    return applier_owner(
        applier, MagicMock(), store=SQLiteActionLivenessStore(tmp_path / "l.sqlite"),
        clock=clock, policy=POLICY,
    )


def _cycles(owner, clock, until, step=timedelta(minutes=10)) -> None:
    """Planning cycles every ``step``, none later than ``until``: each retries
    owed writes."""
    while clock() + step <= until:
        clock.advance(step)
        owner.reconcile_effects()


def test_a_rate_limited_block_and_comment_wait_for_the_reset_then_land(tmp_path) -> None:
    """A needs-human label and its comment refused for 3 hours: paced (not one
    attempt per cycle), never exhausted, and both land after the reset."""
    clock = ManualClock()
    applier = _RateLimitedApplier(clock, clock() + LIMITED_FOR)
    owner = _owner(tmp_path, applier, clock)

    owner.record(KEY, ActionOutcome.permanent("stuck"))
    _cycles(owner, clock, applier.reset - timedelta(minutes=1))

    # 18 cycles ran during the limit; the block was asked a handful of times:
    # once, at the declared-wait bound, and at the ladder's pace after it.
    assert 1 <= len(applier.refused) <= 4, applier.refused
    assert applier.landed == []

    _cycles(owner, clock, applier.reset + 2 * POLICY.max_backoff)

    assert applier.landed == [("AddLabelAction", 410), ("AddCommentAction", 410)]
    row = SQLiteActionLivenessStore(tmp_path / "l.sqlite").row(KEY)
    assert row is not None and row.escalated and row.explained


def test_a_rate_limited_withdrawal_waits_for_the_reset_then_lands(tmp_path) -> None:
    """The park resolves while GitHub is rate-limited: the owed withdrawal is
    paced through the limit and lands after it, not exhausted during it."""
    clock = ManualClock()
    applier = _RateLimitedApplier(clock, clock())  # not limited yet
    owner = _owner(tmp_path, applier, clock)
    owner.record(KEY, ActionOutcome.permanent("stuck"))
    assert [kind for kind, _ in applier.landed] == ["AddLabelAction", "AddCommentAction"]

    applier.reset = clock() + LIMITED_FOR
    owner.record(KEY, ActionOutcome.done())
    _cycles(owner, clock, applier.reset - timedelta(minutes=1))
    assert 1 <= len(applier.refused) <= 4, applier.refused
    assert ("RemoveLabelAction", 410) not in applier.landed

    _cycles(owner, clock, applier.reset + 2 * POLICY.max_backoff)

    assert applier.landed[-1] == ("RemoveLabelAction", 410)
    assert SQLiteActionLivenessStore(tmp_path / "l.sqlite").pending_releases() == ()


# --- The reconciliation pause observed drift calls for ----------------------

SUBJECT = 410


class _Labels:
    """GitHub's labels on #410, whose pause write is refused while ``refuse``."""

    def __init__(self) -> None:
        self.live: dict[int, list[str]] = {SUBJECT: ["blocked"]}
        self.added: list[tuple[int, str]] = []
        self.refuse = True

    def add_label(self, issue_number, label):
        if label == get_pause_label() and self.refuse:
            raise RuntimeError("GitHub 502 on the pause label")
        self.added.append((issue_number, label))
        self.live.setdefault(issue_number, []).append(label)

    def remove_label(self, issue_number, label):  # pragma: no cover - unused
        raise AssertionError("no removal expected")

    def list_labels(self, issue_number):
        return list(self.live.get(issue_number, []))

    def read_issue_labels(self, issue_number):
        return self.list_labels(issue_number)


def test_a_refused_pause_is_owed_until_it_lands_without_an_operator(tmp_path) -> None:
    """Drift observed; GitHub refuses the ``io:needs-reconcile`` write through
    all of the drifting action's attempts, which then parks. The pause is still
    owed, and once GitHub accepts it the label lands on a later cycle -- no
    operator Retry."""
    from issue_orchestrator.control.orchestrator_support import (
        OrchestratorSupport,
        pause_issue_for_reconciliation,
    )
    from issue_orchestrator.domain.models import OrchestratorState
    from issue_orchestrator.domain.pause_state import PauseState
    from tests.runtime_lifecycle_helpers import make_action_applier

    events = MagicMock()
    labels = _Labels()
    applier = make_action_applier(
        labels=labels, sessions=MagicMock(), events=events,
        fresh_issue_reader=labels, reconcile=True,
    )
    context = MagicMock()
    context.enrich = Mock(side_effect=lambda d: d)
    support = OrchestratorSupport(
        config=MagicMock(), events=events, repository_host=MagicMock(),
        state=OrchestratorState(pause_state=PauseState.running()), event_context=context,
        session_manager=MagicMock(), action_applier=applier, fact_gatherer=MagicMock(),
        planner=MagicMock(), worktree_manager=MagicMock(), state_machine_manager=MagicMock(),
        cleanup_manager=MagicMock(), get_review_machine=Mock(), kill_session=Mock(),
        pending_work_claims=MagicMock(),
    )
    clock = ManualClock()
    owner = applier_owner(
        applier, events, store=SQLiteActionLivenessStore(tmp_path / "l.sqlite"),
        clock=clock, policy=POLICY,
    )

    def pause(number, reason):
        pause_issue_for_reconciliation(events, owner, context, number, reason)

    def tick() -> None:
        # The planner re-derives the same drifting write every cycle.
        plan = Plan(actions=(
            AddLabelAction(issue_number=SUBJECT, label="pr-pending", reason="done",
                           expected=build_expected_for_mutation(forbidden={"blocked"})),
        ), skipped=())
        support.apply_plan(gated(plan, owner), pause)
        clock.advance(POLICY.max_backoff)

    for _ in range(3 * POLICY.max_attempts):
        tick()

    parked = SQLiteActionLivenessStore(tmp_path / "l.sqlite").parked_rows()
    assert [row.key.escalation_issue for row in parked] == [SUBJECT], "the drifting write parked"
    assert (SUBJECT, get_pause_label()) not in labels.added

    labels.refuse = False
    tick()

    assert (SUBJECT, get_pause_label()) in labels.added
    assert SQLiteActionLivenessStore(tmp_path / "l.sqlite").pending_pauses() == ()
    paused = [
        call.args[0] for call in events.publish.call_args_list
        if call.args[0].name == EventName.ISSUE_PAUSED_RECONCILE
    ]
    assert [event.data["issue_number"] for event in paused] == [SUBJECT]


def test_a_rate_limited_pause_waits_for_the_reset(tmp_path) -> None:
    """A typed limit on the pause write defers it to the reset: not attempted
    each cycle during the limit, and written once it lifts."""
    clock = ManualClock()
    applier = _RateLimitedApplier(clock, clock() + timedelta(minutes=45))
    owner = _owner(tmp_path, applier, clock)

    assert not owner.owe_pause(SUBJECT, "drift").committed
    _cycles(owner, clock, applier.reset - timedelta(minutes=1), step=timedelta(minutes=5))
    assert applier.refused == [("AddLabelAction", SUBJECT)]

    _cycles(owner, clock, applier.reset + timedelta(minutes=5), step=timedelta(minutes=5))
    assert applier.landed == [("AddLabelAction", SUBJECT)]


def test_an_owed_pause_seen_on_its_issue_is_settled(tmp_path) -> None:
    """Observed on the issue (a person added it, or a write reported as refused
    had in fact committed): the debt is settled, nothing is written again."""
    clock = ManualClock()
    applier = _RateLimitedApplier(clock, clock() + LIMITED_FOR)
    owner = _owner(tmp_path, applier, clock)
    owner.owe_pause(SUBJECT, "drift")

    owner.settle_observed_pauses({SUBJECT: ("blocked", get_pause_label())})
    clock.advance(LIMITED_FOR + POLICY.max_backoff)
    owner.reconcile_effects()

    assert applier.landed == []
    assert SQLiteActionLivenessStore(tmp_path / "l.sqlite").pending_pauses() == ()


def test_the_escalation_adapter_keeps_the_hosts_rate_limit() -> None:
    """The adapter hands the owner the typed limit behind a refused write,
    whether the applier returned it or raised it."""
    from issue_orchestrator.ports.repository_host import RepositoryHostRateLimitedError

    clock = ManualClock()
    reset = clock() + LIMITED_FOR
    returned = ActionLivenessEscalation(
        events=MagicMock(), applier=_RateLimitedApplier(clock, reset),
        needs_human_label="needs-human",
    )
    assert returned.pause(SUBJECT, "drift").rate_limit == HostRateLimit(reset, "secondary")

    limited = RepositoryHostRateLimitedError("API rate limit exceeded")
    limited.rate_limit = HostRateLimit(resets_at=reset, kind="primary")
    raising = MagicMock()
    raising.apply.side_effect = limited
    result = ActionLivenessEscalation(
        events=MagicMock(), applier=raising, needs_human_label="needs-human",
    ).unblock(SUBJECT)
    assert not result.committed and result.rate_limit == limited.rate_limit

