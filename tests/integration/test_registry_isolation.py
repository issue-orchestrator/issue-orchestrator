"""Tests for registry isolation in integration tests.

These tests verify that the isolated_registry fixture properly isolates
subprocess-based tests from the production registry.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

class TestRegistryIsolation:
    """Verify registry isolation works correctly."""

    def test_env_var_is_set(self, isolated_registry: Path) -> None:
        """Fixture sets the ISSUE_ORCHESTRATOR_CONFIG_DIR env var."""
        assert "ISSUE_ORCHESTRATOR_CONFIG_DIR" in os.environ
        assert os.environ["ISSUE_ORCHESTRATOR_CONFIG_DIR"] == str(isolated_registry)

    def test_subprocess_inherits_env_var(self, isolated_registry: Path) -> None:
        """Subprocesses inherit the isolated config directory."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('ISSUE_ORCHESTRATOR_CONFIG_DIR', 'NOT_SET'))",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == str(isolated_registry)

    def test_subprocess_uses_isolated_registry(self, isolated_registry: Path) -> None:
        """Subprocesses use the isolated registry for repo operations."""
        # Create a repo to register
        test_repo = isolated_registry / "test-repo"
        test_repo.mkdir()

        # Run subprocess that registers and lists repos
        code = """
import json
from pathlib import Path
from issue_orchestrator.infra.repo_registry import add_repo, list_repos, _repos_file

# Register a repo
test_repo = Path("{test_repo}")
add_repo(test_repo)

# List repos
repos = list_repos()

# Output results
print(json.dumps({{
    "repos_file": str(_repos_file()),
    "count": len(repos),
    "paths": [r.path for r in repos],
}}))
""".format(test_repo=test_repo)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Subprocess failed: {result.stderr}"

        import json
        data = json.loads(result.stdout)

        # Verify registry file is in the isolated directory
        assert str(isolated_registry) in data["repos_file"]
        assert data["count"] == 1
        assert str(test_repo.resolve()) in data["paths"]

        # Verify registry file was created in the isolated directory
        registry_file = isolated_registry / "repos.json"
        assert registry_file.exists()

    def test_registry_not_polluted_after_test(self, isolated_registry: Path) -> None:
        """Registry created during test is in isolated dir, not production."""
        # The isolated_registry fixture ensures we're writing to tmp_path
        # This test verifies the mechanism works
        production_registry = Path.home() / ".config" / "issue-orchestrator" / "repos.json"

        # Get production registry content before (if exists)
        before_content = None
        if production_registry.exists():
            before_content = production_registry.read_text()

        # Register a test repo in isolated registry
        test_repo = isolated_registry / "should-not-appear-in-production"
        test_repo.mkdir()

        from issue_orchestrator.infra.repo_registry import add_repo, _repos_file

        add_repo(test_repo)

        # Verify it went to isolated registry
        assert str(isolated_registry) in str(_repos_file())

        # Production registry should be unchanged
        if production_registry.exists():
            after_content = production_registry.read_text()
            # If production registry existed, content should be unchanged
            # (We can't fully verify this if no production registry exists)
            if before_content is not None:
                # Only check if test repo path appeared if production registry existed before
                # and content changed
                if before_content != after_content:
                    assert "should-not-appear-in-production" not in after_content
