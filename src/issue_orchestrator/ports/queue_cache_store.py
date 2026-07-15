"""Port protocol for queue cache persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Sequence

if TYPE_CHECKING:
    from .issue import Issue


class QueueCacheStore(Protocol):
    """Persists the in-scope issue snapshot across restarts."""

    def load_issues(self, repo: str) -> Sequence["Issue"]: ...
    def load_watermark(self) -> str | None: ...
    def load_last_health_review_at(self) -> float: ...
    def save_last_health_review_at(self, value: float) -> None: ...
    def save_snapshot(
        self,
        issues: Sequence["Issue"],
        watermark: str | None,
        repo: str = "",
    ) -> None: ...
    def clear(self) -> None: ...
