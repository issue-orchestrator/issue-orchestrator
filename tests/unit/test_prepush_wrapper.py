"""Tests for pre-push wrapper hook output capture.

The pre-push wrapper captures validation output to a file so agents
can read it after a failure, instead of having to re-run tests.
"""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


class TestPrepushWrapperOutputCapture:
    """Test that the pre-push wrapper captures output correctly."""

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

        # Create .issue-orchestrator directory (will be created by hook)
        return repo

    @pytest.fixture
    def hooks_dir(self, fake_git_repo: Path) -> Path:
        """Get hooks directory and ensure it exists."""
        hooks = fake_git_repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        return hooks

    def test_output_captured_on_project_hook_failure(
        self, fake_git_repo: Path, hooks_dir: Path, wrapper_script: Path
    ):
        """Test that project hook output is captured to diagnostics file."""
        # Install the wrapper
        wrapper_dest = hooks_dir / "pre-push"
        wrapper_dest.write_text(wrapper_script.read_text())
        wrapper_dest.chmod(0o755)

        # Create a failing project hook that outputs text
        project_hook = hooks_dir / "pre-push.project"
        project_hook.write_text(textwrap.dedent("""
            #!/bin/bash
            echo "Running tests..."
            echo "FAILED: test_something.py::test_foo"
            echo "AssertionError: expected 42, got 41"
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

        # Verify failure
        assert result.returncode == 1

        # Verify output file was created
        output_file = fake_git_repo / ".issue-orchestrator" / "diagnostics" / "prepush-output.log"
        assert output_file.exists(), f"Output file should exist at {output_file}"

        # Verify output file contains the test output
        output_content = output_file.read_text()
        assert "Running tests..." in output_content
        assert "FAILED: test_something.py::test_foo" in output_content
        assert "AssertionError: expected 42, got 41" in output_content

    def test_failure_message_shows_output_path(
        self, fake_git_repo: Path, hooks_dir: Path, wrapper_script: Path
    ):
        """Test that failure message tells the agent where to find output."""
        # Install the wrapper
        wrapper_dest = hooks_dir / "pre-push"
        wrapper_dest.write_text(wrapper_script.read_text())
        wrapper_dest.chmod(0o755)

        # Create a failing project hook
        project_hook = hooks_dir / "pre-push.project"
        project_hook.write_text(textwrap.dedent("""
            #!/bin/bash
            echo "Test failure output"
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

        # Verify the output mentions where to find the full output
        combined_output = result.stdout + result.stderr
        assert "prepush-output.log" in combined_output, "Should mention output file"
        assert "Full output saved to:" in combined_output, "Should indicate output was saved"
        assert ".issue-orchestrator/diagnostics" in combined_output, "Should show diagnostics path"

    def test_output_captured_on_orchestrator_hook_failure(
        self, fake_git_repo: Path, hooks_dir: Path, wrapper_script: Path
    ):
        """Test that orchestrator hook output is captured when it fails."""
        # Install the wrapper
        wrapper_dest = hooks_dir / "pre-push"
        wrapper_dest.write_text(wrapper_script.read_text())
        wrapper_dest.chmod(0o755)

        # Create passing project hook
        project_hook = hooks_dir / "pre-push.project"
        project_hook.write_text(textwrap.dedent("""
            #!/bin/bash
            echo "Project tests passed"
            exit 0
        """).strip())
        project_hook.chmod(0o755)

        # Create failing orchestrator hook
        orch_hook = hooks_dir / "pre-push.orchestrator"
        orch_hook.write_text(textwrap.dedent("""
            #!/bin/bash
            echo "Checking commit trailers..."
            echo "ERROR: Missing Agent-Status trailer"
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

        # Verify failure
        assert result.returncode == 1

        # Verify output file contains both hooks' output
        output_file = fake_git_repo / ".issue-orchestrator" / "diagnostics" / "prepush-output.log"
        assert output_file.exists()
        output_content = output_file.read_text()
        assert "Project tests passed" in output_content
        assert "ERROR: Missing Agent-Status trailer" in output_content

    def test_success_does_not_print_failure_message(
        self, fake_git_repo: Path, hooks_dir: Path, wrapper_script: Path
    ):
        """Test that successful push doesn't print failure message."""
        # Install the wrapper
        wrapper_dest = hooks_dir / "pre-push"
        wrapper_dest.write_text(wrapper_script.read_text())
        wrapper_dest.chmod(0o755)

        # Create passing project hook
        project_hook = hooks_dir / "pre-push.project"
        project_hook.write_text(textwrap.dedent("""
            #!/bin/bash
            echo "All tests passed"
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

        # Verify success
        assert result.returncode == 0

        # Verify no failure message
        combined_output = result.stdout + result.stderr
        assert "FAILED" not in combined_output
        assert "Full output saved to:" not in combined_output

    def test_output_file_is_in_expected_location(
        self, fake_git_repo: Path, hooks_dir: Path, wrapper_script: Path
    ):
        """Test that output file is at .issue-orchestrator/diagnostics/prepush-output.log."""
        # Install the wrapper
        wrapper_dest = hooks_dir / "pre-push"
        wrapper_dest.write_text(wrapper_script.read_text())
        wrapper_dest.chmod(0o755)

        # Create a failing hook
        project_hook = hooks_dir / "pre-push.project"
        project_hook.write_text(textwrap.dedent("""
            #!/bin/bash
            echo "Test output"
            exit 1
        """).strip())
        project_hook.chmod(0o755)

        # Run the wrapper
        subprocess.run(
            [str(wrapper_dest)],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )

        # Verify exact path
        expected_path = fake_git_repo / ".issue-orchestrator" / "diagnostics" / "prepush-output.log"
        assert expected_path.exists(), f"Output should be at {expected_path}"

        # Verify directory structure
        assert (fake_git_repo / ".issue-orchestrator").is_dir()
        assert (fake_git_repo / ".issue-orchestrator" / "diagnostics").is_dir()

    def test_stderr_is_also_captured(
        self, fake_git_repo: Path, hooks_dir: Path, wrapper_script: Path
    ):
        """Test that stderr output is captured alongside stdout."""
        # Install the wrapper
        wrapper_dest = hooks_dir / "pre-push"
        wrapper_dest.write_text(wrapper_script.read_text())
        wrapper_dest.chmod(0o755)

        # Create a hook that writes to both stdout and stderr
        project_hook = hooks_dir / "pre-push.project"
        project_hook.write_text(textwrap.dedent("""
            #!/bin/bash
            echo "stdout message"
            echo "stderr message" >&2
            exit 1
        """).strip())
        project_hook.chmod(0o755)

        # Run the wrapper
        subprocess.run(
            [str(wrapper_dest)],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )

        # Verify both stdout and stderr are captured
        output_file = fake_git_repo / ".issue-orchestrator" / "diagnostics" / "prepush-output.log"
        output_content = output_file.read_text()
        assert "stdout message" in output_content
        assert "stderr message" in output_content

    def test_no_project_hook_still_works(
        self, fake_git_repo: Path, hooks_dir: Path, wrapper_script: Path
    ):
        """Test wrapper works when there's no project hook."""
        # Install the wrapper
        wrapper_dest = hooks_dir / "pre-push"
        wrapper_dest.write_text(wrapper_script.read_text())
        wrapper_dest.chmod(0o755)

        # No project hook, but add orchestrator hook
        orch_hook = hooks_dir / "pre-push.orchestrator"
        orch_hook.write_text(textwrap.dedent("""
            #!/bin/bash
            echo "Orchestrator check"
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

        # Should succeed
        assert result.returncode == 0
