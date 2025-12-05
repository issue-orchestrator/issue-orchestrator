"""Unit tests for the locks module."""

import pytest
import time
from pathlib import Path
from issue_orchestrator.locks import (
    try_claim, release_claim, is_claimed, list_claimed, LOCK_DIR,
    get_claim_age, is_claim_stale, cleanup_stale_claims, TIMESTAMP_FILE,
    set_paused, set_resumed, is_paused, PAUSE_LOCK
)


def _cleanup_lock_dir():
    """Helper to clean up lock directory including timestamp files and pause lock."""
    if LOCK_DIR.exists():
        for item in LOCK_DIR.iterdir():
            if item.is_dir():
                # Remove any files inside (e.g., timestamp file)
                for subitem in item.iterdir():
                    subitem.unlink()
                item.rmdir()
            elif item.is_file():
                # Remove files like PAUSE_LOCK
                item.unlink()
        LOCK_DIR.rmdir()


@pytest.fixture(autouse=True)
def cleanup_locks():
    """Clean up lock directory before and after each test."""
    # Clean up before test
    _cleanup_lock_dir()

    yield

    # Clean up after test
    _cleanup_lock_dir()


class TestTryClaim:
    """Test the try_claim function."""

    def test_try_claim_success(self):
        """Test successfully claiming an unclaimed issue."""
        result = try_claim(1)
        assert result is True
        assert is_claimed(1)

    def test_try_claim_already_claimed(self):
        """Test claiming an already claimed issue."""
        # First claim should succeed
        assert try_claim(1) is True

        # Second claim should fail
        assert try_claim(1) is False

    def test_try_claim_creates_lock_dir(self):
        """Test that try_claim creates the lock directory if it doesn't exist."""
        # Ensure lock dir doesn't exist
        if LOCK_DIR.exists():
            LOCK_DIR.rmdir()

        assert not LOCK_DIR.exists()

        try_claim(1)

        assert LOCK_DIR.exists()
        assert LOCK_DIR.is_dir()

    def test_try_claim_multiple_issues(self):
        """Test claiming multiple different issues."""
        assert try_claim(1) is True
        assert try_claim(2) is True
        assert try_claim(3) is True

        assert is_claimed(1)
        assert is_claimed(2)
        assert is_claimed(3)

    def test_try_claim_concurrent_attempts(self):
        """Test that only one claim succeeds when attempted concurrently."""
        # First claim
        result1 = try_claim(1)

        # Second claim (simulates concurrent attempt)
        result2 = try_claim(1)

        # Only one should succeed
        assert result1 is True
        assert result2 is False


class TestReleaseClaim:
    """Test the release_claim function."""

    def test_release_claim_success(self):
        """Test releasing a claimed issue."""
        try_claim(1)
        assert is_claimed(1)

        release_claim(1)

        assert not is_claimed(1)

    def test_release_claim_unclaimed_issue(self):
        """Test releasing an issue that was never claimed (should not error)."""
        # Should not raise an exception
        release_claim(999)

    def test_release_claim_allows_reclaim(self):
        """Test that an issue can be reclaimed after being released."""
        # Claim, release, then claim again
        assert try_claim(1) is True
        release_claim(1)
        assert try_claim(1) is True

    def test_release_claim_multiple_issues(self):
        """Test releasing multiple different issues."""
        try_claim(1)
        try_claim(2)
        try_claim(3)

        release_claim(1)
        release_claim(2)

        assert not is_claimed(1)
        assert not is_claimed(2)
        assert is_claimed(3)


class TestIsClaimed:
    """Test the is_claimed function."""

    def test_is_claimed_true(self):
        """Test checking a claimed issue."""
        try_claim(1)
        assert is_claimed(1) is True

    def test_is_claimed_false(self):
        """Test checking an unclaimed issue."""
        assert is_claimed(999) is False

    def test_is_claimed_after_release(self):
        """Test checking an issue after it's been released."""
        try_claim(1)
        release_claim(1)
        assert is_claimed(1) is False

    def test_is_claimed_no_lock_dir(self):
        """Test checking when lock directory doesn't exist."""
        # Ensure lock dir doesn't exist
        if LOCK_DIR.exists():
            LOCK_DIR.rmdir()

        assert is_claimed(1) is False


