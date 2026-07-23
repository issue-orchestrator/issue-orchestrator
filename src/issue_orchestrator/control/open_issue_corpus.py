"""GitHub-to-SQL synchronization for the tech-lead open-issue corpus."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from ..domain.open_issue_corpus import (
    OpenIssueFingerprint,
    build_open_issue_fingerprint,
)
from ..ports import RepositoryHost
from ..ports.issue import Issue
from ..ports.open_issue_corpus_store import OpenIssueCorpusStore
from .proposal_dedup_gate import OpenIssueCorpus

logger = logging.getLogger(__name__)

# A complete corpus is a safety fact, so cold rebuilds fail loudly if a
# repository exceeds this deliberately generous bounded walk. Delta reads use
# the same cap so the adapter never returns a silently partial safety corpus.
_OPEN_ISSUE_FETCH_LIMIT = 10_000


@dataclass(frozen=True)
class OpenIssueCorpusSyncResult:
    """Observable outcome of one cold rebuild or delta refresh."""

    mode: str
    upserted: int
    evicted: int
    watermark: str


class OpenIssueCorpusManager:
    """Owns synchronization and gate projection for the rebuildable corpus."""

    def __init__(
        self,
        repository_host: RepositoryHost | None,
        store: OpenIssueCorpusStore,
        *,
        is_enabled: Callable[[], bool],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository_host = repository_host
        self._store = store
        self._is_enabled = is_enabled
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def sync(self) -> OpenIssueCorpusSyncResult | None:
        """Cold-rebuild once, then apply repo-wide issue deltas on each refresh."""

        if not self._is_enabled():
            return None
        if self._repository_host is None:
            raise RuntimeError("open-issue corpus sync requires a repository host")
        snapshot = self._store.load()
        if snapshot is None:
            return self._rebuild()
        return self._apply_delta(snapshot.watermark)

    def load(self) -> OpenIssueCorpus:
        """Project the last successful SQL generation into gate-ready facts."""

        if not self._is_enabled():
            return OpenIssueCorpus.disabled()
        try:
            snapshot = self._store.load()
        except Exception:
            logger.exception("[tech_lead] Failed to load the open-issue dedup corpus")
            return OpenIssueCorpus.unavailable()
        if snapshot is None:
            return OpenIssueCorpus.unavailable()
        return OpenIssueCorpus.ready(snapshot.issues)

    def _rebuild(self) -> OpenIssueCorpusSyncResult:
        # Capture before the scan.  An issue changed while pagination is in flight
        # then has updated_at > this cursor and is picked up by the next delta.
        watermark = self._clock().isoformat()
        issues = self._repository_host.list_issues(
            state="open",
            limit=_OPEN_ISSUE_FETCH_LIMIT,
            exhaustive=True,
        )
        entries = tuple(_fingerprint(issue) for issue in issues)
        self._store.replace_all(entries, watermark=watermark)
        return OpenIssueCorpusSyncResult(
            mode="rebuild",
            upserted=len(entries),
            evicted=0,
            watermark=watermark,
        )

    def _apply_delta(self, watermark: str) -> OpenIssueCorpusSyncResult:
        issues, next_watermark = self._repository_host.list_issues_delta(
            since=watermark,
            limit=_OPEN_ISSUE_FETCH_LIMIT,
        )
        if len(issues) >= _OPEN_ISSUE_FETCH_LIMIT:
            # The delta port is bounded. Rebuild rather than advance past a
            # possibly truncated update window and silently strand older changes.
            return self._rebuild()
        upserts: list[OpenIssueFingerprint] = []
        evictions: list[int] = []
        for issue in issues:
            state = issue.state.lower()
            if state == "open":
                upserts.append(_fingerprint(issue))
            elif state == "closed":
                evictions.append(issue.number)
            else:
                raise ValueError(
                    f"issue #{issue.number} has unsupported state {issue.state!r}"
                )
        cursor = next_watermark or watermark
        self._store.apply_delta(
            upserts,
            evict_issue_numbers=evictions,
            watermark=cursor,
        )
        return OpenIssueCorpusSyncResult(
            mode="delta",
            upserted=len(upserts),
            evicted=len(evictions),
            watermark=cursor,
        )


def _fingerprint(issue: Issue) -> OpenIssueFingerprint:
    """Translate the narrow Issue protocol surface into a normalized cache row."""

    number = issue.number
    title = issue.title
    body = issue.body
    return build_open_issue_fingerprint(number, title, body)
