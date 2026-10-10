"""Queue cache mutations and eligibility policy.

This module centralizes queue eligibility and mutations so call sites
cannot bypass scope policy when updating ``state.cached_queue_issues``.
"""

from __future__ import annotations

from dataclasses import dataclass, is_dataclass, replace
from enum import Enum
import logging
import time
import traceback
from typing import TYPE_CHECKING, cast

from ..domain.issue_cache_ledger import LabelWrite
from ..history import issues_held_by_session_history
from .issue_scope import issue_scope_skip_detail


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ..infra.config import Config
    from ..domain.issue_cache_ledger import IssueCacheLedger, IssueFetchEpoch
    from ..domain.models import OrchestratorState
    from ..ports.issue import Issue
    from ..ports.queue_cache_store import QueueCacheStore

logger = logging.getLogger(__name__)


class QueueMutationStatus(str, Enum):
    """Result status for queue cache mutation operations."""

    ACCEPTED = "accepted"
    REJECTED_OUT_OF_SCOPE = "rejected_out_of_scope"
    REJECTED_EXCLUDED = "rejected_excluded"


@dataclass(frozen=True)
class QueueMutationOutcome:
    """Outcome details from queue cache mutations."""

    status: QueueMutationStatus
    in_queue: bool
    updated: bool


_UI_VISIBILITY_STALENESS_SECONDS = 120
_SUSPICIOUS_SHRINK_MIN_REMOVALS = 10
_SUSPICIOUS_SHRINK_MIN_RATIO = 0.5
QUEUE_SHRINK_CONFIRM_DELAY_SECONDS = 60.0


