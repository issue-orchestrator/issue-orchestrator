"""Safety state persistence for hook verification.

Tracks when safety checks were last run and their results to avoid
running expensive live verification on every startup.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Location within the orchestrator state directory
SAFETY_STATE_FILE = ".issue-orchestrator/safety-state.json"


@dataclass
class SafetyCheckResult:
    """Result of a single agent's safety check."""
    success: bool
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SafetyState:
    """Persisted state for safety checks.

    Tracks when the last safety check was performed and results
    for each agent type that was tested.
    """
    last_check: datetime | None = None
    last_results: dict[str, SafetyCheckResult] = field(default_factory=dict)

    def is_stale(self, interval_days: int) -> bool:
        """Check if safety check needs to be run.

        Args:
            interval_days: Maximum days between checks. 0 means disabled.

        Returns:
            True if check should be run (first run or interval exceeded).
        """
        if interval_days <= 0:
            return False  # Disabled
        if self.last_check is None:
            return True  # First run
        elapsed = datetime.now(timezone.utc) - self.last_check
        return elapsed.days >= interval_days

    def mark_checked(
        self,
        results: dict[str, tuple[bool, str]],
    ) -> None:
        """Update state after running safety checks.

        Args:
            results: Dict mapping agent_type to (success, message) tuple.
        """
        now = datetime.now(timezone.utc)
        self.last_check = now
        self.last_results = {
            agent_type: SafetyCheckResult(
                success=success,
                message=message,
                timestamp=now,
            )
            for agent_type, (success, message) in results.items()
        }

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "last_results": {
                agent_type: {
                    "success": result.success,
                    "message": result.message,
                    "timestamp": result.timestamp.isoformat(),
                }
                for agent_type, result in self.last_results.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SafetyState":
        """Create from dict loaded from JSON.

        Raises:
            KeyError, TypeError, ValueError: If data structure is invalid.
        """
        last_check = None
        if data.get("last_check"):
            last_check = datetime.fromisoformat(data["last_check"])

        last_results = {}
        for agent_type, result_data in data.get("last_results", {}).items():
            # Validate result_data is a dict with required keys
            if not isinstance(result_data, dict):
                raise TypeError(f"Expected dict for result_data, got {type(result_data)}")
            timestamp = datetime.fromisoformat(result_data["timestamp"])
            last_results[agent_type] = SafetyCheckResult(
                success=result_data["success"],
                message=result_data["message"],
                timestamp=timestamp,
            )

        return cls(last_check=last_check, last_results=last_results)


def get_safety_state_path(repo_root: Path) -> Path:
    """Get the path to the safety state file."""
    return repo_root / SAFETY_STATE_FILE


def load_safety_state(repo_root: Path) -> SafetyState:
    """Load safety state from disk.

    Args:
        repo_root: Root of the repository.

    Returns:
        SafetyState instance (empty defaults if file doesn't exist).
    """
    state_path = get_safety_state_path(repo_root)
    if not state_path.exists():
        return SafetyState()

    try:
        with open(state_path) as f:
            data = json.load(f)
        return SafetyState.from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        logger.warning("Failed to load safety state from %s: %s", state_path, e)
        return SafetyState()


def save_safety_state(repo_root: Path, state: SafetyState) -> None:
    """Save safety state to disk.

    Args:
        repo_root: Root of the repository.
        state: SafetyState to persist.
    """
    state_path = get_safety_state_path(repo_root)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    with open(state_path, "w") as f:
        json.dump(state.to_dict(), f, indent=2)
