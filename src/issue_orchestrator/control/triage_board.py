"""Triage board publisher: the projection sink for triage facts (#6781).

Runs at fact-gathering time, whenever the triage anchor scan produced facts
(the health-review-relevant cadence). Two fire-and-forget effects, in the
same spirit as the fact gatherer's event sink — projections of observed
facts, never decisions:

1. retain the latest open case-file facts behind a read-only injected reader
   so the board snapshot builder projects the ledger into later snapshots;
2. render the operator-facing triage board markdown
   (:mod:`..view_models.triage_board`) and write it to
   ``.issue-orchestrator/state/triage-board.md``, throttled by content
   comparison — the render is deterministic and hour-granular, so unchanged
   ledgers cost zero writes.

Publish failures are logged, never raised: a projection write must not fail
the planning tick (the ledgers themselves stay authoritative in SQLite).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ..infra.repo_identity import state_dir
from ..view_models.triage_board import build_triage_board_view, render_triage_board_md
from .triage_case_files import case_file_area_counts

if TYPE_CHECKING:
    from ..domain.models import TriageFacts
    from ..domain.triage_session import TriageCaseFileSummary, TriageShippedFixSummary
    from ..ports.triage_authority import TriageAuthorityStore
    from ..view_models.triage_board import TriageBoardView

logger = logging.getLogger(__name__)

TRIAGE_BOARD_FILENAME = "triage-board.md"


def triage_board_path(repo_root: Path | str) -> Path:
    """Canonical on-disk home of the rendered triage board (single owner)."""
    return state_dir(repo_root) / TRIAGE_BOARD_FILENAME


class TriageBoardPublisher:
    """Projects each triage-armed fact scan onto state + the board file."""

    def __init__(
        self,
        *,
        board_path: Path,
        authority: "Optional[TriageAuthorityStore]",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._board_path = board_path
        self._authority = authority
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_rendered: str | None = None
        self._case_files: tuple[TriageCaseFileSummary, ...] = ()

    def case_files(self) -> tuple["TriageCaseFileSummary", ...]:
        """Return the latest immutable anchor-scan case-file projection."""
        return self._case_files

    def shipped_fixes(self, limit: int) -> tuple["TriageShippedFixSummary", ...]:
        """Return restart-safe merged-fix facts from the durable ledger."""
        if self._authority is None:
            return ()
        return self._authority.list_recent_shipped_fixes(limit=limit)

    def publish(self, facts: "TriageFacts", *, last_health_review_at: float) -> None:
        """Retain the scan facts and refresh the local board projection."""
        self._case_files = facts.open_case_files
        try:
            rendered = render_triage_board_md(
                self._build_view(facts, last_health_review_at)
            )
        except Exception:
            logger.warning("[triage-board] Failed to render board", exc_info=True)
            return
        if rendered == self._last_rendered:
            return
        try:
            self._board_path.parent.mkdir(parents=True, exist_ok=True)
            self._board_path.write_text(rendered, encoding="utf-8")
        except OSError:
            logger.warning(
                "[triage-board] Failed to write %s", self._board_path, exc_info=True
            )
            return
        self._last_rendered = rendered
        logger.debug("[triage-board] Board written: %s", self._board_path)

    def _build_view(
        self, facts: "TriageFacts", last_health_review_at: float
    ) -> "TriageBoardView":
        return build_triage_board_view(
            ops=self._authority.list_ops() if self._authority is not None else (),
            case_files=facts.open_case_files,
            area_counts=case_file_area_counts(facts.open_case_files),
            last_health_review_at=last_health_review_at,
            now=self._clock(),
        )
