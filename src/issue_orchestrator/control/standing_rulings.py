"""THE owner of an issue's standing rulings (#8141).

Every writer records through :meth:`StandingRulingsOwner.record` (an approved
``propose_decision``, an approved ``resolve_block`` answer, a maintainer's
ruling command) and every reader asks it (each agent prompt on the issue, the
review rule, the tech-lead page). Nothing else reads or writes the block.

Where a ruling lives:

* **On GitHub, in the issue body** (``domain/standing_ruling``): the durable,
  crash-safe record, and the place agents already read. A write re-reads the
  body fresh, replaces only the block, and the adapter verifies GitHub kept
  it, so a ruling is never reported recorded unless it is on the issue.
* **In the local index** (``ports/standing_rulings``): a mirror of the body,
  synced on every read the owner makes and written through after each body
  write. The tech-lead page reads it (the page makes no GitHub call).
  Prompts and the review rule never trust it alone: they read the body, one
  fresh read per launch or review, so a maintainer's hand edit, a ruling
  another engine recorded or retired, or a lost state directory is never
  invisible. The body is the truth.

A body that cannot be read, or whose block is malformed, raises
:class:`~..ports.standing_rulings.StandingRulingsUnavailable`: a session must
not launch, and a review must not be judged, without the rulings that bind it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING

from ..domain.session_kind import SessionKind
from ..domain.standing_ruling import (
    RulingAuthority,
    RulingsBlockError,
    RulingScope,
    StandingRuling,
    audience_for,
    parse_rulings_block,
    rulings_prompt,
    with_rulings_block,
)
from ..infra.logging_config import issue_log
from ..ports.standing_rulings import StandingRulingsIndex, StandingRulingsUnavailable, SyncedRulings

if TYPE_CHECKING:
    from ..ports.issue import Issue

logger = logging.getLogger(__name__)


class RecordOutcome(StrEnum):
    RECORDED = "recorded"
    #: A ruling with this id is on the issue already (a replay): nothing written.
    ALREADY_RECORDED = "already_recorded"


class RetireOutcome(StrEnum):
    RETIRED = "retired"
    NOT_FOUND = "not_found"


@dataclass
class StandingRulingsOwner:
    """Records, retires and answers for an issue's standing rulings (module docstring)."""

    #: A FRESH read of the issue (its body); None when it cannot be read.
    read_issue: Callable[[int], "Issue | None"]
    #: Replace the issue's body; raises unless GitHub verifiably kept it.
    write_body: Callable[[int, str], None]
    index: StandingRulingsIndex
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- writers --------------------------------------------------------------

    def ruling(
        self,
        *,
        ruling_id: str,
        text: str,
        authority: RulingAuthority,
        source: str,
        scope: RulingScope,
    ) -> StandingRuling:
        """A new ruling stamped now (its validation raises on bad input)."""
        return StandingRuling(
            ruling_id=ruling_id, text=text.strip().replace("\r\n", "\n"), authority=authority,
            source=source, scope=scope, recorded_at=self.clock().isoformat(),
        )

    def unrecordable(self, issue_number: int, ruling: StandingRuling) -> str | None:
        """Why :meth:`record` would fail for *ruling* now (an unreadable issue, a
        malformed rulings block, a block that would not fit), else None. Reads only:
        a caller with several writes checks this before its first (#8691)."""
        try:
            issue = self._read(issue_number)
            current = self._parse(issue_number, issue.body)
            if not any(existing.ruling_id == ruling.ruling_id for existing in current):
                with_rulings_block(issue.body, (*current, ruling))
        except (StandingRulingsUnavailable, RulingsBlockError) as error:
            return str(error)
        return None

    def record(self, issue_number: int, ruling: StandingRuling) -> RecordOutcome:
        """Put *ruling* on the issue, create-once by its id. Raises on any failure."""
        with self._lock:
            issue = self._read(issue_number)
            current = self._parse(issue_number, issue.body)
            if any(existing.ruling_id == ruling.ruling_id for existing in current):
                self.index.save(issue_number, current)
                return RecordOutcome.ALREADY_RECORDED
            updated = (*current, ruling)
            self.write_body(issue_number, with_rulings_block(issue.body, updated))
            self.index.save(issue_number, updated)
        logger.info(issue_log(issue_number, "Standing ruling %s recorded (%s, %s): %s"),
                    ruling.ruling_id, ruling.authority.value, ruling.source, ruling.summary)
        return RecordOutcome.RECORDED

    def retire(self, issue_number: int, ruling_id: str) -> RetireOutcome:
        """Take a ruling off the issue (a maintainer's own act)."""
        with self._lock:
            issue = self._read(issue_number)
            current = self._parse(issue_number, issue.body)
            kept = tuple(ruling for ruling in current if ruling.ruling_id != ruling_id)
            if len(kept) == len(current):
                self.index.save(issue_number, current)
                return RetireOutcome.NOT_FOUND
            self.write_body(issue_number, with_rulings_block(issue.body, kept))
            self.index.save(issue_number, kept)
        logger.info(issue_log(issue_number, "Standing ruling %s retired"), ruling_id)
        return RetireOutcome.RETIRED

    # -- readers --------------------------------------------------------------

    def active(self, issue_number: int) -> tuple[StandingRuling, ...]:
        """The issue's rulings as its body records them NOW, synced into the index."""
        with self._lock:
            rulings = self._parse(issue_number, self._read(issue_number).body)
            self.index.save(issue_number, rulings)
        return rulings

    def prompt_section(self, issue_number: int, kind: SessionKind) -> str | None:
        rulings = self.active(issue_number)
        if not rulings:
            return None
        return rulings_prompt(issue_number, rulings, audience_for(kind))

    def synced(self) -> dict[int, SyncedRulings]:
        """Every issue the index holds rulings for, as of its last sync (the
        tech-lead page's read: it makes no GitHub call, so it shows the age)."""
        return self.index.synced()

    # -- internals ------------------------------------------------------------

    def _read(self, issue_number: int) -> "Issue":
        issue = self.read_issue(issue_number)
        if issue is None:
            raise StandingRulingsUnavailable(f"issue #{issue_number} could not be read for its standing rulings")
        return issue

    @staticmethod
    def _parse(issue_number: int, body: str | None) -> tuple[StandingRuling, ...]:
        try:
            return parse_rulings_block(body)
        except RulingsBlockError as error:
            raise StandingRulingsUnavailable(f"issue #{issue_number}'s standing rulings: {error}") from error


__all__ = ["RecordOutcome", "RetireOutcome", "StandingRulingsOwner"]
