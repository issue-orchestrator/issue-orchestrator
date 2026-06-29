"""Reconcile the local label_store against GitHub (the source of truth).

The ``label_store`` records the orchestrator-owned labels the orchestrator
believes it has applied to each issue. It is a write-through mirror, so it can
silently diverge from GitHub when a label is changed out-of-band, when a
write-through removal is lost, or after a crash mid-mutation. Left unreconciled,
the mirror accumulates phantom ``publish-failed`` / ``pr-pending`` rows for work
that has actually landed.

This owner brings each stored issue's orchestrator-owned labels back into
agreement with GitHub: stale rows (in the store, absent on GitHub) are pruned
and missing rows (on GitHub, absent from the store) are added. Only
orchestrator-owned labels are touched — human/agent labels are left alone.

To respect GitHub API discipline, live labels are taken from the warm queue
cache when available (zero extra calls) and fetched per-issue only for stored
issues that are out of the cache's scope, bounded by ``fetch_budget``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Sequence

if TYPE_CHECKING:
    from ..ports.label_store import LabelStore
    from ..ports.repository_host import RepositoryHost
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

# Default per-startup ceiling on individual GitHub issue fetches used to
# reconcile stored issues that fall outside the warm queue cache (typically
# closed/out-of-scope issues). Bounded so a large drifted backlog cannot turn
# startup into a long serial scan; the remainder is reconciled on later
# startups and, going forward, sheds itself via the clear-on-merge path.
DEFAULT_LABEL_STORE_RECONCILE_FETCH_BUDGET = 100


@dataclass(frozen=True)
class FreshLabelSnapshot:
    """GitHub label observations verified fresh during the current startup.

    ``LabelStoreReconciler`` treats every entry as authoritative GitHub truth
    and rewrites the mirror to match it, so this type exists to force the caller
    to state freshness explicitly at the boundary. Two origins must never be
    conflated into a raw label map:

    - :meth:`from_github_sync` — labels observed from a *successful*
      GitHub-backed queue sync this run. Safe to trust as source of truth.
    - :meth:`degraded` — the queue label source could not be confirmed fresh
      (e.g. a transient fetch failure left startup on a persisted, possibly
      stale snapshot). Carries no entries, so the reconciler reads each stored
      issue fresh within budget — or leaves the store untouched when that read
      fails — instead of rewriting the mirror from cache that may predate a
      prior run's label mutation.
    """

    labels_by_issue: Mapping[int, Sequence[str]]

    @classmethod
    def from_github_sync(
        cls, labels_by_issue: Mapping[int, Sequence[str]]
    ) -> "FreshLabelSnapshot":
        """Snapshot of labels observed from this run's successful GitHub sync."""
        return cls(labels_by_issue=dict(labels_by_issue))

    @classmethod
    def degraded(cls) -> "FreshLabelSnapshot":
        """No verified-fresh labels — force per-issue reads / leave untouched."""
        return cls(labels_by_issue={})


@dataclass(frozen=True)
class LabelStoreReconciliationResult:
    """Summary of a label_store reconciliation pass."""

    issues_checked: int = 0
    issues_changed: int = 0
    labels_added: int = 0
    labels_removed: int = 0
    issues_fetched: int = 0
    issues_skipped_budget: int = 0


class LabelStoreReconciler:
    """Brings the local label_store back into agreement with GitHub labels."""

    def __init__(
        self,
        label_store: "LabelStore",
        label_manager: "LabelManager",
        repository_host: "RepositoryHost",
        fetch_budget: int = DEFAULT_LABEL_STORE_RECONCILE_FETCH_BUDGET,
    ) -> None:
        self._label_store = label_store
        self._lm = label_manager
        self._repository_host = repository_host
        self._fetch_budget = max(0, fetch_budget)

    def reconcile(
        self,
        fresh_labels: FreshLabelSnapshot,
    ) -> LabelStoreReconciliationResult:
        """Reconcile every stored issue's orchestrator-owned labels with GitHub.

        Args:
            fresh_labels: GitHub labels verified fresh this startup (see
                :class:`FreshLabelSnapshot`). Entries are trusted as the
                zero-cost source of truth; stored issues absent from the
                snapshot are fetched individually, up to ``fetch_budget``. A
                degraded snapshot carries no entries, so every stored issue is
                read fresh (or left untouched when the read fails) rather than
                reconciled against possibly-stale cache.
        """
        stored = self._label_store.load_all()
        cache = {
            number: set(labels)
            for number, labels in fresh_labels.labels_by_issue.items()
        }

        issues_checked = 0
        issues_changed = 0
        labels_added = 0
        labels_removed = 0
        issues_fetched = 0
        issues_skipped_budget = 0

        for issue_number in sorted(stored):
            ours_in_store = {
                label for label in stored[issue_number] if self._lm.is_ours(label)
            }
            if not ours_in_store:
                # Nothing the orchestrator owns is recorded for this issue;
                # there is no mirror to reconcile.
                continue

            github_labels = cache.get(issue_number)
            if github_labels is None:
                if issues_fetched >= self._fetch_budget:
                    issues_skipped_budget += 1
                    continue
                github_labels = self._fetch_github_labels(issue_number)
                issues_fetched += 1
                if github_labels is None:
                    # Transient/unreadable — leave the store untouched rather
                    # than guess. A later pass retries.
                    continue

            issues_checked += 1
            ours_on_github = {
                label for label in github_labels if self._lm.is_ours(label)
            }
            to_remove = ours_in_store - ours_on_github
            to_add = ours_on_github - ours_in_store
            if not to_remove and not to_add:
                continue

            # Replace the issue's rows atomically: keep any non-orchestrator
            # rows untouched and bring the orchestrator-owned rows into exact
            # agreement with GitHub. Using the store's bulk save (not per-label
            # mutation) keeps this a pure mirror operation, distinct from a
            # GitHub label write.
            stored_labels = set(stored[issue_number])
            desired = (stored_labels - ours_in_store) | ours_on_github
            self._label_store.save_labels(issue_number, desired)
            labels_removed += len(to_remove)
            labels_added += len(to_add)
            issues_changed += 1
            logger.info(
                "[label-store-reconcile] issue=%d added=%s removed=%s",
                issue_number,
                sorted(to_add),
                sorted(to_remove),
            )

        if issues_skipped_budget:
            logger.warning(
                "[label-store-reconcile] fetch budget (%d) exhausted; "
                "%d stored issue(s) deferred to a later startup",
                self._fetch_budget,
                issues_skipped_budget,
            )
        logger.info(
            "[label-store-reconcile] checked=%d changed=%d added=%d removed=%d "
            "fetched=%d deferred=%d",
            issues_checked,
            issues_changed,
            labels_added,
            labels_removed,
            issues_fetched,
            issues_skipped_budget,
        )
        return LabelStoreReconciliationResult(
            issues_checked=issues_checked,
            issues_changed=issues_changed,
            labels_added=labels_added,
            labels_removed=labels_removed,
            issues_fetched=issues_fetched,
            issues_skipped_budget=issues_skipped_budget,
        )

    def _fetch_github_labels(self, issue_number: int) -> set[str] | None:
        try:
            issue = self._repository_host.get_issue(issue_number)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[label-store-reconcile] failed to fetch issue #%d: %s",
                issue_number,
                exc,
            )
            return None
        if issue is None:
            return None
        return set(issue.labels)
