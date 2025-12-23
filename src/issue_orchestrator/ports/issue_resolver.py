"""Issue resolver port - translates IssueKeys to backing-store handles.

Resolution is contextual and belongs to the adapter layer, not to IssueKey:
- GitHub needs: repo + GH issue number
- DB needs: primary key
- File-based backlog needs: path lookup

The resolver maintains a mapping from stable identity (IssueKey) to
the current backing-store locator (handle).
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..domain.issue_key import IssueKey, IssueHandle


class IssueResolver(Protocol):
    """Protocol for resolving IssueKeys to backing-store handles.

    Implementations cache the external_id -> handle mapping and can
    rebuild it by scanning issues (e.g., from a milestone).

    The resolver does NOT own or modify issues - it only translates
    identities to locators.

    Store-specific implementations:
    - GitHubIssueResolver: returns int (issue number)
    - DBIssueResolver: returns int/UUID (row ID)
    - FileIssueResolver: returns str (path)
    """

    def resolve(self, key: "IssueKey") -> "IssueHandle":
        """Resolve an IssueKey to its backing-store handle.

        The return type depends on the backing store:
        - GitHub: int (issue number)
        - DB: int or str (row ID)
        - File: str (path)
        - None if the key cannot be resolved

        Args:
            key: The IssueKey to resolve

        Returns:
            The backing-store handle, or None if not found
        """
        ...

    def build_index(self) -> None:
        """Rebuild the resolution cache.

        Scans relevant issues and indexes external_id -> handle mapping.
        Call this at startup or when issues may have changed.
        """
        ...

    def invalidate(self, key: "IssueKey") -> None:
        """Invalidate a cached resolution.

        Call when you know an issue has been modified and the cache
        may be stale.

        Args:
            key: The IssueKey to invalidate
        """
        ...
