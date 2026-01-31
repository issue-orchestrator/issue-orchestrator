"""State persistence for review exchange probes."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILE = ".issue-orchestrator/review-exchange-state.json"


@dataclass
class ReviewExchangeProbeResult:
    success: bool
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ReviewExchangeState:
    last_check: datetime | None = None
    last_results: dict[str, ReviewExchangeProbeResult] = field(default_factory=dict)

    def is_stale(self, interval: timedelta) -> bool:
        if interval.total_seconds() <= 0:
            return False
        if self.last_check is None:
            return True
        elapsed = datetime.now(timezone.utc) - self.last_check
        return elapsed >= interval

    def mark_checked(self, results: dict[str, tuple[bool, str]]) -> None:
        now = datetime.now(timezone.utc)
        self.last_check = now
        self.last_results = {
            key: ReviewExchangeProbeResult(success=success, message=message, timestamp=now)
            for key, (success, message) in results.items()
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "last_results": {
                key: {
                    "success": result.success,
                    "message": result.message,
                    "timestamp": result.timestamp.isoformat(),
                }
                for key, result in self.last_results.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewExchangeState":
        last_check = None
        if data.get("last_check"):
            last_check = datetime.fromisoformat(data["last_check"])

        last_results: dict[str, ReviewExchangeProbeResult] = {}
        for key, result_data in data.get("last_results", {}).items():
            if not isinstance(result_data, dict):
                raise TypeError(f"Expected dict for result_data, got {type(result_data)}")
            timestamp = datetime.fromisoformat(result_data["timestamp"])
            last_results[key] = ReviewExchangeProbeResult(
                success=result_data["success"],
                message=result_data["message"],
                timestamp=timestamp,
            )

        return cls(last_check=last_check, last_results=last_results)


def get_state_path(repo_root: Path) -> Path:
    return repo_root / STATE_FILE


def load_state(repo_root: Path) -> ReviewExchangeState:
    path = get_state_path(repo_root)
    if not path.exists():
        return ReviewExchangeState()

    try:
        with open(path) as f:
            data = json.load(f)
        return ReviewExchangeState.from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.warning("Failed to load review exchange state from %s: %s", path, exc)
        return ReviewExchangeState()


def save_state(repo_root: Path, state: ReviewExchangeState) -> None:
    path = get_state_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state.to_dict(), f, indent=2)