class TestListClaimed:
    """Test the list_claimed function."""

    def test_list_claimed_empty(self):
        """Test listing claimed issues when none are claimed."""
        result = list_claimed()
        assert result == []

    def test_list_claimed_single_issue(self):
        """Test listing claimed issues with one claim."""
        try_claim(1)
        result = list_claimed()
        assert result == [1]

    def test_list_claimed_multiple_issues(self):
        """Test listing claimed issues with multiple claims."""
        try_claim(1)
        try_claim(5)
        try_claim(10)

        result = list_claimed()

        # Should contain all claimed issues (order doesn't matter)
        assert set(result) == {1, 5, 10}
        assert len(result) == 3

    def test_list_claimed_after_releases(self):
        """Test listing claimed issues after some are released."""
        try_claim(1)
        try_claim(2)
        try_claim(3)

        release_claim(2)

        result = list_claimed()

        assert set(result) == {1, 3}
        assert 2 not in result

    def test_list_claimed_no_lock_dir(self):
        """Test listing claimed issues when lock directory doesn't exist."""
        # Ensure lock dir doesn't exist
        if LOCK_DIR.exists():
            LOCK_DIR.rmdir()

        result = list_claimed()
        assert result == []

    def test_list_claimed_ignores_invalid_names(self):
        """Test that list_claimed ignores directories with invalid names."""
        try_claim(1)

        # Create a directory with an invalid name (should be ignored)
        (LOCK_DIR / "not-an-issue").mkdir()
        (LOCK_DIR / "issue-not-a-number").mkdir()

        result = list_claimed()

        # Should only include the valid issue number
        assert result == [1]


class TestIntegrationScenarios:
    """Test integration scenarios with multiple operations."""

    def test_full_lifecycle(self):
        """Test complete lifecycle: claim, check, release, reclaim."""
        issue_num = 42

        # Initially unclaimed
        assert not is_claimed(issue_num)
        assert issue_num not in list_claimed()

        # Claim it
        assert try_claim(issue_num) is True
        assert is_claimed(issue_num)
        assert issue_num in list_claimed()

        # Try to claim again (should fail)
        assert try_claim(issue_num) is False

        # Release it
        release_claim(issue_num)
        assert not is_claimed(issue_num)
        assert issue_num not in list_claimed()

        # Reclaim it
        assert try_claim(issue_num) is True
        assert is_claimed(issue_num)

    def test_multiple_instances_coordination(self):
        """Test coordination between multiple instances (simulated)."""
        # Instance 1 claims issue 1
        assert try_claim(1) is True

        # Instance 2 tries to claim issue 1 (should fail)
        assert try_claim(1) is False

        # Instance 2 claims issue 2 instead
        assert try_claim(2) is True

        # Both issues are claimed
        claimed = list_claimed()
        assert set(claimed) == {1, 2}

        # Instance 1 finishes and releases
        release_claim(1)

        # Now instance 2 could claim issue 1
        assert try_claim(1) is True

    def test_crash_recovery_scenario(self):
        """Test that orphaned locks can be detected and cleaned up."""
        # Simulate an instance claiming issues
        try_claim(1)
        try_claim(2)
        try_claim(3)

        # Simulate instance crash (locks remain)
        claimed = list_claimed()
        assert set(claimed) == {1, 2, 3}

        # Recovery process can see all claimed issues
        # and potentially clean them up based on age or other criteria
        for issue_num in claimed:
            release_claim(issue_num)

        # All should be released now
        assert list_claimed() == []


class TestGetClaimAge:
    """Test the get_claim_age function."""

    def test_get_claim_age_claimed_issue(self):
        """Test getting age of a claimed issue."""
        try_claim(1)
        time.sleep(0.1)  # Small delay to ensure age > 0

        age = get_claim_age(1)

        assert age is not None
        assert age >= 0.1
        assert age < 1.0  # Should be a fraction of a second

    def test_get_claim_age_unclaimed_issue(self):
        """Test getting age of an unclaimed issue."""
        age = get_claim_age(999)
        assert age is None

    def test_get_claim_age_no_timestamp_file(self):
        """Test getting age when timestamp file is missing."""
        # Create lock directory without timestamp file
        lock_path = LOCK_DIR / "issue-1"
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_path.mkdir()

        age = get_claim_age(1)
        assert age is None

    def test_get_claim_age_invalid_timestamp(self):
        """Test getting age when timestamp file contains invalid data."""
        # Create lock with invalid timestamp
        lock_path = LOCK_DIR / "issue-1"
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_path.mkdir()
        (lock_path / TIMESTAMP_FILE).write_text("not-a-number")

        age = get_claim_age(1)
        assert age is None

    def test_get_claim_age_multiple_issues(self):
        """Test getting ages of multiple claimed issues."""
        try_claim(1)
        time.sleep(0.05)
        try_claim(2)
        time.sleep(0.05)
        try_claim(3)

        age1 = get_claim_age(1)
        age2 = get_claim_age(2)
        age3 = get_claim_age(3)

        # Issue 1 should be oldest
        assert age1 > age2 > age3


