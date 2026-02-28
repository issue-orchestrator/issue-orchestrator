"""Architectural guardrails for the unified AgentRunner.

These tests enforce that ALL agent process spawning goes through the unified
AgentRunner in ``execution/agent_runner.py``.  Any code that bypasses it
risks creating an untested I/O path — the root cause of #4057.

Phase 1 known violations are files not yet migrated to AgentRunner.
They must shrink to empty in Phase 2.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"
TESTS_ROOT = Path(__file__).resolve().parents[2] / "tests"


# ---------------------------------------------------------------------------
# Phase 2 migration targets — files allowed to use pexpect.spawn directly.
# Remove entries here as each file is migrated to AgentRunner.
# ---------------------------------------------------------------------------
KNOWN_PEXPECT_SPAWN_VIOLATIONS = {
    "execution/terminal_subprocess.py",
    "execution/review_exchange_local_loop.py",
}


def _find_pexpect_spawn_calls(source_file: Path) -> list[int]:
    """Return line numbers where pexpect.spawn() is called (not type annotations)."""
    try:
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    except SyntaxError:
        return []

    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match pexpect.spawn(...)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "spawn"
            and isinstance(func.value, ast.Name)
            and func.value.id == "pexpect"
        ):
            lines.append(node.lineno)
    return lines


class TestOnlyAgentRunnerSpawnsAgents:
    """No code outside AgentRunner may call pexpect.spawn() to create agents."""

    def test_no_unauthorized_pexpect_spawn_in_src(self) -> None:
        """Scan src/ for pexpect.spawn() calls outside the allowed set."""
        allowed = {"execution/agent_runner.py"} | KNOWN_PEXPECT_SPAWN_VIOLATIONS
        violations: list[str] = []

        for py_file in SRC_ROOT.rglob("*.py"):
            rel = str(py_file.relative_to(SRC_ROOT))
            if rel in allowed:
                continue
            spawn_lines = _find_pexpect_spawn_calls(py_file)
            if spawn_lines:
                violations.append(f"{rel}: lines {spawn_lines}")

        assert not violations, (
            "Unauthorized pexpect.spawn() calls found outside AgentRunner.\n"
            "All agent process spawning must go through execution/agent_runner.py.\n"
            "Violations:\n" + "\n".join(f"  - {v}" for v in violations)
        )

    def test_known_violations_still_exist(self) -> None:
        """Each entry in KNOWN_PEXPECT_SPAWN_VIOLATIONS must actually use pexpect.spawn.

        If a violation is removed (migrated), remove it from the allowlist.
        Stale entries indicate the migration is done but the allowlist wasn't updated.
        """
        for rel_path in KNOWN_PEXPECT_SPAWN_VIOLATIONS:
            source_file = SRC_ROOT / rel_path
            assert source_file.exists(), (
                f"Known violation {rel_path!r} does not exist — "
                f"remove it from KNOWN_PEXPECT_SPAWN_VIOLATIONS"
            )
            spawn_lines = _find_pexpect_spawn_calls(source_file)
            assert spawn_lines, (
                f"{rel_path} no longer calls pexpect.spawn() — "
                f"remove it from KNOWN_PEXPECT_SPAWN_VIOLATIONS (migration complete!)"
            )


class TestScriptSessionRunnerUsesAgentRunner:
    """ScriptSessionRunner must delegate to AgentRunner, not subprocess.run."""

    def test_no_subprocess_run_in_script_session_runner(self) -> None:
        """ScriptSessionRunner must NOT use subprocess.run for session execution.

        If this fails, someone bypassed the unified AgentRunner, re-creating
        the divergent I/O path that caused #4057.
        """
        conftest = TESTS_ROOT / "simulated_scenarios" / "conftest.py"
        tree = ast.parse(conftest.read_text(encoding="utf-8"), filename=str(conftest))

        # Find the ScriptSessionRunner class
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != "ScriptSessionRunner":
                continue
            source_segment = ast.get_source_segment(
                conftest.read_text(encoding="utf-8"), node,
            )
            assert source_segment is not None
            assert "subprocess.run" not in source_segment, (
                "ScriptSessionRunner must not use subprocess.run — "
                "it must delegate to AgentRunner for PTY-realistic execution"
            )
            assert "capture_output" not in source_segment, (
                "ScriptSessionRunner must not use capture_output — "
                "output flows through AgentRunner's PTY to CleaningLogWriter"
            )
            return

        pytest.fail("ScriptSessionRunner class not found in conftest.py")


class TestAgentRunnerImplementation:
    """Verify the unified AgentRunner uses the correct mechanisms."""

    def test_uses_pexpect_not_subprocess(self) -> None:
        """AgentRunner must use pexpect (PTY), not raw subprocess."""
        from issue_orchestrator.execution.agent_runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "pexpect.spawn" in source, (
            "AgentRunner must use pexpect.spawn for PTY-based output capture"
        )
        assert "subprocess.Popen" not in source, (
            "AgentRunner must not use subprocess.Popen — use pexpect.spawn"
        )
        assert "subprocess.run" not in source, (
            "AgentRunner must not use subprocess.run — use pexpect.spawn"
        )

    def test_does_not_use_pipe_capture(self) -> None:
        """AgentRunner must NOT capture stdout/stderr with PIPE."""
        from issue_orchestrator.execution.agent_runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "subprocess.PIPE" not in source, (
            "AgentRunner must not use PIPE — output flows through PTY"
        )
        assert "capture_output" not in source, (
            "AgentRunner must not use capture_output — output flows through PTY"
        )

    def test_does_not_use_setsid(self) -> None:
        """AgentRunner must NOT use start_new_session=True (setsid).

        setpgrp (process group) is correct; setsid (new session) disconnects
        the controlling terminal, breaking PTY output (see #4057).
        """
        from issue_orchestrator.execution.agent_runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "start_new_session" not in source, (
            "AgentRunner must not use start_new_session — "
            "process group isolation via pexpect is sufficient"
        )
        assert "setsid" not in source, (
            "AgentRunner must not use setsid — "
            "process group isolation via pexpect is sufficient"
        )

    def test_uses_cleaning_log_writer(self) -> None:
        """AgentRunner must route output through CleaningLogWriter."""
        from issue_orchestrator.execution.agent_runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "CleaningLogWriter" in source, (
            "AgentRunner must use CleaningLogWriter for ANSI/spinner filtering"
        )

    def test_uses_filtered_env(self) -> None:
        """AgentRunner must filter the environment (credential scrubbing)."""
        from issue_orchestrator.execution.agent_runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "build_filtered_env" in source, (
            "AgentRunner must use build_filtered_env for security"
        )
