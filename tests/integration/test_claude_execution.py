"""Integration tests for Claude Code execution.

These tests verify that we can actually execute Claude Code commands
and that the command escaping works correctly in real shells.
"""

import pytest
import subprocess
import shutil
from pathlib import Path


def is_claude_available() -> bool:
    """Check if claude CLI is available in PATH."""
    return shutil.which("claude") is not None


@pytest.mark.skipif(not is_claude_available(), reason="Claude CLI not available")
class TestClaudeExecution:
    """Integration tests that actually run Claude Code."""

    def test_claude_version(self):
        """Verify claude CLI is accessible and responds to --version."""
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "claude" in result.stdout.lower() or "Claude" in result.stdout

    def test_claude_simple_calculation(self):
        """Run Claude with a simple calculation task to verify execution works.

        This tests that:
        1. Claude can be invoked via subprocess
        2. The --print flag works for non-interactive output
        3. Claude can perform a simple task and return results
        """
        # Use --print for non-interactive single-turn execution
        result = subprocess.run(
            [
                "claude",
                "--print",  # Output response and exit (non-interactive)
                "What is 2 + 2? Reply with just the number.",
            ],
            capture_output=True,
            text=True,
            timeout=60,  # Give Claude time to respond
        )

        # Claude should exit successfully
        assert result.returncode == 0, f"Claude failed: {result.stderr}"

        # The output should contain "4"
        assert "4" in result.stdout, f"Expected '4' in output: {result.stdout}"

    def test_claude_command_with_single_quotes(self):
        """Test that commands with single quotes work correctly via zsh -l -c wrapper.

        This is the actual pattern used by the orchestrator.
        """
        # Build a command similar to what orchestrator generates
        inner_command = "claude --print 'Reply with just the word: hello'"

        # Escape single quotes for zsh wrapper (the fix we just made)
        escaped_command = inner_command.replace("'", "'\\''")

        # Wrap in zsh -l -c (like ITermSessionManager.create_session does)
        wrapped_command = f"zsh -l -c '{escaped_command}'"

        result = subprocess.run(
            ["bash", "-c", wrapped_command],
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, f"Command failed: {result.stderr}"
        assert "hello" in result.stdout.lower(), f"Expected 'hello' in output: {result.stdout}"


    def test_claude_read_file_with_bypass_permissions(self):
        """Test that Claude can read files when permissions are bypassed.

        This tests the scenario where orchestrator runs unattended and needs
        to read instruction files without waiting for permission prompts.
        """
        # Create a test file to read
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("# Test Instructions\n\nThis is a test file for Claude to read.\n")
            test_file = f.name

        try:
            result = subprocess.run(
                [
                    "claude",
                    "--print",
                    "--dangerously-skip-permissions",  # Bypass permission prompts
                    f"Read the file at {test_file} and tell me what the title is. Reply with just the title text.",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )

            assert result.returncode == 0, f"Claude failed: {result.stderr}"
            # Should find "Test Instructions" in the output
            assert "Test Instructions" in result.stdout or "test" in result.stdout.lower(), \
                f"Expected file content reference in output: {result.stdout}"
        finally:
            Path(test_file).unlink(missing_ok=True)

    def test_claude_read_from_specific_directory(self):
        """Test reading from a subdirectory like docs/10-ai/.

        This mimics what the orchestrator does when agents read their instructions.
        """
        import tempfile
        import os

        # Create a directory structure like docs/10-ai/
        with tempfile.TemporaryDirectory() as tmpdir:
            ai_dir = Path(tmpdir) / "docs" / "10-ai"
            ai_dir.mkdir(parents=True)

            # Create an instruction file
            instruction_file = ai_dir / "test-instructions.md"
            instruction_file.write_text("# Agent Instructions\n\nDo the thing.\n")

            result = subprocess.run(
                [
                    "claude",
                    "--print",
                    "--dangerously-skip-permissions",
                    f"Read {instruction_file} and confirm you can see 'Agent Instructions' in it. Reply YES or NO.",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=tmpdir,  # Run from the temp directory
            )

            assert result.returncode == 0, f"Claude failed: {result.stderr}"
            assert "YES" in result.stdout.upper() or "agent" in result.stdout.lower(), \
                f"Claude couldn't read the file: {result.stdout}"


class TestShellEscaping:
    """Test shell escaping without requiring Claude CLI."""

    def test_shell_single_quote_escaping(self):
        """Verify our single-quote escaping pattern works in shell."""
        # This is the pattern: replace ' with '\'' (POSIX standard)
        original = "echo 'hello world'"
        escaped = original.replace("'", "'\\''")
        wrapped = f"bash -c '{escaped}'"

        result = subprocess.run(
            ["bash", "-c", wrapped],
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert result.returncode == 0, f"Command failed: {result.stderr}"
        assert "hello world" in result.stdout

    def test_complex_quoting_pattern(self):
        """Test the exact quoting pattern used by orchestrator commands."""
        # Simulate the orchestrator command pattern
        command = "echo --flag 'value with spaces' 'another value'"
        escaped = command.replace("'", "'\\''")
        wrapped = f"bash -c 'cd /tmp && {escaped}'"

        result = subprocess.run(
            ["bash", "-c", wrapped],
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert result.returncode == 0
        assert "value with spaces" in result.stdout
        assert "another value" in result.stdout

    def test_nested_quotes_in_prompt(self):
        """Test quoting when the prompt itself contains quotes."""
        # Prompts may contain quotes like: "Fix the 'broken' feature"
        prompt = "This has 'single' and \"double\" quotes"
        # For shell safety, we escape single quotes
        escaped_prompt = prompt.replace("'", "'\\''")
        command = f"echo '{escaped_prompt}'"
        escaped = command.replace("'", "'\\''")
        wrapped = f"bash -c '{escaped}'"

        result = subprocess.run(
            ["bash", "-c", wrapped],
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert result.returncode == 0
        # The output should contain the original text (with quotes resolved)
        assert "single" in result.stdout or "double" in result.stdout
