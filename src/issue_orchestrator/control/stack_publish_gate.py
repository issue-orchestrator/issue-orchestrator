"""Stack base-gate owner for the completion/publish path (ADR-0029, #6596).

The completion processor knows an issue *number* and (at publish time) its
*worktree*, but not the issue body, milestone, or stack policy. This bounded
owner closes that gap: given ``(issue_number[, worktree])`` it reads the issue,
asks the single :class:`DependencyEvaluator` gate owner for the relevant gate
decision, and returns a shared :class:`StackBaseDecision` the processor consumes
— whether the slice is a stack successor, whether the gate is open, the
predecessor branch to base on, and a human reason when blocked.

Two questions share this owner so stack base selection has exactly one rule:

- :meth:`decide_publish` — the *publish* gate (with successor-vs-predecessor
  ancestry), used when creating/reusing a PR.
- :meth:`decide_work` — the *work* gate (no ancestry; ancestry is the thing a
  rebase fixes), used by the non-fast-forward push retry to pick the rebase base.

Keeping this here (not in the processor) preserves the ADR-0029 contract that
stack policy has exactly one owner: the processor never re-derives base
selection or staleness, it asks this gate, which asks the evaluator, which
builds the one gate report.

Non-stack issues are short-circuited to a non-stack verdict so existing base
selection and PR behavior are untouched. An issue read failure (or a managed
publish whose issue cannot be found) fails *closed* with a retryable blocked
verdict: because the gate cannot prove the slice is *not* a stack successor
without the body, allowing it would let a real successor open/push on the wrong
base before any later gate sees it (ADR-0029 "facts over labels", and the repo's
fail-fast stance). The block is marked retryable so a transient lookup error
simply re-runs next tick rather than permanently blocking.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from ..domain.dependency_gates import Gate
from .dependency_evaluator import DependencyEvaluator
from .stack_base import StackBaseDecision

logger = logging.getLogger(__name__)

# Backwards-compatible alias: this verdict is now shared across launch, push
# retry, and publish, so its canonical home is ``stack_base.StackBaseDecision``.
StackPublishDecision = StackBaseDecision


class _StackIssue(Protocol):
    """The slice of an issue the gate needs: its body and milestone."""

    @property
    def body(self) -> str | None: ...

    @property
    def milestone(self) -> str | None: ...


class StackIssueReader(Protocol):
    """Reads an issue (with body + milestone) by number for the gate."""

    def get_issue(self, issue_number: int) -> _StackIssue | None: ...


class StackBaseGate:
    """Owns stack base selection + gating for the completion processor."""

    def __init__(
        self,
        *,
        evaluator: DependencyEvaluator,
        issue_reader: StackIssueReader,
        configured_base_branch: str | None = None,
    ) -> None:
        self._evaluator = evaluator
        self._issue_reader = issue_reader
        self._configured_base_branch = configured_base_branch

    def _read_stack_issue(
        self, issue_number: int, *, context: str
    ) -> _StackIssue | StackBaseDecision | None:
        """Read the issue, or a fail-closed decision, or ``None`` for non-stack.

        Returns the issue when it is a stack successor, ``None`` when it is an
        ordinary issue (caller uses its normal base), or a blocked
        :class:`StackBaseDecision` when the issue cannot be read/found — the gate
        cannot prove the slice is *not* a stack successor, so it fails closed.
        """
        try:
            issue = self._issue_reader.get_issue(issue_number)
        except Exception as exc:  # fail-closed: cannot prove this is not a stack
            logger.warning(
                "Stack %s gate could not read issue #%d (blocking, retryable): %s",
                context,
                issue_number,
                exc,
            )
            return StackBaseDecision.blocked(
                f"Stack {context} gate could not read issue #{issue_number}: {exc}",
                retryable=True,
                is_stack=False,
            )
        if issue is None:
            logger.warning(
                "Stack %s gate found no issue #%d for a managed run "
                "(blocking, retryable)",
                context,
                issue_number,
            )
            return StackBaseDecision.blocked(
                f"Stack {context} gate found no issue #{issue_number} to confirm "
                "stack base",
                retryable=True,
                is_stack=False,
            )
        # Cheap short-circuit: only Stack-after: slices consult the gate, so an
        # ordinary issue keeps its exact prior base selection.
        if "stack-after" not in (issue.body or "").lower():
            return None
        return issue

    def decide_publish(self, issue_number: int, worktree: Path) -> StackBaseDecision:
        """Stack PR base decision (publish gate, includes ancestry)."""
        read = self._read_stack_issue(issue_number, context="publish")
        if isinstance(read, StackBaseDecision):
            return read
        if read is None:
            return StackBaseDecision.not_stack()
        report = self._evaluator.evaluate_publish_gate(
            issue_number,
            read.body or "",
            read.milestone,
            worktree=worktree,
            configured_base_branch=self._configured_base_branch,
        )
        decision = StackBaseDecision.from_stack_report(report, Gate.PUBLISH)
        if not decision.allowed:
            logger.warning("Issue #%d publish blocked: %s", issue_number, decision.reason)
        return decision

    def decide_work(self, issue_number: int) -> StackBaseDecision:
        """Stack base decision for the work gate (no ancestry).

        Used by the non-fast-forward push retry to choose the rebase base: a
        rebase is what *fixes* ancestry, so the work gate (which omits the
        successor-vs-predecessor ancestry check) is the right authority for
        "may this successor build on its predecessor base, and which branch".
        """
        read = self._read_stack_issue(issue_number, context="work")
        if isinstance(read, StackBaseDecision):
            return read
        if read is None:
            return StackBaseDecision.not_stack()
        report = self._evaluator.evaluate_work_gate(
            issue_number,
            read.body or "",
            read.milestone,
            configured_base_branch=self._configured_base_branch,
            emit_event=False,
        )
        decision = StackBaseDecision.from_stack_report(report, Gate.WORK)
        if not decision.allowed:
            logger.warning("Issue #%d work blocked: %s", issue_number, decision.reason)
        return decision


# Backwards-compatible alias for the renamed owner.
StackPublishGate = StackBaseGate

