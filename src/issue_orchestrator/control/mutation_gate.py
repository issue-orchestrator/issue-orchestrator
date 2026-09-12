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

Actions with no ``ExpectedState``, and appliers with reconciliation disabled,
pass straight through; that is the pre-existing contract and this module does
not change it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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
    ReconciliationRequired,
    require_reconciliation,
)

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
        ``FreshIssueReadError`` is the port's explicit failure; the broad catch
        keeps any other adapter fault on the same fail-closed path rather than
        trusting a half-formed answer.
        """
        if self.fresh_issue_reader is None:
            return None
        try:
            return set(self.fresh_issue_reader.read_issue_labels(issue_number))
        except FreshIssueReadError as exc:
            logger.warning(
                issue_log(
                    issue_number,
                    "Fresh label read failed; reconciliation state is unknown: %s",
                ),
                exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                issue_log(issue_number, "Failed to fetch labels for reconciliation: %s"),
                exc,
            )
            return None

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
        if self.fresh_issue_snapshot_reader is None:
            return None
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
            return None
        except Exception as exc:
            logger.warning(
                issue_log(issue_number, "Failed to fetch issue for reconciliation: %s"),
                exc,
            )
            return None
        return ExternalSnapshot.for_issue(
            issue_number, set(observed.labels), issue_state=observed.state
        )

    def require_state(self, expected: ExpectedState, issue_number: int) -> None:
        """Require one explicit expected state without manufacturing an action."""
        if not self.reconcile:
            return

        observed = self._observe(expected, issue_number)
        if observed is None:
            logger.warning(
                issue_log(
                    issue_number,
                    "Reconciliation required but cannot observe the issue"
                    " - failing closed",
                ),
            )
            raise ReconciliationRequired(
                entity_type="issue",
                entity_id=issue_number,
                expected=ExternalSnapshot.for_issue(
                    issue_number, set(expected.required_labels)
                ),
                actual=ExternalSnapshot.for_issue(issue_number, set()),
                reason="Cannot fetch current issue state to verify expected state",
            )

        # Raises ReconciliationRequired when the constraints are not satisfied.
        require_reconciliation(expected, observed, entity_type="issue")

    def _observe(
        self, expected: ExpectedState, issue_number: int
    ) -> ExternalSnapshot | None:
        """Read exactly the facts *expected* constrains, and no more.

        An expectation about labels alone keeps the cheap labels-only read every
        other mutation in the system already pays for. One that also constrains
        the issue's own state needs the snapshot reader, and when no snapshot
        reader is wired the honest answer is UNKNOWN: the gate cannot verify
        what it was asked to verify, so it fails closed rather than passing on
        the half of the expectation it happens to be able to check.
        """
        if expected.required_issue_state is None:
            labels = self.current_labels(issue_number)
            return (
                None
                if labels is None
                else ExternalSnapshot.for_issue(issue_number, labels)
            )
        if self.fresh_issue_snapshot_reader is None:
            logger.warning(
                issue_log(
                    issue_number,
                    "Expected state constrains the issue state, but no fresh"
                    " snapshot reader is wired - failing closed",
                ),
            )
            return None
        return self.current_snapshot(issue_number)
