"""Port for orchestrator-owned triage launch authority (ADR-0031).

The agent-writable worktree carries copies of the triage assignment and PR
manifest for the *agent* to read; the orchestrator must never treat those
copies as authority (#6761 re-review F1). This port is the behavior seam the
control plane uses instead: it owns trusted launch scope plus the durable
proposal, pattern, and shipped-fix ledgers that triage policy consults across
process restarts.

Constructed once at the composition root (``entrypoints/bootstrap.py``) and
injected into the session launcher, the completion processor, and the
completion action planner. Tests mock this protocol; the durable SQLite
implementation lives in ``infra/triage_authority_store.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..domain.models import DiscoveredFailure
    from ..domain.triage_session import (
        StoredTriageOp,
        TriageLaunchAuthority,
        TriageShippedFixSummary,
    )


class TriageAuthorityConflictError(RuntimeError):
    """A different launch authority already exists for this session run."""


class TriageStormCohortConflictError(RuntimeError):
    """A different problem cohort already exists for this anchor issue."""


class TriageOpConflictError(RuntimeError):
    """A different stored op already exists for this proposal issue."""


class TriagePatternConflictError(RuntimeError):
    """A different case-file issue already exists for this pattern signature."""


class TriageShippedFixConflictError(RuntimeError):
    """Different shipped-fix evidence already exists for an issue."""


class TriageAuthorityStore(Protocol):
    """Durable trusted scope and operational ledgers for triage."""

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

    # -- Problem-storm cohorts (#6780) --------------------------------------
    #
    # The durable cohort ledger, keyed by the health-review ANCHOR issue
    # number. A storm collapses N per-issue failure investigations into one
    # anchor, so from that moment the cohort is the only record of which
    # problems the review owns — and the pending queue that carries it is
    # in-memory. This ledger is the recoverable boundary between the two
    # orchestrator-owned facts that outlive a tick:
    #
    #   * WHAT the review may act on. Launch records
    #     ``TriageLaunchAuthority.problem_issue_numbers`` from the cohort the
    #     anchor owns; a restart between anchor creation and launch would
    #     otherwise rehydrate an empty cohort and strip the review of its
    #     act-level scope.
    #   * WHICH run artifacts must survive. The cleanup-hold owner holds the
    #     cohort members' worktrees while the anchor is still referenced, so
    #     ``DiscoveredFailure.artifact_hints`` cannot outlive the files.
    #
    # Recorded create-once at anchor intake, rehydrated by startup recovery,
    # and discarded by the completion retention owner. The issue body is NOT
    # the authority: it is mutable human documentation.
    #
    # Retention: a row whose anchor is neither pending nor active is inert,
    # not load-bearing — every reader intersects the ledger with live pending
    # or active triage work, so a row leaked by an anchor that never reached
    # completion (e.g. dropped after exhausted launch retries) holds nothing
    # and grants nothing.

    def record_storm_cohort(
        self, *, anchor_issue_number: int, cohort: tuple["DiscoveredFailure", ...]
    ) -> None:
        """Persist the problem cohort for one anchor issue (create-once).

        Recording an identical cohort for an existing anchor is a no-op;
        recording a DIFFERENT cohort must raise
        :class:`TriageStormCohortConflictError` — the cohort is the review's
        act-level authority and its artifact-retention scope, so it must
        never silently change or expand after the anchor is created.
        """
        ...

    def load_storm_cohort(
        self, *, anchor_issue_number: int
    ) -> tuple["DiscoveredFailure", ...] | None:
        """Return an anchor's persisted cohort, or None when absent.

        None means "not a storm anchor" (a periodic health review has no
        cohort) — distinct from an empty tuple, which never gets recorded.
        """
        ...

    def discard_storm_cohort(self, *, anchor_issue_number: int) -> None:
        """Remove an anchor's cohort row. No-op if absent (retention owner)."""
        ...

    def list_storm_cohorts(
        self,
    ) -> tuple[tuple[int, tuple["DiscoveredFailure", ...]], ...]:
        """All (anchor_issue_number, cohort) rows — the cleanup-hold read.

        Once a health review LAUNCHES its pending queue item is removed, so
        this ledger is the only remaining carrier of the cohort its run still
        references. The hold owner reads the whole (small, storm-scoped)
        ledger once per tick rather than issuing a lookup per active session.
        """
        ...

    # -- Pattern case files (#6781) -----------------------------------------
    #
    # The durable flag_pattern ledger: one case-file issue per pattern
    # signature. Recorded create-once when the case-file issue is created;
    # subsequent flag_pattern proposals with the same signature look it up
    # and plan an evidence comment on the existing issue instead of a second
    # one. Rows are never discarded by the orchestrator — the case file IS
    # the accumulating artifact (graduation happens on GitHub).

    def record_pattern(self, *, signature: str, issue_number: int) -> None:
        """Persist the case-file issue for one signature (create-once).

        Recording the same issue for an existing signature is a no-op;
        recording a DIFFERENT issue must raise
        :class:`TriagePatternConflictError` — the signature keys exactly one
        evidence trail, which must never silently move.
        """
        ...

    def lookup_pattern(self, *, signature: str) -> int | None:
        """Return the case-file issue for a signature, or None when absent."""
        ...

    def list_patterns(self) -> tuple[tuple[str, int], ...]:
        """All (signature, case_file_issue_number) rows — the pattern ledger."""
        ...

    # -- Shipped-fix operational memory (#6781 amendment) -----------------

    def record_shipped_fix(
        self, *, issue_number: int, title: str, pr_url: str, area: str
    ) -> None:
        """Record an area-tagged fix at merge time (create-once by issue)."""
        ...

    def list_recent_shipped_fixes(
        self, *, limit: int
    ) -> tuple["TriageShippedFixSummary", ...]:
        """Newest persisted fixes, bounded for the agent-read board snapshot."""
        ...