class TestIsClaimStale:
    """Test the is_claim_stale function."""

    def test_is_claim_stale_fresh_claim(self):
        """Test that a fresh claim is not stale."""
        try_claim(1)

        # With default 60 minute threshold, should not be stale
        assert is_claim_stale(1, max_age_minutes=60) is False

        # Even with 1 second threshold, should not be stale
        assert is_claim_stale(1, max_age_minutes=0.001) is False

    def test_is_claim_stale_old_claim(self):
        """Test that an old claim is stale."""
        # Create a claim with a very old timestamp
        lock_path = LOCK_DIR / "issue-1"
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_path.mkdir()
        # Timestamp from 2 hours ago
        old_timestamp = time.time() - (2 * 60 * 60)
        (lock_path / TIMESTAMP_FILE).write_text(str(old_timestamp))

        # Should be stale with 60 minute threshold
        assert is_claim_stale(1, max_age_minutes=60) is True

        # Should not be stale with 3 hour threshold
        assert is_claim_stale(1, max_age_minutes=180) is False

    def test_is_claim_stale_unclaimed_issue(self):
        """Test that an unclaimed issue is not considered stale."""
        assert is_claim_stale(999, max_age_minutes=60) is False

    def test_is_claim_stale_no_timestamp(self):
        """Test that a claim without timestamp is not considered stale."""
        # Create lock directory without timestamp file
        lock_path = LOCK_DIR / "issue-1"
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_path.mkdir()

        assert is_claim_stale(1, max_age_minutes=60) is False

    def test_is_claim_stale_custom_threshold(self):
        """Test is_claim_stale with custom threshold."""
        # Create a claim that's 5 minutes old
        lock_path = LOCK_DIR / "issue-1"
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_path.mkdir()
        old_timestamp = time.time() - (5 * 60)
        (lock_path / TIMESTAMP_FILE).write_text(str(old_timestamp))

        # Should be stale with 1 minute threshold
        assert is_claim_stale(1, max_age_minutes=1) is True

        # Should not be stale with 10 minute threshold
        assert is_claim_stale(1, max_age_minutes=10) is False


