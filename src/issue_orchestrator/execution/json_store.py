"""JSON file-based session store implementation."""

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import asdict

from ..ports.session_store import SessionStore

logger = logging.getLogger(__name__)


class JsonSessionStore:
    """Persists session state to a JSON file.

    This allows the orchestrator to recover state after restart.
    The store is designed to be simple and human-readable.
    """

    def __init__(self, store_path: Path):
        """Initialize the store.

        Args:
            store_path: Path to the JSON file for persistence
        """
        self.store_path = store_path
        self._cache: dict = {}
        self._load()

    def _load(self) -> None:
        """Load state from disk."""
        if self.store_path.exists():
            try:
                with open(self.store_path) as f:
                    self._cache = json.load(f)
                logger.info(f"Loaded session store from {self.store_path}")
            except Exception as e:
                logger.warning(f"Failed to load session store: {e}")
                self._cache = {}
        else:
            self._cache = {"sessions": {}, "state_machines": {}}

    def _save(self) -> None:
        """Persist state to disk."""
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.store_path, 'w') as f:
                json.dump(self._cache, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to save session store: {e}")

    def save_session_state(
        self,
        session_id: str,
        issue_number: int,
        state: str,
        started_at: Optional[datetime] = None,
        metadata: Optional[dict] = None
    ) -> None:
        """Save session state machine state."""
        self._cache.setdefault("state_machines", {})[session_id] = {
            "issue_number": issue_number,
            "state": state,
            "started_at": started_at.isoformat() if started_at else None,
            "metadata": metadata or {},
            "updated_at": datetime.now().isoformat()
        }
        self._save()
        logger.debug(f"Saved state for session {session_id}: {state}")

    def get_session_state(self, session_id: str) -> Optional[dict]:
        """Get saved state for a session."""
        return self._cache.get("state_machines", {}).get(session_id)

    def get_all_sessions(self) -> dict[str, dict]:
        """Get all saved session states."""
        return self._cache.get("state_machines", {})

    def delete_session_state(self, session_id: str) -> None:
        """Delete saved state for a session."""
        if session_id in self._cache.get("state_machines", {}):
            del self._cache["state_machines"][session_id]
            self._save()
            logger.debug(f"Deleted state for session {session_id}")

    def save_issue_state(
        self,
        issue_number: int,
        state: str,
        pr_number: Optional[int] = None,
        metadata: Optional[dict] = None
    ) -> None:
        """Save issue state machine state."""
        self._cache.setdefault("issue_machines", {})[str(issue_number)] = {
            "state": state,
            "pr_number": pr_number,
            "metadata": metadata or {},
            "updated_at": datetime.now().isoformat()
        }
        self._save()

    def get_issue_state(self, issue_number: int) -> Optional[dict]:
        """Get saved state for an issue."""
        return self._cache.get("issue_machines", {}).get(str(issue_number))

    def save_review_state(
        self,
        pr_number: int,
        state: str,
        rework_count: int = 0,
        metadata: Optional[dict] = None
    ) -> None:
        """Save review state machine state."""
        self._cache.setdefault("review_machines", {})[str(pr_number)] = {
            "state": state,
            "rework_count": rework_count,
            "metadata": metadata or {},
            "updated_at": datetime.now().isoformat()
        }
        self._save()

    def get_review_state(self, pr_number: int) -> Optional[dict]:
        """Get saved state for a review."""
        return self._cache.get("review_machines", {}).get(str(pr_number))

    def clear_completed(self) -> int:
        """Remove all completed/terminal state entries. Returns count removed."""
        terminal_states = {"completed", "failed", "timed_out", "merged", "closed"}
        removed = 0

        for key in ["state_machines", "issue_machines", "review_machines"]:
            machines = self._cache.get(key, {})
            to_remove = [
                k for k, v in machines.items()
                if v.get("state") in terminal_states
            ]
            for k in to_remove:
                del machines[k]
                removed += 1

        if removed:
            self._save()
            logger.info(f"Cleared {removed} completed state machine entries")

        return removed