class QueueCache:
    """Only writer for queue cache state.

    An instance is a view over one state, and callers build their own; what
    makes them one writer is the state's ``issue_cache_ledger`` (#8113). Every
    mutation any instance makes runs under it, so a refresh on the tick, an
    upsert from a web request and a label write from a background review
    exchange still change the cache in one order. A GitHub read is committed
    only with a :meth:`fetch` opened before the read, so the commit carries
    every label write that landed while the read was in flight.
    """

    def __init__(
        self,
        config: "Config",
        state: "OrchestratorState",
        queue_cache_store: "QueueCacheStore | None" = None,
    ):
        self._config = config
        self._state = state
        self._store = queue_cache_store
        self._ledger = state.issue_cache_ledger

    def fetch(self) -> "QueueCacheFetch":
        """Open a fetch; call it BEFORE the GitHub read whose result is committed.

        Commit what the read returned with :meth:`replace_from_refresh` or
        :meth:`upsert_refreshed_issue`, then close the fetch (it is a context
        manager). Opening after the read would let the commit put back a label
        the engine changed between the two.
        """
        return QueueCacheFetch(self._ledger)

    def replace_from_refresh(self, issues: list["Issue"], *, fetched: "QueueCacheFetch") -> list["Issue"]:
        """Install a refresh of the in-scope issues: what ``fetched`` read, plus every later write."""
        opened = fetched.opened_on(self._ledger)
        with self._ledger.serialised():
            return self._install_refresh(self._with_writes_since(opened, issues))

    def upsert_refreshed_issue(self, issue: "Issue", *, fetched: "QueueCacheFetch") -> QueueMutationOutcome:
        """Install one refreshed issue: what ``fetched`` read, plus every later write."""
        opened = fetched.opened_on(self._ledger)
        with self._ledger.serialised():
            [current] = self._with_writes_since(opened, [issue])
            return self._install_issue(current)

    def _with_writes_since(self, opened: "IssueFetchEpoch", issues: list["Issue"]) -> list["Issue"]:
        """``issues`` as read, carrying every label write recorded after the fetch opened.

        A write the read already saw changes nothing. One it may have missed
        was verified on GitHub after the fetch opened, so it wins: the read
        cannot be ordered against it, and at worst a person's change to the
        same label in that window waits for the next read. Scope is then
        judged on the result, so a write that adds an exclude label keeps the
        issue out exactly as a later refresh would.
        """
        writes = self._ledger.writes_since(opened)
        if not writes:
            return list(issues)
        current: list["Issue"] = []
        for issue in issues:
            for write in writes:
                if write.issue_number == issue.number:
                    issue = _with_label(issue, write.label, present=write.present) or issue
            current.append(issue)
        return current

    def _install_refresh(self, issues: list["Issue"]) -> list["Issue"]:
        """Replace queue from fetched issues using canonical eligibility policy."""
        prior_scope = list(self._state.cached_scope_issues)
        prior_queue = list(self._state.cached_queue_issues)
        prior_count = len(prior_queue)
        scope = [issue for issue in issues if _matches_scope(self._config, issue)]
        queue = [issue for issue in scope if self.evaluate_issue(issue) == QueueMutationStatus.ACCEPTED]
        retainable_removed_numbers = _retainable_removed_numbers(self, prior_queue, queue)
        if _is_suspicious_shrink(prior_count, len(retainable_removed_numbers)):
            if _pending_shrink_confirmed(self._state, retainable_removed_numbers):
                logger.warning(
                    "[QUEUE_CACHE] confirmed large queue shrink: prior=%d candidate=%d "
                    "removing=%d",
                    prior_count,
                    len(queue),
                    len(retainable_removed_numbers),
                )
                clear_queue_shrink_confirmation(self._state)
            else:
                _record_pending_shrink(
                    self._state,
                    prior_count=prior_count,
                    candidate_count=len(queue),
                    missing_numbers=retainable_removed_numbers,
                )
                logger.warning(
                    "[QUEUE_CACHE] suspicious queue shrink retained pending confirmation: "
                    "prior=%d candidate=%d missing=%d confirm_at=%.3f",
                    prior_count,
                    len(queue),
                    len(retainable_removed_numbers),
                    self._state.queue_pending_shrink_confirm_at,
                )
                self._state.cached_scope_issues = _merge_issue_lists(prior_scope, scope)
                self._state.cached_queue_issues = _merge_issue_lists(prior_queue, queue)
                self.prune_refresh_timestamps()
                return self._state.cached_queue_issues
        else:
            if queue_shrink_confirmation_pending(self._state):
                logger.info("[QUEUE_CACHE] clearing unconfirmed queue shrink; refresh recovered")
            clear_queue_shrink_confirmation(self._state)

        if prior_count > 0 and not queue:
            rejected = len(issues) - len(queue)
            active_count = len(self._state.active_sessions)
            history_count = len(self._state.session_history)
            logger.warning(
                "[QUEUE_CACHE] replace_from_refresh dropping in-memory queue from %d to 0 "
                "(fetched=%d, rejected_by_eligibility=%d, active_sessions=%d, session_history=%d); "
                "downstream save_snapshot will wipe persisted cache\nstack:\n%s",
                prior_count, len(issues), rejected, active_count, history_count,
                "".join(traceback.format_stack(limit=10)),
            )
        self._state.cached_scope_issues = scope
        self._state.cached_queue_issues = queue
        self.prune_refresh_timestamps()
        return queue

    def _install_issue(self, issue: "Issue") -> QueueMutationOutcome:
        """Upsert a refreshed issue while enforcing queue eligibility policy."""
        was_present = any(cached.number == issue.number for cached in self._state.cached_queue_issues)
        self._state.cached_scope_issues = [
            cached for cached in self._state.cached_scope_issues if cached.number != issue.number
        ]
        self._state.cached_queue_issues = [
            cached for cached in self._state.cached_queue_issues if cached.number != issue.number
        ]
        if _matches_scope(self._config, issue):
            self._state.cached_scope_issues.append(issue)
        status = self.evaluate_issue(issue)
        if status == QueueMutationStatus.ACCEPTED:
            self._state.cached_queue_issues.append(issue)
            return QueueMutationOutcome(status=status, in_queue=True, updated=was_present)
        return QueueMutationOutcome(status=status, in_queue=False, updated=False)

    def reflect_label_write(self, issue_number: int, label: str, *, present: bool) -> bool:
        """Carry a label write the engine just made onto the cached copies (#8113).

        Without this the cache holds the labels of the last GitHub refresh, so
        every reader of it - the triage agenda, the board snapshot, the
        planners - lags the engine's own writes: a block removed this tick
        still earned a triage grant and had its removal re-planned every tick,
        and a block put on this tick was missing from the agenda of a review
        launched after it (#8094).

        Only issues already cached are touched; a number the cache does not
        hold (a pull request, an issue outside scope) waits for a refresh to
        decide whether it belongs, and a fetch in flight carries the write
        into whatever it commits. The write is recorded and applied under the
        ledger, so no refresh or upsert commits between the two. The cached copy
        is replaced in its slot rather than the list rebound, so a reader
        already holding the list sees it. If the write takes the issue out of
        scope (a configured exclude label), it leaves the cache by the same rule
        a refresh applies.

        Returns whether a cached copy changed.
        """
        with self._ledger.serialised():
            self._ledger.record(LabelWrite(issue_number=issue_number, label=label, present=present))
            changed = False
            in_scope = True
            for cached in (self._state.cached_scope_issues, self._state.cached_queue_issues):
                for index, issue in enumerate(cached):
                    if issue.number != issue_number:
                        continue
                    updated = _with_label(issue, label, present=present)
                    if updated is None:
                        continue
                    cached[index] = updated
                    changed = True
                    in_scope = in_scope and _matches_scope(self._config, updated)
            if not in_scope:
                self._state.cached_scope_issues = [
                    issue for issue in self._state.cached_scope_issues if issue.number != issue_number
                ]
                self._state.cached_queue_issues = [
                    issue for issue in self._state.cached_queue_issues if issue.number != issue_number
                ]
                self.prune_refresh_timestamps()
            return changed

    def remove_issue(self, issue_number: int) -> None:
        """Remove issue from cached queue and refresh metadata."""
        with self._ledger.serialised():
            self._state.cached_scope_issues = [
                issue for issue in self._state.cached_scope_issues if issue.number != issue_number
            ]
            self._state.cached_queue_issues = [
                issue for issue in self._state.cached_queue_issues if issue.number != issue_number
            ]
            clear_issue_refresh(self._state, issue_number)
            self.prune_refresh_timestamps()

    def remove_issue_and_save(self, issue_number: int) -> None:
        """Remove an issue and persist the resulting warm-restart snapshot."""
        self.remove_issue(issue_number)
        self.save_snapshot()

    def evaluate_issue(self, issue: "Issue") -> QueueMutationStatus:
        """Evaluate whether issue can be in queue cache."""
        if not _matches_scope(self._config, issue):
            return QueueMutationStatus.REJECTED_OUT_OF_SCOPE

        excluded_numbers = set(issues_held_by_session_history(self._state.session_history))
        excluded_numbers.update(session.issue.number for session in self._state.active_sessions)
        if issue.number in excluded_numbers:
            return QueueMutationStatus.REJECTED_EXCLUDED

        if self._config.filtering.issue and issue.number != self._config.filtering.issue:
            return QueueMutationStatus.REJECTED_OUT_OF_SCOPE

        return QueueMutationStatus.ACCEPTED

    def prune_refresh_timestamps(self) -> None:
        """Prune refresh timestamp map to currently tracked issue IDs."""
        if (
            not self._state.issue_refresh_timestamps
            and not self._state.issue_last_refreshed_at
            and not self._state.awaiting_merge_drift_scan_timestamps
        ):
            return
        with self._ledger.serialised():
            keep_numbers = {issue.number for issue in self._state.cached_scope_issues}
            keep_numbers.update(issue.number for issue in self._state.cached_queue_issues)
            keep_numbers.update(session.issue.number for session in self._state.active_sessions)
            keep_numbers.update(entry.issue_number for entry in self._state.session_history)
            keep_numbers.update(self._visible_issue_numbers())
            self._state.issue_refresh_timestamps = {
                issue_number: refreshed_at
                for issue_number, refreshed_at in self._state.issue_refresh_timestamps.items()
                if issue_number in keep_numbers
            }
            self._state.issue_last_refreshed_at = {
                issue_number: refreshed_at
                for issue_number, refreshed_at in self._state.issue_last_refreshed_at.items()
                if issue_number in keep_numbers
            }
            self._state.awaiting_merge_drift_scan_timestamps = {
                issue_number: scanned_at
                for issue_number, scanned_at in self._state.awaiting_merge_drift_scan_timestamps.items()
                if issue_number in keep_numbers
            }

    def _visible_issue_numbers(self) -> set[int]:
        """Return issues that the UI is actively displaying and should keep fresh."""
        if self._state.ui_visible_updated_at <= 0:
            return set()
        if (time.time() - self._state.ui_visible_updated_at) > _UI_VISIBILITY_STALENESS_SECONDS:
            return set()
        return set(self._state.ui_visible_issue_numbers)

    def save_snapshot(self) -> None:
        """Persist the current in-scope queue snapshot for warm restarts.

        The durable snapshot covers in-scope issues and the delta watermark;
        runtime scheduling priority remains in memory.
        """
        if self._store is None:
            raise RuntimeError("QueueCacheStore is required to persist queue cache snapshot")
        self._store.save_snapshot(
            self._state.cached_scope_issues,
            self._state.queue_delta_watermark,
            repo=self._config.repo or "",  # cache/display value, not work identity; identity uses require_repo (#7255)
        )


