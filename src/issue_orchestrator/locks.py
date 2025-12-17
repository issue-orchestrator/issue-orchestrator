"""Simple file-based locking using atomic mkdir for multi-instance coordination."""

import os
import time
from pathlib import Path
from typing import Callable, Optional

LOCK_DIR = Path("/tmp/issue-orchestrator/locks")
TIMESTAMP_FILE = "claimed_at"


def try_claim(issue_number: int, prefix: str = "issue") -> bool:
    """Try to claim an issue. Returns True if claimed, False if already claimed.

    Args:
        issue_number: The issue/PR number to claim
        prefix: Lock prefix (default "issue", use "review" for code reviews)
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f"{prefix}-{issue_number}"

    try:
        lock_path.mkdir()
        # Write timestamp to track when claim was made
        timestamp_path = lock_path / TIMESTAMP_FILE
        timestamp_path.write_text(str(time.time()))
        return True
    except FileExistsError:
        return False


def release_claim(issue_number: int, prefix: str = "issue") -> None:
    """Release a claim on an issue.

    Args:
        issue_number: The issue/PR number to release
        prefix: Lock prefix (default "issue", use "review" for code reviews)
    """
    lock_path = LOCK_DIR / f"{prefix}-{issue_number}"
    if lock_path.exists():
        # Remove timestamp file first if it exists
        timestamp_path = lock_path / TIMESTAMP_FILE
        if timestamp_path.exists():
            timestamp_path.unlink()
        lock_path.rmdir()


def is_claimed(issue_number: int) -> bool:
    """Check if an issue is claimed."""
    return (LOCK_DIR / f"issue-{issue_number}").exists()


def list_claimed(prefix: str = "issue") -> list[int]:
    """List all claimed issue/PR numbers for the given prefix."""
    if not LOCK_DIR.exists():
        return []
    claimed = []
    for item in LOCK_DIR.iterdir():
        if item.is_dir() and item.name.startswith(f"{prefix}-"):
            try:
                claimed.append(int(item.name.replace(f"{prefix}-", "")))
            except ValueError:
                pass
    return claimed


def get_claim_age(issue_number: int, prefix: str = "issue") -> Optional[float]:
    """Get the age of a claim in seconds. Returns None if not claimed or no timestamp."""
    lock_path = LOCK_DIR / f"{prefix}-{issue_number}"
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


def is_claim_stale(issue_number: int, max_age_minutes: int = 60, prefix: str = "issue") -> bool:
    """Check if a claim is stale (older than max_age_minutes)."""
    age_seconds = get_claim_age(issue_number, prefix)
    if age_seconds is None:
        return False

    max_age_seconds = max_age_minutes * 60
    return age_seconds > max_age_seconds


def cleanup_stale_claims(max_age_minutes: int = 60, prefix: str = "issue") -> list[int]:
    """
    Clean up stale claims (older than max_age_minutes).
    Returns list of issue/PR numbers that were cleaned up.
    """
    cleaned = []
    claimed_issues = list_claimed(prefix)

    for issue_number in claimed_issues:
        if is_claim_stale(issue_number, max_age_minutes, prefix):
            release_claim(issue_number, prefix)
            cleaned.append(issue_number)

    return cleaned


def cleanup_orphaned_claims(
    session_exists_fn: Callable[[str], bool],
    prefix: str = "issue"
) -> list[int]:
    """
    Clean up claims that don't have active sessions (orphaned locks).

    This handles cases where a session crashed immediately (e.g., command not found)
    before the claim could be released, leaving "fresh" locks that aren't stale by age.

    Args:
        session_exists_fn: Callback that takes session_name (e.g. "issue-123")
                          and returns True if session is active
        prefix: Lock prefix to check (default "issue")

    Returns:
        List of issue numbers that were cleaned up.
    """
    cleaned = []

    if not LOCK_DIR.exists():
        return cleaned

    for item in LOCK_DIR.iterdir():
        if item.is_dir() and item.name.startswith(f"{prefix}-"):
            try:
                issue_number = int(item.name.replace(f"{prefix}-", ""))
            except ValueError:
                continue

            session_name = f"{prefix}-{issue_number}"
            if not session_exists_fn(session_name):
                release_claim(issue_number, prefix)
                cleaned.append(issue_number)

    return cleaned
