"""A transient GitHub read failure defers the subject; it never pauses it (#7379).

porchpin #410 was paused behind ``io:needs-reconcile`` because a GitHub
TRANSPORT error stopped the reconciliation gate reading its labels. The gate
treated "could not read" as drift, and only a human can lift that pause, so a
network blip became a permanent hold.

The distinction is now typed end to end: the adapter classifies each read
failure (``FreshIssueReadError.transient``), the gate turns a transient one into
``ReconciliationDeferred`` (still a refusal everywhere), and the plan applier
defers the subject for the tick instead of pausing it. A real disagreement, and
a failure that will not clear by itself, still pause exactly as before.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock

import pytest

from issue_orchestrator.adapters.github.errors import (
    GitHubAuthError,
    GitHubHttpError,
    GitHubRateLimitedError,
    GitHubTransportError,
)
from issue_orchestrator.adapters.github.fresh_issue_reader import GitHubFreshIssueReader
from issue_orchestrator.control.actions import AddLabelAction
from issue_orchestrator.control.mutation_gate import ReconciliationGate
from issue_orchestrator.control.planner_types import Plan
from issue_orchestrator.control.reconciliation import (
    ExpectedState,
    ReconciliationDeferred,
    ReconciliationRequired,
    build_expected_for_mutation,
    get_pause_label,
)
from issue_orchestrator.domain.host_rate_limit import HostRateLimit
from issue_orchestrator.events import EventName
from issue_orchestrator.ports.fresh_issue_reader import FreshIssueReadError
from issue_orchestrator.ports.repository_host import host_rate_limit_of

SUBJECT = 410
OTHER = 379


def _rate_limited() -> GitHubRateLimitedError:
    return GitHubRateLimitedError(
        "secondary rate limit",
        rate_limit=HostRateLimit(
            resets_at=datetime.now(timezone.utc) + timedelta(minutes=5), kind="secondary"
        ),
        status_code=403,
    )


TRANSIENT = [
    pytest.param(GitHubTransportError("connection reset"), id="transport"),
    pytest.param(GitHubHttpError("bad gateway", status_code=502), id="5xx"),
    pytest.param(GitHubHttpError("too many requests", status_code=429), id="429"),
    pytest.param(_rate_limited(), id="rate-limited"),
]
LASTING = [
    pytest.param(GitHubAuthError("bad credentials", status_code=401), id="auth"),
    pytest.param(GitHubHttpError("not found", status_code=404), id="404"),
    pytest.param(GitHubHttpError("forbidden", status_code=403), id="403"),
    pytest.param(RuntimeError("adapter bug"), id="non-host-fault"),
]


def _reader(error: Exception) -> GitHubFreshIssueReader:
    client = Mock()
    client.get_issue_labels.side_effect = error
    return GitHubFreshIssueReader(repo="porchpin/porchpin", http_client=client)


class TestTheAdapterClassifiesEveryReadFailure:
    @pytest.mark.parametrize("error", TRANSIENT)
    def test_a_failure_the_host_will_clear_is_transient(self, error):
        with pytest.raises(FreshIssueReadError) as raised:
            _reader(error).read_issue_labels(SUBJECT)
        assert raised.value.transient is True

    @pytest.mark.parametrize("error", LASTING)
    def test_a_failure_that_will_not_clear_by_itself_is_not(self, error):
        with pytest.raises(FreshIssueReadError) as raised:
            _reader(error).read_issue_labels(SUBJECT)
        assert raised.value.transient is False


def _gate(reader) -> ReconciliationGate:
    return ReconciliationGate(fresh_issue_reader=reader, reconcile=True)


class TestTheGateDefersOnlyWhatItCouldNotRead:
    @pytest.mark.parametrize("error", TRANSIENT)
    def test_a_transient_read_failure_is_a_deferral(self, error):
        with pytest.raises(ReconciliationDeferred):
            _gate(_reader(error)).require_state(build_expected_for_mutation(), SUBJECT)

    def test_the_deferral_still_carries_the_hosts_rate_limit(self):
        """So the action-liveness owner can wait for the reset (#7350)."""
        with pytest.raises(ReconciliationDeferred) as raised:
            _gate(_reader(_rate_limited())).require_state(
                build_expected_for_mutation(), SUBJECT
            )
        assert host_rate_limit_of(raised.value) is not None

    @pytest.mark.parametrize("error", LASTING)
    def test_a_lasting_read_failure_still_fails_closed_as_drift(self, error):
        with pytest.raises(ReconciliationRequired) as raised:
            _gate(_reader(error)).require_state(build_expected_for_mutation(), SUBJECT)
        assert not isinstance(raised.value, ReconciliationDeferred)

    def test_no_reader_wired_still_fails_closed_as_drift(self):
        with pytest.raises(ReconciliationRequired) as raised:
            _gate(None).require_state(build_expected_for_mutation(), SUBJECT)
        assert not isinstance(raised.value, ReconciliationDeferred)

    def test_labels_that_really_disagree_are_drift(self):
        reader = Mock()
        reader.read_issue_labels.return_value = ["blocked"]
        with pytest.raises(ReconciliationRequired) as raised:
            _gate(reader).require_state(
                ExpectedState.with_labels(required={"in-progress"}), SUBJECT
            )
        assert not isinstance(raised.value, ReconciliationDeferred)


class _Labels:
    def __init__(self) -> None:
        self.added: list[tuple[int, str]] = []

    def add_label(self, issue_number, label):
        self.added.append((issue_number, label))

    def remove_label(self, issue_number, label):  # pragma: no cover - unused
        raise AssertionError("no removal expected")

    def list_labels(self, issue_number):
        return []


def _support_over(reader, events):
    """The REAL plan applier over the real applier and real gate."""
    from issue_orchestrator.control.orchestrator_support import (
        OrchestratorSupport,
        pause_issue_for_reconciliation,
    )
    from issue_orchestrator.domain.models import OrchestratorState
    from issue_orchestrator.domain.pause_state import PauseState
    from tests.runtime_lifecycle_helpers import make_action_applier

    labels = _Labels()
    applier = make_action_applier(
        labels=labels, sessions=MagicMock(), events=events,
        fresh_issue_reader=reader, reconcile=True,
    )
    context = MagicMock()
    context.enrich = Mock(side_effect=lambda d: d)
    cleanup = MagicMock()
    cleanup.should_retry_tech_lead_issue = Mock(return_value=True)
    support = OrchestratorSupport(
        config=MagicMock(), events=events, repository_host=MagicMock(),
        state=OrchestratorState(pause_state=PauseState.running()), event_context=context,
        session_manager=MagicMock(), action_applier=applier, fact_gatherer=MagicMock(),
        planner=MagicMock(), worktree_manager=MagicMock(), state_machine_manager=MagicMock(),
        cleanup_manager=cleanup, get_review_machine=Mock(), kill_session=Mock(),
    )

    def pause(number, reason):
        pause_issue_for_reconciliation(events, applier, context, number, reason)

    return support, labels, pause


class _Events:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event) -> None:
        self.events.append(event)

    def named(self, name):
        return [e for e in self.events if e.name == name]