class QueueCacheFetch:
    """A GitHub read on its way into the cache, opened before the read (#8113).

    Required to commit a refresh or an upsert. The commit first re-applies
    every label write the engine made since the fetch opened, so a read that
    raced a write commits the write too, never the labels it replaced. Close
    the fetch once its read is committed or abandoned (use it as a context
    manager); until then the ledger keeps every later write for it.
    """

    def __init__(self, ledger: "IssueCacheLedger") -> None:
        self._ledger = ledger
        self._opened = ledger.open_fetch()
        self._open = True

    def __enter__(self) -> "QueueCacheFetch":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the writes kept for this fetch. Closing twice is a no-op."""
        if self._open:
            self._open = False
            self._ledger.close_fetch(self._opened)

    def opened_on(self, ledger: "IssueCacheLedger") -> "IssueFetchEpoch":
        """Where this fetch opened in ``ledger``; it must still be open, and be that ledger's."""
        if ledger is not self._ledger:
            raise ValueError("a fetch commits only to the state whose cache it was opened on")
        if not self._open:
            raise RuntimeError(
                "a closed fetch cannot commit: the label writes since it opened are no longer kept"
            )
        return self._opened


class QueueCacheLabelWrites:
    """The issue cache's subscription to the engine's label writes (#8113).

    The repository adapter reports every label add and remove it completes
    (:class:`~..ports.label_write_observer.LabelWriteObserver`), from
    whichever thread wrote it: the tick, a background review exchange, an
    operator command. Each report goes through the same ledger as every
    refresh and upsert of the state, so it is ordered with them and carried
    into any fetch that was in flight when it landed.
    """

    def __init__(self, config: "Config", state: "OrchestratorState") -> None:
        self._cache = QueueCache(config, state)

    def label_written(self, issue_number: int, label: str, *, present: bool) -> None:
        changed = self._cache.reflect_label_write(issue_number, label, present=present)
        if changed:
            logger.debug(
                "[QUEUE_CACHE] #%d cached labels follow the engine's write: %s %s",
                issue_number,
                "+" if present else "-",
                label,
            )


