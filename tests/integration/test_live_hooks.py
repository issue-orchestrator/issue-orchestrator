"""Integration tests for live hook verification.

These tests verify that the hook enforcement system works end-to-end by
actually spawning Claude and testing that --no-verify commands are blocked
(or not blocked when hooks are missing).

Uses static fixtures in tests/fixtures/ with local bare git repos as remotes.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Mark all tests in this module as integration tests that spawn Claude
pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,  # These tests actually spawn Claude
]


@pytest.fixture
def fixtures_path() -> Path:
    """Path to the test fixtures directory."""
    return Path(__file__).parent.parent / "fixtures"


def _clean_git_env() -> dict[str, str]:
    """Return environment with git variables cleared to prevent leaking to test repos.

    Without this, GIT_DIR/GIT_WORK_TREE from parent processes can cause
    git commands in test repos to write config to the wrong repository.
    """
    env = os.environ.copy()
    for var in ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"]:
        env.pop(var, None)
    return env


@pytest.fixture
def test_repo_with_hooks(tmp_path: Path, fixtures_path: Path) -> Path:
    """Create a test git repo with hooks installed.

    Sets up:
    - A bare repo acting as "origin" remote
    - A working repo cloned from it with hooks installed
    - A commit ready to push

    Returns the path to the working repo.
    """
    # Clean environment to prevent git config leaking to main repo
    clean_env = _clean_git_env()

    # Create bare "remote" repo
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(
        ["git", "init", "--bare"],
        cwd=remote,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Create working repo
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Configure git user
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=work,
        env=clean_env,
        capture_output=True,
        check=True,
    )

    # Add remote
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Copy hooks from fixture
    src_claude = fixtures_path / "hooks-installed" / ".claude"
    dst_claude = work / ".claude"
    shutil.copytree(src_claude, dst_claude)

    # Ensure hook is executable
    hook_script = dst_claude / "hooks" / "block-no-verify.sh"
    hook_script.chmod(0o755)

    # Create initial commit and push to establish branch
    readme = work / "README.md"
    readme.write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=work, capture_output=True, check=True, env=clean_env)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Create a new commit ready to push (for testing)
    test_file = work / "test.txt"
    test_file.write_text("test content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=work, capture_output=True, check=True, env=clean_env)
    subprocess.run(
        ["git", "commit", "-m", "Test commit"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    return work


@pytest.fixture
def test_repo_without_hooks(tmp_path: Path, fixtures_path: Path) -> Path:
    """Create a test git repo WITHOUT hooks installed.

    Same setup as test_repo_with_hooks but no .claude directory.
    Used to verify that we correctly detect when hooks are missing.

    Returns the path to the working repo.
    """
    # Clean environment to prevent git config leaking to main repo
    clean_env = _clean_git_env()

    # Create bare "remote" repo
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(
        ["git", "init", "--bare"],
        cwd=remote,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Create working repo
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Configure git user
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Add remote
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Copy content from fixture (but NO .claude directory)
    src_readme = fixtures_path / "hooks-missing" / "README.md"
    dst_readme = work / "README.md"
    shutil.copy(src_readme, dst_readme)

    # Create initial commit and push
    subprocess.run(["git", "add", "."], cwd=work, capture_output=True, check=True, env=clean_env)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=work,
        capture_output=True,
        check=True,
        env=clean_env,
    )

    # Create a new commit ready to push
    test_file = work / "test.txt"
    test_file.write_text("test content\n")
    subprocess.run(["git", "add", "test.txt"], cwd=work, capture_output=True, check=True, env=clean_env)
    subprocess.run(
        ["git", "commit", "-m", "Test commit"],
        cwd=work,
        capture_output=True,
        env=clean_env,
        check=True,
    )

    return work


class TestHookBlocking:
    """Tests that verify hooks correctly block --no-verify commands."""

    def test_hook_script_blocks_no_verify(self, test_repo_with_hooks: Path):
        """Test that the hook script blocks --no-verify when called directly."""
        import json

        hook_script = test_repo_with_hooks / ".claude" / "hooks" / "block-no-verify.sh"

        # Simulate Claude Code's PreToolUse input
        test_input = json.dumps({
            "tool_input": {
                "command": "git push --no-verify"
            }
        })

        result = subprocess.run(
            [str(hook_script)],
            input=test_input,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 2, f"Expected exit code 2 (blocked), got {result.returncode}"
        assert "BLOCKED" in result.stderr

    def test_hook_script_allows_normal_push(self, test_repo_with_hooks: Path):
        """Test that the hook script allows normal git push."""
        import json

        hook_script = test_repo_with_hooks / ".claude" / "hooks" / "block-no-verify.sh"

        test_input = json.dumps({
            "tool_input": {
                "command": "git push origin main"
            }
        })

        result = subprocess.run(
            [str(hook_script)],
            input=test_input,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Expected exit code 0 (allowed), got {result.returncode}"


class TestLiveVerification:
    """Tests that spawn Claude to verify end-to-end hook enforcement.

    These tests are slower and require Claude CLI to be installed.
    They verify the ENTIRE chain works, not just the hook script.
    """

    @pytest.fixture
    def skip_if_no_claude(self):
        """Skip test if Claude CLI is not available."""
        result = subprocess.run(
            ["which", "claude"],
            capture_output=True,
        )
        if result.returncode != 0:
            pytest.skip("Claude CLI not installed")

    def test_live_verify_blocks_no_verify(
        self,
        test_repo_with_hooks: Path,
        skip_if_no_claude,
    ):
        """Test that Claude is actually blocked from running --no-verify.

        This spawns Claude and has it try to run the blocked command.
        """
        from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        success, message = adapter.live_verify(test_repo_with_hooks, timeout=60)

        assert success, f"Live verification should pass (hooks installed): {message}"
        assert "blocked" in message.lower()

    def test_live_verify_detects_missing_hooks(
        self,
        test_repo_without_hooks: Path,
        skip_if_no_claude,
    ):
        """Test that live verification correctly detects when hooks are missing.

        This is the critical failure-detection test - we need to verify that
        when hooks are NOT installed, the verification reports FAILURE.
        """
        from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        success, message = adapter.live_verify(test_repo_without_hooks, timeout=60)

        # This should FAIL because hooks are not installed
        # Claude should be able to run --no-verify without being blocked
        assert not success, (
            f"Live verification should FAIL (no hooks): {message}\n"
            "If this passes, our failure detection is broken!"
        )


class TestVerificationResult:
    """Tests for the static (non-live) verification."""

    def test_verify_hooks_passes_with_hooks(self, test_repo_with_hooks: Path):
        """Test that verify_hooks passes when hooks are properly installed."""
        from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        result = adapter.verify_hooks(test_repo_with_hooks)

        assert result.success, f"Verification should pass: {result.checks_failed}"
        assert len(result.checks_passed) > 0

    def test_verify_hooks_fails_without_hooks(self, test_repo_without_hooks: Path):
        """Test that verify_hooks fails when hooks are missing."""
        from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        result = adapter.verify_hooks(test_repo_without_hooks)

        assert not result.success, "Verification should fail when hooks missing"
        assert len(result.checks_failed) > 0

    def test_is_installed_true_with_hooks(self, test_repo_with_hooks: Path):
        """Test that is_installed returns True when hooks are installed."""
        from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        assert adapter.is_installed(test_repo_with_hooks)

    def test_is_installed_false_without_hooks(self, test_repo_without_hooks: Path):
        """Test that is_installed returns False when hooks are missing."""
        from issue_orchestrator.infra.hooks.hooks import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        assert not adapter.is_installed(test_repo_without_hooks)
