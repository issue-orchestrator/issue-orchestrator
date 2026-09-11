"""Typed issue-lifecycle attempt shown by the issue-detail timeline."""

from .lifecycle_semantics import IssueCycle, LifecycleBase, OutcomeBadge


class Attempt(LifecycleBase):
    """A logical-run grouping of issue cycles for the drawer view.

    The name follows the user-facing vocabulary established in #6322:
    Test run → Test → Issue → Attempt → Cycle → Event. The session
    recording ``run_id`` is a filesystem identity and keeps its existing name.
    """

    attempt_number: int
    attempt_label: str
    outcome: OutcomeBadge
    attempt_key: str = ""
    run_id: str | None = None
    session_run_ids: tuple[str, ...] = ()
    timestamp: str = ""
    time_label: str = ""
    expanded: bool = False
    reset_from_scratch: bool = False
    cycles: tuple[IssueCycle, ...] = ()


__all__ = ["Attempt"]
