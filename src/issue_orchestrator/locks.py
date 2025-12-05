"""Simple file-based locking using atomic mkdir for multi-instance coordination."""

import os
import time
from pathlib import Path
from typing import Optional

LOCK_DIR = Path("/tmp/issue-orchestrator/locks")
TIMESTAMP_FILE = "claimed_at"
PAUSE_LOCK = LOCK_DIR / "paused"


def try_claim(issue_number: int) -> bool:
    """Try to claim an issue. Returns True if claimed, False if already claimed."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f"issue-{issue_number}"

    try:
        lock_path.mkdir()
        # Write timestamp to track when claim was made
        timestamp_path = lock_path / TIMESTAMP_FILE
        timestamp_path.write_text(str(time.time()))
        return True
    except FileExistsError:
        return False


def release_claim(issue_number: int) -> None:
    """Release a claim on an issue."""
    lock_path = LOCK_DIR / f"issue-{issue_number}"
    if lock_path.exists():
        # Remove timestamp file first if it exists
        timestamp_path = lock_path / TIMESTAMP_FILE
        if timestamp_path.exists():
            timestamp_path.unlink()
        lock_path.rmdir()


def is_claimed(issue_number: int) -> bool:
    """Check if an issue is claimed."""
    return (LOCK_DIR / f"issue-{issue_number}").exists()


def list_claimed() -> list[int]:
    """List all claimed issue numbers."""
    if not LOCK_DIR.exists():
        return []
    claimed = []
    for item in LOCK_DIR.iterdir():
        if item.is_dir() and item.name.startswith("issue-"):
            try:
                claimed.append(int(item.name.replace("issue-", "")))
            except ValueError:
                pass
    return claimed


def get_claim_age(issue_number: int) -> Optional[float]:
    """Get the age of a claim in seconds. Returns None if not claimed or no timestamp."""
    lock_path = LOCK_DIR / f"issue-{issue_number}"
    if not lock_path.exists():
        return None

    timestamp_path = lock_path / TIMESTAMP_FILE
    if not timestamp_path.exists():
        return None

    try:
        claimed_at = float(timestamp_path.read_text().strip())
        return time.time() - claimed_at
    except (ValueError, OSError):
        return None


def is_claim_stale(issue_number: int, max_age_minutes: int = 60) -> bool:
    """Check if a claim is stale (older than max_age_minutes)."""
    age_seconds = get_claim_age(issue_number)
    if age_seconds is None:
        return False

    max_age_seconds = max_age_minutes * 60
    return age_seconds > max_age_seconds


def cleanup_stale_claims(max_age_minutes: int = 60) -> list[int]:
    """
    Clean up stale claims (older than max_age_minutes).
    Returns list of issue numbers that were cleaned up.
    """
    cleaned = []
    claimed_issues = list_claimed()

    for issue_number in claimed_issues:
        if is_claim_stale(issue_number, max_age_minutes):
            release_claim(issue_number)
            cleaned.append(issue_number)

    return cleaned


def set_paused() -> None:
    """Set the orchestrator to paused state."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    PAUSE_LOCK.write_text(str(time.time()))


def set_resumed() -> None:
    """Resume the orchestrator from paused state."""
    if PAUSE_LOCK.exists():
        PAUSE_LOCK.unlink()


def is_paused() -> bool:
    """Check if the orchestrator is paused."""
    return PAUSE_LOCK.exists()
