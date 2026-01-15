"""Integration tests for Codex CLI execution.

These tests verify that we can actually execute Codex CLI commands
and that the agent-runner package correctly captures output and handles
the agent-done completion protocol.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def is_codex_available() -> bool:
    """Check if codex CLI is available in PATH."""
    return shutil.which("codex") is not None


@pytest.fixture
def require_codex():
    """Fixture that fails fast if Codex CLI is not installed."""
    if not is_codex_available():
        pytest.fail(
            "Codex CLI not found!\n"
            "Install Codex: npm install -g @openai/codex"
        )


@pytest.mark.skipif(not is_codex_available(), reason="Codex CLI not installed")
class TestCodexExecution:
    """Integration tests that actually run Codex CLI."""

    def test_codex_version(self):
        """Verify codex CLI is accessible and responds to --version."""
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        # Version output should contain a version number
        assert result.stdout.strip() or result.stderr.strip()

    def test_codex_help(self):
        """Verify codex CLI responds to --help."""
        result = subprocess.run(
            ["codex", "exec", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "non-interactively" in result.stdout.lower()

    def test_codex_simple_calculation(self, tmp_path):
        """Run Codex with a simple calculation task to verify execution works.

        This tests that:
        1. Codex can be invoked via subprocess
        2. The exec subcommand works for non-interactive output
        3. Codex can perform a simple task and return results
        """
        # Initialize a git repo (Codex requires this)
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path, capture_output=True, check=True
        )

        # Create a dummy file so git has something
        (tmp_path / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=tmp_path, capture_output=True, check=True
        )

        result = subprocess.run(
            [
                "codex", "exec",
                "--full-auto",
                "--skip-git-repo-check",
                "What is 2 + 2? Reply with just the number, no explanation.",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=tmp_path,
        )

        # Codex should exit successfully
        assert result.returncode == 0, f"Codex failed: {result.stderr}"

        # The output should contain "4"
        assert "4" in result.stdout, f"Expected '4' in output: {result.stdout}"


@pytest.mark.skipif(not is_codex_available(), reason="Codex CLI not installed")
class TestCodexWithAgentRunner:
    """Integration tests for Codex via AgentRunner."""

    def test_codex_via_agent_runner_captures_output(self, tmp_path):
        """Test that AgentRunner correctly captures Codex output to log files."""
        from agent_runner import AgentRunner
        from agent_runner.ports import RunSpec
        from agent_runner.providers import CodexProvider

        # Set up a git repo
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path, capture_output=True, check=True
        )
        (tmp_path / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=tmp_path, capture_output=True, check=True
        )

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        provider = CodexProvider()
        # Build command - use simpler task, let Codex use its default model
        cmd = provider.build_command(
            prompt="Echo the word SUCCESS and exit.",
            json_output="false",  # Disable JSON for cleaner output
        )

        runner = AgentRunner()
        spec = RunSpec(
            command=cmd,
            working_dir=tmp_path,
            timeout_seconds=120,
            output_dir=output_dir,
        )

        result = runner.run(spec)

        # Verify log files were created
        assert (output_dir / "stdout.log").exists(), "stdout.log should exist"
        assert (output_dir / "stderr.log").exists(), "stderr.log should exist"

        # Verify result structure
        assert result.stdout_path == output_dir / "stdout.log"
        assert result.stderr_path == output_dir / "stderr.log"
        assert result.duration_seconds > 0

        # Check output contains expected content
        stdout_content = result.stdout
        print(f"Codex stdout: {stdout_content}")
        print(f"Codex stderr: {result.stderr}")
        print(f"Exit code: {result.exit_code}")

        # Should have some output - Codex writes session info to stderr
        assert stdout_content or result.stderr, "Expected some output from Codex"
        assert len(result.stderr) > 0, "Session log (stderr) should not be empty"
        assert "codex" in result.stderr.lower(), "Session log should contain Codex output"


@pytest.mark.skipif(not is_codex_available(), reason="Codex CLI not installed")
class TestCodexAgentDoneInvocation:
    """Integration tests for agent-done invocation from Codex.

    These tests verify the critical path: Codex can invoke agent-done
    and write completion.json, which is how sessions signal completion.
    """

    def test_agent_done_invocable_from_codex(self, tmp_path, require_codex):
        """Verify Codex can invoke agent-done in worktree-like environment.

        This tests the exact mechanism the orchestrator relies on:
        1. PATH includes scripts directory with agent-done wrapper
        2. Codex runs with exec subcommand (non-interactive)
        3. Codex invokes agent-done via shell
        4. completion.json is written
        """
        # Get the scripts directory (where agent-done wrapper lives)
        repo_root = Path(__file__).parent.parent.parent
        scripts_dir = repo_root / "src" / "issue_orchestrator" / "scripts"

        # Create worktree-like structure with git repo
        worktree = tmp_path / "test-worktree"
        worktree.mkdir()
        subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=worktree, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=worktree, capture_output=True, check=True
        )
        (worktree / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=worktree, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=worktree, capture_output=True, check=True
        )

        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir()

        # Build environment like orchestrator does:
        # - Prepend scripts directory to PATH
        # - Set completion path
        env = dict(os.environ)
        env["PATH"] = f"{scripts_dir}:{env.get('PATH', '')}"
        env["ORCHESTRATOR_COMPLETION_PATH"] = str(completion_dir / "completion.json")

        # Run Codex with exec asking it to invoke agent-done
        prompt = (
            "You are in a test. Run this exact bash command and nothing else:\n"
            "agent-done completed --implementation 'test' --problems 'none'\n"
            "Do not explain, just run the command using the shell tool."
        )

        result = subprocess.run(
            [
                "codex", "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(worktree),
            env=env,
        )

        # Log output for debugging
        print(f"Codex stdout: {result.stdout}")
        print(f"Codex stderr: {result.stderr}")
        print(f"Return code: {result.returncode}")

        # Check for completion.json
        completion_files = list(completion_dir.glob("completion*.json"))
        assert len(completion_files) > 0, (
            f"No completion.json written!\n"
            f"Codex stdout: {result.stdout}\n"
            f"Codex stderr: {result.stderr}\n"
            f"Return code: {result.returncode}\n"
            f"Files in {completion_dir}: {list(completion_dir.iterdir())}"
        )

        # Validate completion record
        completion_path = completion_files[0]
        completion_data = json.loads(completion_path.read_text())
        assert completion_data.get("outcome") == "completed", (
            f"Unexpected outcome: {completion_data}"
        )

    def test_codex_file_creation(self, tmp_path, require_codex):
        """Test that Codex can create files (simpler than agent-done).

        This is a simpler verification that Codex can execute commands
        and create files in the working directory.
        """
        # Set up a git repo
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=tmp_path, capture_output=True, check=True
        )
        (tmp_path / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=tmp_path, capture_output=True, check=True
        )

        verify_file = tmp_path / "codex_was_here.txt"

        result = subprocess.run(
            [
                "codex", "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                f"Create a file at {verify_file} containing exactly the text VERIFIED. "
                "Use the write_file tool or echo command. Do not explain, just do it.",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(tmp_path),
        )

        print(f"Codex stdout: {result.stdout}")
        print(f"Codex stderr: {result.stderr}")
        print(f"Return code: {result.returncode}")

        # Verify Codex created the file
        assert verify_file.exists(), f"Codex did not create verification file. Output: {result.stdout}"
        content = verify_file.read_text().strip()
        assert "VERIFIED" in content, f"Verification file has wrong content: {content}"


@pytest.mark.skipif(not is_codex_available(), reason="Codex CLI not installed")
class TestCodexWithAgentRunnerFullPath:
    """E2E test that runs Codex through AgentRunner with full orchestrator path."""

    def test_codex_via_agent_runner_with_agent_done(self, tmp_path, require_codex):
        """Run Codex via AgentRunner and verify agent-done works.

        This tests the full integration path:
        1. AgentRunner invokes Codex
        2. Codex executes task
        3. Codex calls agent-done
        4. completion.json is written
        5. AgentRunner captures all output
        """
        from agent_runner import AgentRunner
        from agent_runner.ports import RunSpec
        from agent_runner.providers import CodexProvider

        # Get scripts directory
        repo_root = Path(__file__).parent.parent.parent
        scripts_dir = repo_root / "src" / "issue_orchestrator" / "scripts"

        # Set up worktree
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        subprocess.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=worktree, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=worktree, capture_output=True, check=True
        )
        (worktree / "README.md").write_text("# Test\n")
        subprocess.run(["git", "add", "."], cwd=worktree, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=worktree, capture_output=True, check=True
        )

        io_dir = worktree / ".issue-orchestrator"
        io_dir.mkdir()

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        provider = CodexProvider()
        prompt = (
            "You are in a test. Run this exact bash command:\n"
            "agent-done completed --implementation 'AgentRunner test' --problems 'none'\n"
            "Do not explain, just run it."
        )

        cmd = provider.build_command(
            prompt=prompt,
            approval_mode="yolo",  # Skip approvals for testing
            json_output="false",
        )

        runner = AgentRunner()
        spec = RunSpec(
            command=cmd,
            working_dir=worktree,
            timeout_seconds=180,
            output_dir=output_dir,
            env_overrides={
                "PATH": f"{scripts_dir}:{os.environ.get('PATH', '')}",
                "ORCHESTRATOR_COMPLETION_PATH": str(io_dir / "completion.json"),
            },
        )

        result = runner.run(spec)

        print(f"AgentRunner result:")
        print(f"  exit_code: {result.exit_code}")
        print(f"  timed_out: {result.timed_out}")
        print(f"  duration: {result.duration_seconds:.1f}s")
        print(f"  stdout: {result.stdout[:500] if result.stdout else 'empty'}")
        print(f"  stderr: {result.stderr[:500] if result.stderr else 'empty'}")

        # Verify log files exist and stderr has content (Codex writes to stderr)
        assert result.stdout_path.exists()
        assert result.stderr_path.exists()
        assert len(result.stderr) > 0, "Session log (stderr) should not be empty"
        assert "codex" in result.stderr.lower(), "Session log should contain Codex output"

        # Check for completion.json
        completion_files = list(io_dir.glob("completion*.json"))
        assert len(completion_files) > 0, (
            f"No completion.json written!\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}\n"
            f"Files in {io_dir}: {list(io_dir.iterdir())}"
        )

        # Validate completion record
        completion_path = completion_files[0]
        completion_data = json.loads(completion_path.read_text())
        assert completion_data.get("outcome") == "completed"
        assert "AgentRunner test" in completion_data.get("implementation", "")