def record_issue_refreshes(
    state: "OrchestratorState",
    refreshed_numbers: set[int],
    refreshed_at: float,
) -> None:
    """Record freshness for tracked issues in both dashboard freshness maps."""
    if not refreshed_numbers:
        return
    for issue_number in refreshed_numbers:
        state.issue_refresh_timestamps[issue_number] = refreshed_at
        state.issue_last_refreshed_at[issue_number] = refreshed_at


def clear_issue_refresh(state: "OrchestratorState", issue_number: int) -> None:
    """Clear freshness metadata for an issue from both dashboard freshness maps."""
    state.issue_refresh_timestamps.pop(issue_number, None)
    state.issue_last_refreshed_at.pop(issue_number, None)
    state.awaiting_merge_drift_scan_timestamps.pop(issue_number, None)


def queue_shrink_confirmation_pending(state: "OrchestratorState") -> bool:
    """Whether a large queue shrink is waiting for a confirming refresh."""
    return bool(state.queue_pending_shrink_missing_issue_numbers)


def queue_shrink_confirmation_due(state: "OrchestratorState", now: float) -> bool:
    """Whether the pending queue shrink should force a confirmation scan."""
    return (
        queue_shrink_confirmation_pending(state)
        and state.queue_pending_shrink_confirm_at > 0
        and now >= state.queue_pending_shrink_confirm_at
    )


