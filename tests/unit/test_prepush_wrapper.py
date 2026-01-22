"""Tests for pre-push wrapper hook.

The pre-push wrapper chains project and orchestrator hooks.
Output capture is handled by validate_runner.py, not the wrapper itself.
"""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


class TestPrepushWrapper:
    """Test that the pre-push wrapper chains hooks correctly."""

    @pytest.fixture
    def wrapper_script(self) -> Path:
        """Get path to the wrapper template."""
        return Path(__file__).parent.parent.parent / "src" / "issue_orchestrator" / "templates" / "hooks" / "git" / "pre-push-wrapper.sh"

    @pytest.fixture
    def fake_git_repo(self, tmp_path: Path) -> Path:
        """Create a fake git repo structure for testing."""
        repo = tmp_path / "repo"
        repo.mkdir()

        # Initialize as git repo
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)

        return repo

    @pytest.fixture
    def hooks_dir(self, fake_git_repo: Path) -> Path:
        """Get hooks directory and ensure it exists."""
        hooks = fake_git_repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        return hooks

    def test_wrapper_runs_project_hook(
        self, fake_git_repo: Path, hooks_dir: Path, wrapper_script: Path
    ):
        """Test that wrapper runs the project hook."""
        # Install the wrapper
        wrapper_dest = hooks_dir / "pre-push"
        wrapper_dest.write_text(wrapper_script.read_text())
        wrapper_dest.chmod(0o755)

        # Create a project hook that creates a marker file
        marker = fake_git_repo / "project-hook-ran"
        project_hook = hooks_dir / "pre-push.project"
        project_hook.write_text(textwrap.dedent(f"""
            #!/bin/bash
            touch "{marker}"
            exit 0
        """).strip())
        project_hook.chmod(0o755)

        # Run the wrapper
        result = subprocess.run(
            [str(wrapper_dest)],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )

        assert result.returncode == 0
        assert marker.exists(), "Project hook should have run"

    def test_wrapper_runs_orchestrator_hook(
        self, fake_git_repo: Path, hooks_dir: Path, wrapper_script: Path
    ):
        """Test that wrapper runs the orchestrator hook."""
        # Install the wrapper
        wrapper_dest = hooks_dir / "pre-push"
        wrapper_dest.write_text(wrapper_script.read_text())
        wrapper_dest.chmod(0o755)

        # Create an orchestrator hook that creates a marker file
        marker = fake_git_repo / "orch-hook-ran"
        orch_hook = hooks_dir / "pre-push.orchestrator"
        orch_hook.write_text(textwrap.dedent(f"""
            #!/bin/bash
            touch "{marker}"
            exit 0
        """).strip())
        orch_hook.chmod(0o755)

        # Run the wrapper
        result = subprocess.run(
            [str(wrapper_dest)],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )

        assert result.returncode == 0
        assert marker.exists(), "Orchestrator hook should have run"

    def test_wrapper_fails_if_project_hook_fails(
        self, fake_git_repo: Path, hooks_dir: Path, wrapper_script: Path
    ):
        """Test that wrapper exits with failure if project hook fails."""
        # Install the wrapper
        wrapper_dest = hooks_dir / "pre-push"
        wrapper_dest.write_text(wrapper_script.read_text())
        wrapper_dest.chmod(0o755)

        # Create a failing project hook
        project_hook = hooks_dir / "pre-push.project"
        project_hook.write_text(textwrap.dedent("""
            #!/bin/bash
            echo "Project validation failed"
            exit 1
        """).strip())
        project_hook.chmod(0o755)

        # Run the wrapper
        result = subprocess.run(
            [str(wrapper_dest)],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )

        assert result.returncode == 1

    def test_wrapper_fails_if_orchestrator_hook_fails(
        self, fake_git_repo: Path, hooks_dir: Path, wrapper_script: Path
    ):
        """Test that wrapper exits with failure if orchestrator hook fails."""
        # Install the wrapper
        wrapper_dest = hooks_dir / "pre-push"
        wrapper_dest.write_text(wrapper_script.read_text())
        wrapper_dest.chmod(0o755)

        # Create passing project hook
        project_hook = hooks_dir / "pre-push.project"
        project_hook.write_text(textwrap.dedent("""
            #!/bin/bash
            exit 0
        """).strip())
        project_hook.chmod(0o755)

        # Create failing orchestrator hook
        orch_hook = hooks_dir / "pre-push.orchestrator"
        orch_hook.write_text(textwrap.dedent("""
            #!/bin/bash
            echo "Orchestrator validation failed"
            exit 1
        """).strip())
        orch_hook.chmod(0o755)

        # Run the wrapper
        result = subprocess.run(
            [str(wrapper_dest)],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )

        assert result.returncode == 1

    def test_wrapper_writes_audit_log(
        self, fake_git_repo: Path, hooks_dir: Path, wrapper_script: Path
    ):
        """Test that wrapper writes audit trail."""
        # Install the wrapper
        wrapper_dest = hooks_dir / "pre-push"
        wrapper_dest.write_text(wrapper_script.read_text())
        wrapper_dest.chmod(0o755)

        # Run the wrapper
        subprocess.run(
            [str(wrapper_dest)],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )

        # Check audit log was created
        log_file = hooks_dir / "pre-push.log"
        assert log_file.exists(), "Audit log should be created"
        log_content = log_file.read_text()
        assert "wrapper-started" in log_content
        assert "wrapper-completed" in log_content
