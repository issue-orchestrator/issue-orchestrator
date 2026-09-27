"""Which completions are validated work that recovery owns (#7323).

Validated-work recovery exists for one thing: to publish, as the issue's PR, a
head that the issue's own coding run validated but did not get published. Only
a run whose role produces the issue's deliverable is that - a coding or rework
run.

A tech-lead run (failure investigation, health review) is recorded against its
SUBJECT issue, but its branch is the tech lead's own. Its completion path
already decides what, if anything, that branch publishes. Recovery taking its
validated head as the subject's work puts a blocking ``recovery-pending`` on the
subject (or the health-review anchor) that no publication ever releases. Review
runs publish nothing at all.

This is the ONE rule. Capture consults it before admitting a completion, and
recovery consults it before acting on a record admitted before the rule
existed.
"""

from .registered_completion import CompletionRunRole


def recovery_owns(role: CompletionRunRole) -> bool:
    """Whether this run's validated completion is work recovery may publish.

    The kind's ``capturable`` capability answers (#7347): coding, rework and an
    operator's historical import; never a tech-lead or review run.
    """
    return role.kind.capabilities.capturable


def outside_scope_reason(role: CompletionRunRole) -> str:
    """Why recovery does not own this run's completion. Refuses an owned role."""
    if recovery_owns(role):
        raise ValueError(f"a {role.kind.value} run's completion is recoverable work")
    return (
        f"a {role.kind.value} run ({role.agent_label}) produces no validated work "
        "for recovery to publish; its own completion owns its branch (#7323)"
    )
