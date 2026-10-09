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
from collections.abc import Callable, Iterable, Mapping
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
    covered_rulings_prompt,
    parse_rulings_block,
    rulings_prompt,
    with_rulings_block,
)
from ..infra.logging_config import issue_log
from ..ports.repository_host import RepositoryHostError
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

    #: A FRESH read of the issue (its body); None when GitHub has no such issue
    #: (a read that fails raises).
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

    def unrecordable_all(self, issue_number: int, rulings: tuple[StandingRuling, ...]) -> str | None:
        """Why recording *rulings* on the issue, in order, would fail now (an
        unreadable issue, a malformed rulings block, a body that would not fit
        them all), else None. Reads only: a caller with several writes checks
        them together before its first (#8691)."""
        try:
            issue = self._read(issue_number)
            current = self._parse(issue_number, issue.body)
            present = {existing.ruling_id for existing in current}
            added = tuple(ruling for ruling in rulings if ruling.ruling_id not in present)
            if added:
                with_rulings_block(issue.body, (*current, *added))
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

    def covered_section(self, covered: Mapping[int, tuple[int, ...]]) -> str | None:
        """What a tech-lead run over other issues' work is bound by (#8347): each
        covered issue's rulings, one fresh body read each, synced into the index.

        A covered number GitHub has no issue for (a PR branch named ``2024-...``,
        a link to a deleted issue) binds nothing: there is no body to hold a
        ruling. A read that fails, or a damaged block, still raises.
        """
        rulings: dict[int, tuple[StandingRuling, ...]] = {}
        for number in sorted(covered):
            with self._lock:
                issue = self.read_issue(number)
                if issue is None:
                    logger.warning(issue_log(number, "Covered by a tech-lead run, but GitHub has no such issue"))
                    rulings[number] = ()
                    continue
                rulings[number] = self._parse(number, issue.body)
                self.index.save(number, rulings[number])
        return covered_rulings_prompt(covered, rulings)

    def backfill(self, issues: Iterable["Issue"]) -> int:
        """Bring the index up to the bodies of *issues* (a listing just read);
        how many issues it re-read (#8347).

        The index was created after rulings were already on issues, and the page
        reads only the index, so a ruling recorded earlier, or hand-edited on
        GitHub since the engine last read the issue, never showed. A listed body
        whose block matches the index costs nothing. One that differs (or is
        damaged) is never trusted as is: a listing may predate a ruling recorded
        or retired since, so the issue is read fresh through :meth:`active`,
        under the writers' lock, and the body read then decides. A body still
        unreadable or damaged is left to the prompts and reviews that refuse it;
        a GitHub failure stops the backfill (the next startup resumes it).
        """
        refreshed = 0
        for issue in issues:
            try:
                listed: tuple[StandingRuling, ...] | None = parse_rulings_block(issue.body)
            except RulingsBlockError:
                listed = None
            if listed is not None and listed == (self.index.load(issue.number) or ()):
                continue
            try:
                self.active(issue.number)
            except StandingRulingsUnavailable as error:
                logger.warning(issue_log(issue.number, "Standing rulings not indexed: %s"), error)
                continue
            except RepositoryHostError as error:
                logger.warning("Standing rulings index backfill stopped: %s", error)
                break
            refreshed += 1
        if refreshed:
            logger.info("Standing rulings index backfilled from %d fresh read(s)", refreshed)
        return refreshed

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