class InMemoryTriageAuthorityStore:
    """In-memory store for tests."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], "TriageLaunchAuthority"] = {}
        self._ops: dict[int, "StoredTriageOp"] = {}
        self._patterns: dict[str, int] = {}
        self._shipped_fixes: dict[int, "TriageShippedFixSummary"] = {}
        self._storm_cohorts: dict[int, tuple["DiscoveredFailure", ...]] = {}

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

    def record_storm_cohort(
        self, *, anchor_issue_number: int, cohort: tuple["DiscoveredFailure", ...]
    ) -> None:
        existing = self._storm_cohorts.get(anchor_issue_number)
        if existing is not None:
            if existing == cohort:
                return
            raise TriageStormCohortConflictError(
                f"a different storm cohort is already recorded for anchor"
                f" issue #{anchor_issue_number}"
            )
        self._storm_cohorts[anchor_issue_number] = cohort

    def load_storm_cohort(
        self, *, anchor_issue_number: int
    ) -> tuple["DiscoveredFailure", ...] | None:
        return self._storm_cohorts.get(anchor_issue_number)

    def discard_storm_cohort(self, *, anchor_issue_number: int) -> None:
        self._storm_cohorts.pop(anchor_issue_number, None)

    def list_storm_cohorts(
        self,
    ) -> tuple[tuple[int, tuple["DiscoveredFailure", ...]], ...]:
        return tuple(sorted(self._storm_cohorts.items()))

    def record_pattern(self, *, signature: str, issue_number: int) -> None:
        existing = self._patterns.get(signature)
        if existing is not None:
            if existing == issue_number:
                return
            raise TriagePatternConflictError(
                f"pattern signature {signature!r} is already recorded for"
                f" case-file issue #{existing}"
            )
        self._patterns[signature] = issue_number

    def lookup_pattern(self, *, signature: str) -> int | None:
        return self._patterns.get(signature)

    def list_patterns(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(self._patterns.items()))

    def record_shipped_fix(
        self, *, issue_number: int, title: str, pr_url: str, area: str
    ) -> None:
        from ..domain.triage_session import TriageShippedFixSummary

        existing = self._shipped_fixes.get(issue_number)
        if existing is not None:
            # Titles are human metadata and may be edited between a durable
            # write and a crash-retry. PR + area are the evidence identity;
            # retain the original title rather than blocking reconciliation.
            if existing.pr_url == pr_url and existing.area == area:
                return
            raise TriageShippedFixConflictError(
                f"different shipped-fix evidence is already recorded for"
                f" issue #{issue_number}"
            )
        self._shipped_fixes[issue_number] = TriageShippedFixSummary(
            issue_number=issue_number,
            title=title,
            pr_url=pr_url,
            area=area,
            merged_at=datetime.now(timezone.utc).isoformat(),
        )

    def list_recent_shipped_fixes(
        self, *, limit: int
    ) -> tuple["TriageShippedFixSummary", ...]:
        if limit <= 0:
            raise ValueError("shipped-fix limit must be positive")
        return tuple(
            sorted(
                self._shipped_fixes.values(),
                key=lambda item: (item.merged_at, item.issue_number),
                reverse=True,
            )[:limit]
        )
