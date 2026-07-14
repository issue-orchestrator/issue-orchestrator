"""Port for orchestrator-owned triage launch authority (ADR-0031).

The agent-writable worktree carries copies of the triage assignment and PR
manifest for the *agent* to read; the orchestrator must never treat those
copies as authority (#6761 re-review F1). This port is the behavior seam the
control plane uses instead: the launch side records the immutable
:class:`TriageLaunchAuthority` for a session run, the completion side loads
it back as the only trusted scope, and terminal seams discard the row so
authority never outlives its run (#6769 F3).

Constructed once at the composition root (``entrypoints/bootstrap.py``) and
injected into the session launcher, the completion processor, and the
completion action planner. Tests mock this protocol; the durable SQLite
implementation lives in ``infra/triage_authority_store.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..domain.triage_session import StoredTriageOp, TriageLaunchAuthority


class TriageAuthorityConflictError(RuntimeError):
    """A different launch authority already exists for this session run."""


class TriageOpConflictError(RuntimeError):
    """A different stored op already exists for this proposal issue."""


class TriageAuthorityStore(Protocol):
    """Durable per-run storage for triage launch authority."""

    def record(
        self, *, run_id: str, session_name: str, authority: "TriageLaunchAuthority"
    ) -> None:
        """Persist the launch authority for one session run (create-once).

        Recording an identical payload for an existing key is a no-op;
        recording a DIFFERENT payload for an existing key must raise
        :class:`TriageAuthorityConflictError` — the record constrains the
        session's mutation scope, so it must never silently change or
        expand after launch (#6769 round 4).
        """
        ...

    def load(
        self, *, run_id: str, session_name: str
    ) -> "TriageLaunchAuthority | None":
        """Return the launch authority for a session run, or None when absent."""
        ...

    def discard(self, *, run_id: str, session_name: str) -> None:
        """Remove a run's authority row. No-op if absent (retention owner)."""
        ...

    # -- Gated proposal ops (#6778, ADR-0031 §2 amendment) -----------------
    #
    # The executable payload of a gated triage proposal, keyed by the
    # proposal ISSUE number. Recorded create-once when the proposal issue is
    # created; execution consumes only this record (the issue body is human
    # documentation); discarded after terminal handling so ops run at most
    # once. ``list_ops`` is the ledger read: proposal dedup per (op, target)
    # and the fact gatherer's approval classification both consult it.

    def record_op(self, *, issue_number: int, op: "StoredTriageOp") -> None:
        """Persist the op for one proposal issue (create-once).

        Recording an identical payload for an existing key is a no-op;
        recording a DIFFERENT payload must raise
        :class:`TriageOpConflictError` — the approver's consent binds to
        exactly one recorded payload, which must never silently change.
        """
        ...

    def load_op(self, *, issue_number: int) -> "StoredTriageOp | None":
        """Return the stored op for a proposal issue, or None when absent."""
        ...

    def discard_op(self, *, issue_number: int) -> None:
        """Remove a proposal issue's op row. No-op if absent (once-only owner)."""
        ...

    def list_ops(self) -> tuple[tuple[int, "StoredTriageOp"], ...]:
        """All (proposal_issue_number, op) rows — the open-proposal ledger."""
        ...


class InMemoryTriageAuthorityStore:
    """In-memory store for tests."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], "TriageLaunchAuthority"] = {}
        self._ops: dict[int, "StoredTriageOp"] = {}

    def record(
        self, *, run_id: str, session_name: str, authority: "TriageLaunchAuthority"
    ) -> None:
        existing = self._rows.get((run_id, session_name))
        if existing is not None:
            if existing == authority:
                return
            raise TriageAuthorityConflictError(
                f"launch authority already recorded for run_id={run_id!r} "
                f"session={session_name!r} with a different payload"
            )
        self._rows[(run_id, session_name)] = authority

    def load(
        self, *, run_id: str, session_name: str
    ) -> "TriageLaunchAuthority | None":
        return self._rows.get((run_id, session_name))

    def discard(self, *, run_id: str, session_name: str) -> None:
        self._rows.pop((run_id, session_name), None)

    def record_op(self, *, issue_number: int, op: "StoredTriageOp") -> None:
        existing = self._ops.get(issue_number)
        if existing is not None:
            if existing == op:
                return
            raise TriageOpConflictError(
                f"a different triage op is already recorded for proposal"
                f" issue #{issue_number}"
            )
        self._ops[issue_number] = op

    def load_op(self, *, issue_number: int) -> "StoredTriageOp | None":
        return self._ops.get(issue_number)

    def discard_op(self, *, issue_number: int) -> None:
        self._ops.pop(issue_number, None)

    def list_ops(self) -> tuple[tuple[int, "StoredTriageOp"], ...]:
        return tuple(sorted(self._ops.items()))
