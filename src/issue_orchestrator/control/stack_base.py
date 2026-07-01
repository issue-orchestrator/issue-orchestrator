"""Shared stack base-selection decision contract (ADR-0029, #6596).

A single typed result so every stack-sensitive path — worktree launch,
validation retry, rework, non-fast-forward push retry, and PR creation — consumes
stack base selection the *same* way and can never confuse three distinct
outcomes:

- **non-stack**: the slice is an ordinary issue; use the configured/default base.
- **allowed stack**: the slice is a stack successor whose gate is open;
  ``base_branch`` is the predecessor branch to build on (``None`` only once the
  predecessor has merged, meaning "use the default base").
- **blocked stack**: the slice is (or may be) a stack successor whose gate is
  closed — predecessor not ready, ambiguous base, base conflict, stale, or the
  issue could not be read. The caller must fail closed (no launch / no
  default-base rebase / no publish), never fall back to the default base.

Before this contract these paths reduced the gate report to ``str | None``, where
``None`` ambiguously meant both "not a stack" and "blocked/ambiguous stack", so
default-base behavior leaked into stack-sensitive paths. The single classifier
:meth:`StackBaseDecision.from_stack_report` keeps the open/blocked rule in one
place for both the work gate (launch/retry/rework/push-retry) and the publish
gate (PR creation).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.dependency_gates import DependencyGateReport, Gate


@dataclass(frozen=True)
class StackBaseDecision:
    """A stack base-selection verdict for one slice (ADR-0029 / #6596).

    ``is_stack`` is False for an ordinary issue (the other fields inert). For a
    stack successor, ``allowed`` says whether the relevant gate is open;
    ``base_branch`` is the predecessor branch to base on (``None`` means "use the
    caller's normal base", e.g. once the predecessor has merged); ``reason``
    carries the blocked-gate diagnostic; and ``retryable`` marks a block that
    should be retried later (e.g. a transient issue-read failure) rather than
    treated as permanent.

    Callers MUST key fail-closed behavior off ``allowed`` (not ``is_stack``):
    a fail-closed read error blocks even though the gate could not confirm the
    slice is a stack successor.
    """

    is_stack: bool
    allowed: bool
    base_branch: str | None = None
    reason: str | None = None
    retryable: bool = False

    @classmethod
    def not_stack(cls) -> "StackBaseDecision":
        return cls(is_stack=False, allowed=True)

    @classmethod
    def allowed_on(cls, base_branch: str | None) -> "StackBaseDecision":
        return cls(is_stack=True, allowed=True, base_branch=base_branch)

    @classmethod
    def blocked(
        cls, reason: str, *, retryable: bool, is_stack: bool = True
    ) -> "StackBaseDecision":
        return cls(
            is_stack=is_stack, allowed=False, reason=reason, retryable=retryable
        )

    @classmethod
    def from_stack_report(
        cls, report: DependencyGateReport, gate: Gate
    ) -> "StackBaseDecision":
        """Classify a confirmed stack successor's gate report into a decision.

        The single open/blocked rule shared by the work gate (launch, validation
        retry, rework, push retry) and the publish gate (PR creation): when the
        named gate is open the successor may proceed on ``stack_base_branch``;
        otherwise it is blocked with the gate's machine-readable reason summary.
        """
        gate_decision = report.gate(gate)
        if gate_decision.is_open:
            return cls.allowed_on(report.stack_base_branch)
        return cls.blocked(
            f"Stack {gate.value} gate blocked: {gate_decision.summary()}",
            retryable=False,
        )
