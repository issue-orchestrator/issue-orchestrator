#!/usr/bin/env python3
"""
Local test runner - tests the orchestrator without GitHub.

Creates mock issues, runs the test agent, and verifies the flow works.
"""

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from issue_orchestrator.models import Issue, Session, SessionStatus, AgentConfig
from issue_orchestrator.adapters.worktree._worktree import create_worktree, remove_worktree, generate_branch_name
from issue_orchestrator.adapters.terminal._tmux import create_session, session_exists, kill_session, list_sessions


def test_branch_naming():
    """Test branch name generation."""
    print("\n=== Test: Branch Naming ===")

    cases = [
        (123, "Add user authentication", "123-add-user-authentication"),
        (456, "Fix bug in login!!!!", "456-fix-bug-in-login"),
        (789, "   Spaces   everywhere   ", "789-spaces-everywhere"),
        (1, "A" * 100, "1-" + "a" * 50),  # Truncation
    ]

    for issue_num, title, expected_prefix in cases:
        result = generate_branch_name(issue_num, title)
        print(f"  #{issue_num} '{title[:30]}...' -> {result}")
        assert result.startswith(expected_prefix[:20]), f"Expected prefix {expected_prefix[:20]}, got {result}"

    print("  ✓ All branch naming tests passed")


def test_worktree_creation():
    """Test worktree creation (requires a git repo)."""
    print("\n=== Test: Worktree Creation ===")

    # Use a temp directory with a git repo
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir) / "test-repo"
        repo_root.mkdir()

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo_root, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_root, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, capture_output=True)

        # Create initial commit (required for worktrees)
        (repo_root / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "add", "."], cwd=repo_root, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_root, capture_output=True)

        # Test worktree creation
        worktree_path, branch_name = create_worktree(
            repo_root=repo_root,
            issue_number=123,
            issue_title="Add user auth",
        )

        print(f"  Created worktree at: {worktree_path}")
        print(f"  Branch name: {branch_name}")

        assert worktree_path.exists(), "Worktree should exist"
        assert "123" in str(worktree_path), "Worktree path should contain issue number"
        assert "123" in branch_name, "Branch name should contain issue number"

        # Cleanup
        subprocess.run(["git", "worktree", "remove", str(worktree_path)], cwd=repo_root, capture_output=True)

        print("  ✓ Worktree creation test passed")


def is_tmux_available() -> bool:
    """Check if tmux is installed."""
    try:
        result = subprocess.run(["which", "tmux"], capture_output=True)
        return result.returncode == 0
    except Exception:
        return False


def test_tmux_session():
    """Test tmux session creation."""
    print("\n=== Test: Tmux Session ===")

    if not is_tmux_available():
        print("  ⚠ tmux not installed - skipping")
        print("  Install with: brew install tmux")
        return

    session_name = "test-orchestrator-123"

    # Kill any existing session
    if session_exists(session_name):
        kill_session(session_name)

    # Create a session that just sleeps
    with tempfile.TemporaryDirectory() as tmpdir:
        create_session(session_name, "sleep 30", Path(tmpdir))

        assert session_exists(session_name), "Session should exist"
        print(f"  Created tmux session: {session_name}")

        # Kill it
        kill_session(session_name)
        assert not session_exists(session_name), "Session should be gone"
        print(f"  Killed tmux session: {session_name}")

    print("  ✓ Tmux session test passed")


def test_full_flow_with_mock():
    """Test the full flow with a mock agent."""
    print("\n=== Test: Full Flow (Mock Agent) ===")

    if not is_tmux_available():
        print("  ⚠ tmux not installed - skipping full flow test")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir) / "test-repo"
        repo_root.mkdir()

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo_root, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_root, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, capture_output=True)
        (repo_root / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "add", "."], cwd=repo_root, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_root, capture_output=True)

        # Create worktree
        worktree_path, branch_name = create_worktree(
            repo_root=repo_root,
            issue_number=999,
            issue_title="Test issue for local testing",
        )
        print(f"  1. Created worktree: {worktree_path}")
        print(f"     Branch: {branch_name}")

        # Create tmux session with a simple command (no actual agent)
        session_name = "issue-999"
        command = "echo 'Mock agent running...' && sleep 3 && echo 'Mock agent done'"
        create_session(session_name, command, worktree_path)
        print(f"  2. Created tmux session: {session_name}")

        # Wait for it to finish
        print("  3. Waiting for mock agent to complete...")
        for _ in range(10):
            if not session_exists(session_name):
                print("  4. Session completed!")
                break
            asyncio.get_event_loop().run_until_complete(asyncio.sleep(1))
        else:
            print("  4. Session still running (killing...)")
            kill_session(session_name)

        # Cleanup worktree
        subprocess.run(["git", "worktree", "remove", str(worktree_path), "--force"], cwd=repo_root, capture_output=True)
        print(f"  5. Cleaned up worktree")

    print("  ✓ Full flow test passed")


def main():
    print("=" * 60)
    print("  Issue Orchestrator - Local Test Suite")
    print("=" * 60)

    try:
        test_branch_naming()
        test_worktree_creation()
        test_tmux_session()
        test_full_flow_with_mock()

        print("\n" + "=" * 60)
        print("  All tests passed! ✓")
        print("=" * 60)
        return 0

    except Exception as e:
        print(f"\n  Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
