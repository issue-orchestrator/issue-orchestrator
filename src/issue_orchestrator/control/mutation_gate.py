"""The optimistic-concurrency gate every mutation crosses (#6957 round-2 F4/A5).

``ActionApplier`` owns a lot; this is the one part of it that decides whether a
write may happen AT ALL, so it gets to be its own named owner rather than two
methods buried in a 1800-line dispatcher. Two rules, and they are the whole
module:

1. **Unknown is not empty.** The reader raises when it cannot read an issue
   (:class:`~..ports.fresh_issue_reader.FreshIssueReadError`), and this turns
   that into "unknown", never into an observed empty label set. The distinction
   is load-bearing: an empty set SATISFIES an expectation that merely forbids
   ``io:needs-reconcile``, so conflating them let a failed GitHub read walk the
   control plane straight through an operator pause.
2. **Unknown fails closed.** An action carrying expectations that cannot be
   verified raises ``ReconciliationRequired`` — the same outcome as a verified
   violation — so the orchestrator pauses the issue instead of guessing. An
   expectation this gate has not been wired to verify at all (one constraining
   the issue's own state with no snapshot reader present) is unknown in exactly
   that sense: it is refused, never silently narrowed to the labels-only check
   the gate could perform.
3. **Unknown is not drift.** A read that failed TRANSIENTLY (the port says so:
   a transport failure, a 5xx, a rate limit) raises the
   ``ReconciliationDeferred`` subclass: still a refusal everywhere, but the
   plan applier defers the subject for the tick instead of pausing it (#7379).

Actions with no ``ExpectedState``, and appliers with reconciliation disabled,
pass straight through; that is the pre-existing contract and this module does
not change it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Generic, TypeVar

from ..infra.logging_config import issue_log
from ..ports.fresh_issue_reader import (
    FreshIssueReadError,
    FreshIssueReader,
    FreshIssueSnapshotReader,
)
from .action_base import Action
from .reconciliation import (
    ExpectedState,
    ExternalSnapshot,
    ReconciliationDeferred,
    ReconciliationRequired,
    require_reconciliation,
)

_T = TypeVar("_T")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconciliationGate:
    """Reads current labels and refuses mutations the board no longer allows."""

    fresh_issue_reader: FreshIssueReader | None
    reconcile: bool
    # Needed only by expectations that constrain the ISSUE's own state. Left
    # None, such an expectation is UNVERIFIABLE and therefore refused — a gate
    # must never quietly downgrade to the labels-only check it can perform
    # (#7248 round 6 review F8/A3).
    fresh_issue_snapshot_reader: FreshIssueSnapshotReader | None = None

    def current_labels(self, issue_number: int) -> set[str] | None:
        """The issue's CURRENT labels, or None when they could not be OBSERVED.

        None means "unknown", never "empty" — see the module docstring.
        """
        return self._read_labels(issue_number).value

    def _read_labels(self, issue_number: int) -> "_Read[set[str]]":
        """Labels, or the typed failure that says why they are unknown.

        ``FreshIssueReadError`` is the port's explicit failure; the broad catch
        keeps any other adapter fault on the same fail-closed path rather than
        trusting a half-formed answer (and it is never transient).
        """
        if self.fresh_issue_reader is None:
            return _Read(None, None)
        try:
            return _Read(set(self.fresh_issue_reader.read_issue_labels(issue_number)), None)
        except FreshIssueReadError as exc:
            logger.warning(
                issue_log(
                    issue_number,
                    "Fresh label read failed; reconciliation state is unknown: %s",
                ),
                exc,
            )
            return _Read(None, exc)
        except Exception as exc:
            logger.warning(
                issue_log(issue_number, "Failed to fetch labels for reconciliation: %s"),
                exc,
            )
            return _Read(None, None)

    def require_expected(self, action: Action, issue_number: int) -> None:
        """Enforce *action*'s expected state before it mutates *issue_number*.

        Raises:
            ReconciliationRequired: the current state violates the expectation,
                or could not be observed at all.
        """
        if action.expected is None:
            return
        self.require_state(action.expected, issue_number)

    def current_snapshot(self, issue_number: int) -> ExternalSnapshot | None:
        """Labels AND issue state, or None when either could not be OBSERVED.

        One read for both facts, so they describe the same instant and cost one
        request. None carries the module's first rule: unknown, never a default.
        """
        return self._read_snapshot(issue_number).value

    def _read_snapshot(self, issue_number: int) -> "_Read[ExternalSnapshot]":
        if self.fresh_issue_snapshot_reader is None:
            return _Read(None, None)
        try:
            observed = self.fresh_issue_snapshot_reader.read_issue_snapshot(
                issue_number
            )
        except FreshIssueReadError as exc:
            logger.warning(
                issue_log(
                    issue_number,
                    "Fresh issue read failed; reconciliation state is unknown: %s",
                ),
                exc,
            )
            return _Read(None, exc)
        except Exception as exc:
            logger.warning(
                issue_log(issue_number, "Failed to fetch issue for reconciliation: %s"),
                exc,
            )
            return _Read(None, None)
        return _Read(ExternalSnapshot.for_issue(
            issue_number, set(observed.labels), issue_state=observed.state
        ), None)

    def require_state(self, expected: ExpectedState, issue_number: int) -> None:
        """Require one explicit expected state without manufacturing an action."""
        if not self.reconcile:
            return

        observed = self._observe(expected, issue_number)
        if observed.value is None:
            failure = observed.failure
            transient = failure is not None and failure.transient
            logger.warning(
                issue_log(
                    issue_number,
                    "Reconciliation required but cannot observe the issue"
                    " - deferring this tick" if transient else
                    "Reconciliation required but cannot observe the issue"
                    " - failing closed",
                ),
            )
            # Transient: refused this tick, NOT drift (#7379). Anything else --
            # no reader, auth, not found, an unreadable payload -- stays the
            # fail-closed refusal it always was.
            refusal_type = ReconciliationDeferred if transient else ReconciliationRequired
            raise refusal_type(
                entity_type="issue",
                entity_id=issue_number,
                expected=ExternalSnapshot.for_issue(
                    issue_number, set(expected.required_labels)
                ),
                actual=ExternalSnapshot.for_issue(issue_number, set()),
                reason="Cannot fetch current issue state to verify expected state",
            ) from failure

        # Raises ReconciliationRequired when the constraints are not satisfied.
        require_reconciliation(expected, observed.value, entity_type="issue")

    def _observe(
        self, expected: ExpectedState, issue_number: int
    ) -> "_Read[ExternalSnapshot]":
        """Read exactly the facts *expected* constrains, and no more.

        An expectation about labels alone keeps the cheap labels-only read every
        other mutation in the system already pays for. One that also constrains
        the issue's own state needs the snapshot reader, and when no snapshot
        reader is wired the honest answer is UNKNOWN: the gate cannot verify
        what it was asked to verify, so it fails closed rather than passing on
        the half of the expectation it happens to be able to check.
        """
        if expected.required_issue_state is None:
            labels = self._read_labels(issue_number)
            if labels.value is None:
                return _Read(None, labels.failure)
            return _Read(ExternalSnapshot.for_issue(issue_number, labels.value), None)
        if self.fresh_issue_snapshot_reader is None:
            logger.warning(
                issue_log(
                    issue_number,
                    "Expected state constrains the issue state, but no fresh"
                    " snapshot reader is wired - failing closed",
                ),
            )
            return _Read(None, None)
        return self._read_snapshot(issue_number)


@dataclass(frozen=True, slots=True)
class _Read(Generic[_T]):
    """A fresh read's value, or the typed failure that left it unknown.

    ``failure`` is None when the read failed in a way that carries no
    classification (no reader wired, a non-port fault): that is still unknown,
    and still fails closed.
    """

    value: _T | None
    failure: FreshIssueReadError | None
