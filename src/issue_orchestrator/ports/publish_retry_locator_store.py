"""Port for durable per-issue publish-retry locators.

The store keeps the small set of pointers a publish retry needs (worktree,
branch, run assets, completion-record path, ...) alive across orchestrator
restarts. It replaces the SQLite publish-job store as the durable source of
truth for "how do I re-run publish for this failed issue".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..domain.publish_retry import PublishRetryLocators


class PublishRetryLocatorStore(Protocol):
    """Durable per-issue storage for publish-retry locators."""

    def save(self, locators: "PublishRetryLocators") -> None:
        """Persist (or overwrite) the retry locators for one issue."""
        ...

    def get(self, issue_number: int) -> "PublishRetryLocators | None":
        """Return the stored locators for an issue, or ``None`` if absent."""
        ...

    def clear(self, issue_number: int) -> None:
        """Remove any stored locators for an issue. No-op if absent."""
        ...