def clear_queue_shrink_confirmation(state: "OrchestratorState") -> None:
    """Clear any pending large-shrink confirmation state."""
    state.queue_pending_shrink_missing_issue_numbers = []
    state.queue_pending_shrink_confirm_at = 0.0
    state.queue_pending_shrink_prior_count = 0
    state.queue_pending_shrink_candidate_count = 0


def _matches_scope(config: "Config", issue: "Issue") -> bool:
    """Apply label/milestone/exclude-label scope checks for an issue."""
    return issue_scope_skip_detail(config, issue) is None


def _is_suspicious_shrink(prior_count: int, removed_count: int) -> bool:
    if prior_count <= 0 or removed_count < _SUSPICIOUS_SHRINK_MIN_REMOVALS:
        return False
    return (removed_count / prior_count) >= _SUSPICIOUS_SHRINK_MIN_RATIO


def _retainable_removed_numbers(
    cache: QueueCache,
    prior_queue: list["Issue"],
    queue: list["Issue"],
) -> set[int]:
    candidate_numbers = {issue.number for issue in queue}
    return {
        issue.number
        for issue in prior_queue
        if issue.number not in candidate_numbers
        and cache.evaluate_issue(issue) == QueueMutationStatus.ACCEPTED
    }


def _pending_shrink_confirmed(
    state: "OrchestratorState",
    missing_numbers: set[int],
) -> bool:
    """Return true only when every pending missing issue is still missing."""
    pending = set(state.queue_pending_shrink_missing_issue_numbers)
    return bool(pending) and pending.issubset(missing_numbers)


def _record_pending_shrink(
    state: "OrchestratorState",
    *,
    prior_count: int,
    candidate_count: int,
    missing_numbers: set[int],
) -> None:
    existing_confirm_at = state.queue_pending_shrink_confirm_at
    state.queue_pending_shrink_missing_issue_numbers = sorted(missing_numbers)
    if existing_confirm_at > 0:
        state.queue_pending_shrink_confirm_at = existing_confirm_at
    else:
        state.queue_pending_shrink_confirm_at = (
            time.time() + QUEUE_SHRINK_CONFIRM_DELAY_SECONDS
        )
    state.queue_pending_shrink_prior_count = prior_count
    state.queue_pending_shrink_candidate_count = candidate_count


def _with_label(issue: "Issue", label: str, *, present: bool) -> "Issue | None":
    """``issue`` with ``label`` standing or not, or None when it already is.

    Case-insensitive, as GitHub folds label names.
    """
    folded = label.casefold()
    kept = [name for name in issue.labels if name.casefold() != folded]
    standing = len(kept) < len(issue.labels)
    if standing == present:
        return None
    if not is_dataclass(issue) or isinstance(issue, type):
        raise TypeError(f"cached issue #{issue.number} is not a snapshot dataclass: {type(issue).__name__}")
    # Each Issue implementation keeps its own sequence type (tuple or list).
    rebuild = cast("Callable[[list[str]], Sequence[str]]", type(issue.labels))
    return replace(issue, labels=rebuild([*kept, label] if present else kept))


def _merge_issue_lists(
    prior: list["Issue"],
    current: list["Issue"],
) -> list["Issue"]:
    current_numbers = {issue.number for issue in current}
    return [
        *(issue for issue in current),
        *(issue for issue in prior if issue.number not in current_numbers),
    ]
