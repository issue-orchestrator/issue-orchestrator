"""Session store port for session persistence.

This module defines the protocol (interface) for persisting and retrieving
session data. Implementations can use different storage backends (filesystem,
database, Redis, etc.) while maintaining the same interface.
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from issue_orchestrator.domain.models import Session


class SessionStore(Protocol):
    """Protocol for session persistence operations.

    This protocol defines the interface for storing, retrieving, and managing
    session data. Sessions track the state of work being done on issues,
    including worktree paths, agent processes, and progress information.
    """

    def save_session(self, session: "Session") -> None:
        """Save or update a session.

        If a session with the same ID already exists, it will be updated.
        Otherwise, a new session will be created.

        Args:
            session: The Session object to save.

        Raises:
            StorageError: If there's an error saving the session.
            ValidationError: If the session data is invalid.
        """
        ...

    def get_session(self, session_id: str) -> "Session | None":
        """Retrieve a session by ID.

        Args:
            session_id: The unique identifier for the session.

        Returns:
            The Session object if found, None otherwise.

        Raises:
            StorageError: If there's an error retrieving the session.
        """
        ...

    def get_active_sessions(self) -> list["Session"]:
        """Get all currently active sessions.

        Active sessions are those where work is in progress or waiting to be
        completed. This typically excludes completed or failed sessions.

        Returns:
            A list of active Session objects. Returns empty list if no
            active sessions exist.

        Raises:
            StorageError: If there's an error retrieving sessions.
        """
        ...

    def delete_session(self, session_id: str) -> None:
        """Delete a session by ID.

        This permanently removes the session from storage. Use with caution.
        If the session doesn't exist, this is a no-op.

        Args:
            session_id: The unique identifier for the session to delete.

        Raises:
            StorageError: If there's an error deleting the session.
        """
        ...
