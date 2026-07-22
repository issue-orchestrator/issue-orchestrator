"""Port: board-snapshot provision for tech_lead session launches (ADR-0031 §3).

The session launcher writes a board snapshot into every tech_lead session's
``tech-lead-data/`` directory, but it never holds ``OrchestratorState`` itself.
This Protocol is that seam: the composition root wires an implementation
(``control.board_snapshot_builder.StateBoardSnapshotProvider``) that reads
the live state — launches run inside the tick under the state lock, so the
read is safe — and delegates to the control-layer ``BoardSnapshotBuilder``.

All board data is local orchestrator state; implementations must make no
GitHub/network calls.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from ..domain.board_snapshot import BoardSnapshot


@runtime_checkable
class BoardSnapshotProvider(Protocol):
    """Builds a point-in-time board snapshot for a launching session."""

    def snapshot(
        self,
        focus_issue: int | None,
        problem_cohort: tuple[int, ...] = (),
    ) -> BoardSnapshot:
        """Return a bounded snapshot of the current orchestrator board.

        ``focus_issue`` pins that issue's timeline extract first (failure
        investigations); ``None`` for batch reviews.

        ``problem_cohort`` is the health review's OWNED act-level remit,
        supplied by the launch boundary that holds the grant (#6780). It is
        stamped onto the snapshot as a dedicated surface rather than inferred
        from ``recent_failures``, which is board CONTEXT and includes
        unrelated pending investigations. Empty for every other flavor and for
        a periodic health review.
        """
        ...


class NullBoardSnapshotProvider:
    """Null object for tests: an empty but valid board snapshot.

    The provider is a REQUIRED SessionLauncher dependency (tech_lead prompts
    treat board-snapshot.json as authoritative input), so bare test
    constructions inject this instead of ``None`` — the launch still writes
    a well-formed snapshot file, and there is no silent skip path to mask
    wiring regressions.
    """

    def snapshot(
        self,
        _focus_issue: int | None,
        problem_cohort: tuple[int, ...] = (),
    ) -> BoardSnapshot:
        return BoardSnapshot(
            generated_at=datetime.now().isoformat(),
            orchestrator_paused=False,
            problem_cohort=list(problem_cohort),
        )
