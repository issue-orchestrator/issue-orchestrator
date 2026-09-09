"""Required reconciliation subjects for tech-lead mutation commands."""

from typing import Protocol, runtime_checkable

from .action_base import Action


#: A command whose reconciliation subject is "no managed-repo issue at all".
#: Only two tech-lead mutations legitimately have none: creating the ANCHOR
#: issue itself (there is nothing yet to reconcile against) and discarding
#: terminal ledger rows (a purely orchestrator-side write). Anything else with
#: this subject is a composition bug, and the dispatch guard fails it closed.
NO_RECONCILIATION_SUBJECT = 0


@runtime_checkable
class TechLeadMutation(Protocol):
    """A tech-lead command that MUTATES, and names what it reconciles against.

    Every mutating tech-lead command crosses the applier's optimistic-concurrency
    gate before it writes, and that gate needs an issue to read labels from. The
    dispatch table used to supply those subjects by hand for the four commands
    someone remembered, which is exactly why issue creation kept slipping past
    it: a case file or proposal could still be filed against a source anchor
    paused behind ``io:needs-reconcile`` (#6957 round-6 review F3/A3).

    So the SUBJECT is part of each command's own contract, not a lookup table
    the registry has to keep in sync. A command that mutates and does not
    implement this protocol cannot be dispatched.
    """

    def reconciliation_subject(self) -> int:
        """The managed-repo issue whose current labels gate this mutation.

        :data:`NO_RECONCILIATION_SUBJECT` when the command genuinely has none.
        """
        ...


def reconciliation_subject_for(action: Action) -> int:
    """The managed-repo issue *action*'s mutation is checked against.

    Fails loudly for a mutating tech-lead command that never declared one:
    silently skipping the gate is the failure mode this replaces.
    """
    if not isinstance(action, TechLeadMutation):
        raise TypeError(
            f"{type(action).__name__} is dispatched as a mutating tech-lead"
            " command but does not implement TechLeadMutation; every mutating"
            " command must name the issue its reconciliation is checked against"
        )
    return action.reconciliation_subject()