class _FlakyReader:
    """#410's read fails on the transport; every other issue reads fine."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def read_issue_labels(self, issue_number):
        if issue_number == SUBJECT:
            raise FreshIssueReadError(
                f"could not read fresh labels for issue #{issue_number}: {self._error}",
                transient=True,
            ) from self._error
        return []


def _plan() -> Plan:
    return Plan(actions=(
        AddLabelAction(issue_number=SUBJECT, label="pr-pending", reason="done",
                       expected=build_expected_for_mutation()),
        AddLabelAction(issue_number=OTHER, label="needs-code-review", reason="r",
                       expected=build_expected_for_mutation()),
    ), skipped=())


def test_a_transient_read_failure_defers_the_subject_without_pausing_it():
    """porchpin #410: no ``io:needs-reconcile`` for a network blip. The subject
    is withheld for the tick (a visible failed step), everyone else runs, and
    the next tick simply tries again."""
    events = _Events()
    support, labels, pause = _support_over(
        _FlakyReader(GitHubTransportError("connection reset")), events
    )

    support.apply_plan(_plan(), pause)

    assert (SUBJECT, get_pause_label()) not in labels.added
    assert labels.added == [(OTHER, "needs-code-review")]
    assert events.named(EventName.ISSUE_PAUSED_RECONCILE) == []
    [refusal] = events.named(EventName.RECONCILIATION_REQUIRED)
    assert (refusal.data["issue_number"], refusal.data["response"]) == (SUBJECT, "defer")


def test_a_real_disagreement_still_pauses_the_subject():
    events = _Events()
    reader = Mock()
    reader.read_issue_labels.side_effect = lambda n: ["blocked"] if n == SUBJECT else []
    support, labels, pause = _support_over(reader, events)

    support.apply_plan(Plan(actions=(
        AddLabelAction(issue_number=SUBJECT, label="pr-pending", reason="done",
                       expected=build_expected_for_mutation(forbidden={"blocked"})),
    ), skipped=()), pause)

    assert (SUBJECT, get_pause_label()) in labels.added
    [refusal] = events.named(EventName.RECONCILIATION_REQUIRED)
    assert refusal.data["response"] == "pause"