class TestCleanupStaleClaims:
    """Test the cleanup_stale_claims function."""

    def test_cleanup_stale_claims_no_claims(self):
        """Test cleanup when there are no claims."""
        cleaned = cleanup_stale_claims()
        assert cleaned == []

    def test_cleanup_stale_claims_all_fresh(self):
        """Test cleanup when all claims are fresh."""
        try_claim(1)
        try_claim(2)
        try_claim(3)

        cleaned = cleanup_stale_claims(max_age_minutes=60)

        assert cleaned == []
        # All claims should still exist
        assert set(list_claimed()) == {1, 2, 3}

    def test_cleanup_stale_claims_all_stale(self):
        """Test cleanup when all claims are stale."""
        # Create multiple stale claims
        for issue_num in [1, 2, 3]:
            lock_path = LOCK_DIR / f"issue-{issue_num}"
            LOCK_DIR.mkdir(parents=True, exist_ok=True)
            lock_path.mkdir()
            # Timestamp from 2 hours ago
            old_timestamp = time.time() - (2 * 60 * 60)
            (lock_path / TIMESTAMP_FILE).write_text(str(old_timestamp))

        cleaned = cleanup_stale_claims(max_age_minutes=60)

        assert set(cleaned) == {1, 2, 3}
        # All claims should be removed
        assert list_claimed() == []

    def test_cleanup_stale_claims_mixed(self):
        """Test cleanup with mix of fresh and stale claims."""
        # Create fresh claims
        try_claim(1)
        try_claim(2)

        # Create stale claim
        lock_path = LOCK_DIR / "issue-3"
        lock_path.mkdir()
        old_timestamp = time.time() - (2 * 60 * 60)
        (lock_path / TIMESTAMP_FILE).write_text(str(old_timestamp))

        cleaned = cleanup_stale_claims(max_age_minutes=60)

        assert cleaned == [3]
        # Only fresh claims should remain
        assert set(list_claimed()) == {1, 2}

    def test_cleanup_stale_claims_custom_threshold(self):
        """Test cleanup with custom age threshold."""
        # Create claims with different ages
        # Fresh claim
        try_claim(1)

        # 5 minute old claim
        lock_path_2 = LOCK_DIR / "issue-2"
        lock_path_2.mkdir()
        timestamp_5min = time.time() - (5 * 60)
        (lock_path_2 / TIMESTAMP_FILE).write_text(str(timestamp_5min))

        # 15 minute old claim
        lock_path_3 = LOCK_DIR / "issue-3"
        lock_path_3.mkdir()
        timestamp_15min = time.time() - (15 * 60)
        (lock_path_3 / TIMESTAMP_FILE).write_text(str(timestamp_15min))

        # Cleanup with 10 minute threshold
        cleaned = cleanup_stale_claims(max_age_minutes=10)

        assert cleaned == [3]
        # Issues 1 and 2 should remain
        assert set(list_claimed()) == {1, 2}

    def test_cleanup_stale_claims_returns_list(self):
        """Test that cleanup returns a list of cleaned issue numbers."""
        # Create stale claims
        for issue_num in [10, 20, 30]:
            lock_path = LOCK_DIR / f"issue-{issue_num}"
            LOCK_DIR.mkdir(parents=True, exist_ok=True)
            lock_path.mkdir()
            old_timestamp = time.time() - (2 * 60 * 60)
            (lock_path / TIMESTAMP_FILE).write_text(str(old_timestamp))

        cleaned = cleanup_stale_claims(max_age_minutes=60)

        assert isinstance(cleaned, list)
        assert set(cleaned) == {10, 20, 30}

    def test_cleanup_stale_claims_handles_missing_timestamp(self):
        """Test that cleanup handles claims without timestamp gracefully."""
        # Fresh claim with timestamp
        try_claim(1)

        # Claim without timestamp file
        lock_path = LOCK_DIR / "issue-2"
        lock_path.mkdir()

        # Old claim with timestamp
        lock_path_3 = LOCK_DIR / "issue-3"
        lock_path_3.mkdir()
        old_timestamp = time.time() - (2 * 60 * 60)
        (lock_path_3 / TIMESTAMP_FILE).write_text(str(old_timestamp))

        cleaned = cleanup_stale_claims(max_age_minutes=60)

        # Only issue 3 should be cleaned (issue 2 has no timestamp, so not considered stale)
        assert cleaned == [3]
        assert set(list_claimed()) == {1, 2}


class TestPauseResume:
    """Test the pause/resume functionality."""

    def test_set_paused(self):
        """Test setting the orchestrator to paused state."""
        assert not is_paused()

        set_paused()

        assert is_paused()
        assert PAUSE_LOCK.exists()

    def test_set_resumed(self):
        """Test resuming the orchestrator from paused state."""
        set_paused()
        assert is_paused()

        set_resumed()

        assert not is_paused()
        assert not PAUSE_LOCK.exists()

    def test_is_paused_false_initially(self):
        """Test that orchestrator is not paused initially."""
        assert not is_paused()

    def test_set_resumed_when_not_paused(self):
        """Test that resuming when not paused is safe."""
        assert not is_paused()

        # Should not raise an error
        set_resumed()

        assert not is_paused()

    def test_set_paused_multiple_times(self):
        """Test that pausing multiple times is safe."""
        set_paused()
        assert is_paused()

        # Should not raise an error
        set_paused()

        assert is_paused()

    def test_pause_resume_cycle(self):
        """Test complete pause-resume cycle."""
        # Initially not paused
        assert not is_paused()

        # Pause
        set_paused()
        assert is_paused()

        # Resume
        set_resumed()
        assert not is_paused()

        # Pause again
        set_paused()
        assert is_paused()

        # Resume again
        set_resumed()
        assert not is_paused()

    def test_pause_lock_creates_lock_dir(self):
        """Test that set_paused creates the lock directory if it doesn't exist."""
        # Ensure lock dir doesn't exist
        if LOCK_DIR.exists():
            LOCK_DIR.rmdir()

        assert not LOCK_DIR.exists()

        set_paused()

        assert LOCK_DIR.exists()
        assert PAUSE_LOCK.exists()

    def test_pause_state_persists(self):
        """Test that pause state persists across checks."""
        set_paused()

        # Multiple checks should return True
        assert is_paused()
        assert is_paused()
        assert is_paused()

        set_resumed()

        # Multiple checks should return False
        assert not is_paused()
        assert not is_paused()
        assert not is_paused()
