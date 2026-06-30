"""Stack publish-gate owner for the completion/publish path (ADR-0029, #6596).

The completion processor knows an issue *number* and its *worktree* at PR
creation time, but not the issue body, milestone, or stack policy. This bounded
owner closes that gap: given ``(issue_number, worktree)`` it reads the issue,
asks the single :class:`DependencyEvaluator` gate owner for the *publish*
decision, and returns a small typed verdict the processor consumes — whether the
slice is a stack successor, whether publish is allowed, the predecessor branch
its PR must be based on, and a human reason when blocked.

Keeping this here (not in the processor) preserves the ADR-0029 contract that
stack policy has exactly one owner: the processor never re-derives base
selection or staleness, it asks this gate, which asks the evaluator, which
builds the one gate report.

Non-stack issues are short-circuited to an allow-with-no-base verdict so the
processor's existing base selection and PR behavior are untouched, and any issue
read failure fails *open* (a transient lookup error must not block an ordinary
publish — the merge gate and a later publish-time recheck still guard stack
correctness).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .dependency_evaluator import DependencyEvaluator

logger = logging.getLogger(__name__)


class _StackIssue(Protocol):
    """The slice of an issue the gate needs: its body and milestone."""

    @property
    def body(self) -> str | None: ...

    @property
    def milestone(self) -> str | None: ...


class StackIssueReader(Protocol):
    """Reads an issue (with body + milestone) by number for the gate."""

    def get_issue(self, issue_number: int) -> _StackIssue | None: ...


@dataclass(frozen=True)
class StackPublishDecision:
    """The publish verdict for one slice.

    ``is_stack`` is False for an ordinary issue (the other fields are inert).
    For a stack successor, ``allowed`` says whether the publish gate is open;
    ``base_branch`` is the predecessor branch the PR must target (``None`` means
    "use the processor's normal base", e.g. once the predecessor has merged); and
    ``reason`` carries the blocked-gate diagnostic when ``allowed`` is False.
    """

    is_stack: bool
    allowed: bool
    base_branch: str | None = None
    reason: str | None = None

    @classmethod
    def not_stack(cls) -> "StackPublishDecision":
        return cls(is_stack=False, allowed=True)


class StackPublishGate:
    """Owns the publish-time stack base selection + gate for the processor."""

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

    def decide(self, issue_number: int, worktree: Path) -> StackPublishDecision:
        try:
            issue = self._issue_reader.get_issue(issue_number)
        except Exception as exc:  # fail-open: a read error must not block publish
            logger.warning(
                "Stack publish gate could not read issue #%d (allowing normal "
                "publish): %s",
                issue_number,
                exc,
            )
            return StackPublishDecision.not_stack()
        if issue is None:
            return StackPublishDecision.not_stack()
        body = issue.body or ""
        # Cheap short-circuit: only Stack-after: slices consult the gate, so an
        # ordinary issue keeps its exact prior base selection and publish path.
        if "stack-after" not in body.lower():
            return StackPublishDecision.not_stack()

        report = self._evaluator.evaluate_publish_gate(
            issue_number,
            body,
            issue.milestone,
            worktree=worktree,
            configured_base_branch=self._configured_base_branch,
        )
        if not report.can_publish:
            reason = f"Stack publish gate blocked: {report.publish.summary()}"
            logger.warning("Issue #%d publish blocked: %s", issue_number, reason)
            return StackPublishDecision(is_stack=True, allowed=False, reason=reason)
        return StackPublishDecision(
            is_stack=True,
            allowed=True,
            base_branch=report.stack_base_branch,
        )
