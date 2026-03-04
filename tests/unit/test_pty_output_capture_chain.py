"""PTY-realistic tests for the pexpect → setpgrp child → CleaningLogWriter chain.

These tests exercise the ACTUAL output capture path used in production:

    pexpect.spawn(PTY) → bash → subprocess.Popen(preexec_fn=os.setpgrp) → agent
                  ↓
    CleaningLogWriter.write(bytes) → ui-session.log

The original bug (#4057) was invisible to simulated scenario tests because
ScriptSessionRunner uses subprocess.run(capture_output=True) — a completely
different I/O path that bypasses pexpect and PTYs entirely. These tests use
a real PTY so that the difference between inherited-stdout and PIPE-captured
stdout actually matters.

The #4057 root cause was TWO interacting problems:
1. AgentRunner used subprocess.PIPE to capture stdout — diverting it from the PTY
2. AgentRunner used start_new_session=True (setsid) — unnecessary session isolation

The fix was:
1. Inherit stdout/stderr (no PIPE) so output flows through pexpect's PTY
2. Use preexec_fn=os.setpgrp for process group isolation without session change

If any test here fails, the production output capture chain is broken.
"""

from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

import pexpect
import pytest

from issue_orchestrator.infra.terminal_cleaning import CleaningLogWriter


def _wait_for_exit(child: pexpect.spawn, timeout: float = 10.0) -> None:
    """Wait for pexpect child to exit, draining output."""
    deadline = time.monotonic() + timeout
    while child.isalive():
        assert time.monotonic() < deadline, "child did not exit in time"
        try:
            child.read_nonblocking(size=4096, timeout=0.1)
        except pexpect.TIMEOUT:
            pass
        except pexpect.EOF:
            break
    # Drain any remaining buffered data after exit.
    try:
        child.read_nonblocking(size=4096, timeout=0.5)
    except (pexpect.TIMEOUT, pexpect.EOF):
        pass


def _shell_quote(script: str) -> str:
    """Quote a Python script for embedding in a bash -c command."""
    import shlex
    return shlex.quote(script)


@pytest.mark.xdist_group("pty")
class TestPtyOutputCaptureWithSetpgrp:
    """Setpgrp child with inherited stdout: output flows through PTY to log."""

    def test_setpgrp_child_output_reaches_session_log(self, tmp_path: Path) -> None:
        """The production path: pexpect spawns bash, bash runs a command that
        spawns a subprocess with setpgrp and inherited stdout.

        This is the regression test for #4057. If this fails, agent output
        will not reach ui-session.log in production.
        """
        log_path = tmp_path / "ui-session.log"
        log_writer = CleaningLogWriter(log_path)

        # Script that mimics AgentRunner: spawns a child with setpgrp,
        # child writes output, stdout is inherited (not captured with PIPE).
        agent_script = textwrap.dedent("""\
            import os, subprocess, sys
            proc = subprocess.Popen(
                [sys.executable, "-c", "print('AGENT_MARKER_SETPGRP')"],
                preexec_fn=os.setpgrp,
            )
            proc.wait()
        """)

        child = pexpect.spawn(
            "/bin/bash",
            ["-c", f'{sys.executable} -c {_shell_quote(agent_script)}'],
            logfile=log_writer,
            timeout=10,
        )

        _wait_for_exit(child)
        log_writer.close()

        content = log_path.read_text()
        assert "AGENT_MARKER_SETPGRP" in content, (
            f"setpgrp child output did not reach ui-session.log. "
            f"This means the production output capture chain is broken. "
            f"Log content: {content!r}"
        )

    def test_multiline_output_all_reaches_session_log(self, tmp_path: Path) -> None:
        """Multiple lines from a setpgrp child all survive the PTY chain."""
        log_path = tmp_path / "ui-session.log"
        log_writer = CleaningLogWriter(log_path)

        agent_script = textwrap.dedent("""\
            import os, subprocess, sys
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 "print('LINE_ONE_ALPHA'); print('LINE_TWO_BETA'); print('LINE_THREE_GAMMA')"],
                preexec_fn=os.setpgrp,
            )
            proc.wait()
        """)

        child = pexpect.spawn(
            "/bin/bash",
            ["-c", f'{sys.executable} -c {_shell_quote(agent_script)}'],
            logfile=log_writer,
            timeout=10,
        )

        _wait_for_exit(child)
        log_writer.close()

        content = log_path.read_text()
        for marker in ("LINE_ONE_ALPHA", "LINE_TWO_BETA", "LINE_THREE_GAMMA"):
            assert marker in content, (
                f"Missing {marker!r} in session log. Content: {content!r}"
            )

    def test_nested_subprocess_output_reaches_session_log(self, tmp_path: Path) -> None:
        """Grandchild output also flows through the PTY (same session)."""
        log_path = tmp_path / "ui-session.log"
        log_writer = CleaningLogWriter(log_path)

        # Write the grandchild script to a file to avoid shell quoting issues
        grandchild_script = tmp_path / "grandchild.py"
        grandchild_script.write_text(
            "print('GRANDCHILD_OUTPUT_VISIBLE')\n",
            encoding="utf-8",
        )

        child_script = tmp_path / "child.py"
        child_script.write_text(textwrap.dedent(f"""\
            import subprocess, sys
            proc = subprocess.Popen([sys.executable, {str(grandchild_script)!r}])
            proc.wait()
            print('CHILD_OUTPUT_VISIBLE')
        """), encoding="utf-8")

        agent_script = textwrap.dedent(f"""\
            import os, subprocess, sys
            proc = subprocess.Popen(
                [sys.executable, {str(child_script)!r}],
                preexec_fn=os.setpgrp,
            )
            proc.wait()
        """)

        child = pexpect.spawn(
            "/bin/bash",
            ["-c", f'{sys.executable} -c {_shell_quote(agent_script)}'],
            logfile=log_writer,
            timeout=10,
        )

        _wait_for_exit(child)
        log_writer.close()

        content = log_path.read_text()
        assert "GRANDCHILD_OUTPUT_VISIBLE" in content, (
            f"Grandchild output missing from PTY log. Content: {content!r}"
        )
        assert "CHILD_OUTPUT_VISIBLE" in content, (
            f"Child output missing from PTY log. Content: {content!r}"
        )


@pytest.mark.xdist_group("pty")
class TestPipePlusTeeReachesPty:
    """Verify that PIPE + tee relay restores PTY output passthrough.

    NOTE: The vendored AgentRunner no longer uses stdout=PIPE; it inherits
    stdout for real-time streaming. These tests verify the tee mechanism
    still works (used for stderr), and serve as documentation of why PIPE+tee
    was replaced by direct inheritance.
    """

    def test_pipe_with_tee_reaches_session_log(self, tmp_path: Path) -> None:
        """stdout=PIPE + write to sys.stdout.buffer → output reaches PTY log.

        This is the actual production pattern: capture stdout via PIPE for
        classification, then relay to sys.stdout.buffer so pexpect sees it.
        """
        log_path = tmp_path / "ui-session.log"
        log_writer = CleaningLogWriter(log_path)

        # Script that captures stdout via PIPE, then tees to sys.stdout.buffer
        agent_script = textwrap.dedent("""\
            import os, subprocess, sys, threading

            def _tee(source, dest, chunks):
                while True:
                    chunk = source.read(4096)
                    if not chunk:
                        break
                    dest.write(chunk)
                    dest.flush()
                    chunks.append(chunk)

            proc = subprocess.Popen(
                [sys.executable, "-c", "print('TEE_RELAY_MARKER')"],
                stdout=subprocess.PIPE,
                preexec_fn=os.setpgrp,
            )
            chunks = []
            t = threading.Thread(target=_tee, args=(proc.stdout, sys.stdout.buffer, chunks))
            t.start()
            proc.wait()
            t.join(timeout=5)
        """)

        child = pexpect.spawn(
            "/bin/bash",
            ["-c", f'{sys.executable} -c {_shell_quote(agent_script)}'],
            logfile=log_writer,
            timeout=10,
        )

        _wait_for_exit(child)
        log_writer.close()

        content = log_path.read_text()
        assert "TEE_RELAY_MARKER" in content, (
            f"PIPE+tee output did not reach PTY log. "
            f"This means the tee relay pattern is broken. "
            f"Log content: {content!r}"
        )


@pytest.mark.xdist_group("pty")
class TestPipeCaptureBreaksPtyPath:
    """Document that subprocess.PIPE WITHOUT tee diverts output away from the PTY.

    This is the core anti-pattern that caused #4057. When AgentRunner
    used subprocess.PIPE to capture stdout WITHOUT relaying it back,
    output went to the pipe instead of flowing through pexpect's PTY
    to CleaningLogWriter.
    """

    def test_pipe_capture_prevents_output_reaching_session_log(self, tmp_path: Path) -> None:
        """When a child uses stdout=PIPE, its output does NOT reach the PTY log.

        This proves that adding PIPE capture back would re-break the chain.
        """
        log_path = tmp_path / "ui-session.log"
        log_writer = CleaningLogWriter(log_path)

        # Script that uses PIPE — the old broken AgentRunner behavior.
        # The parent reads from the pipe, but that output never reaches the PTY.
        agent_script = textwrap.dedent("""\
            import os, subprocess, sys
            proc = subprocess.Popen(
                [sys.executable, "-c", "print('PIPE_CAPTURED_MARKER')"],
                stdout=subprocess.PIPE,
                preexec_fn=os.setpgrp,
            )
            out, _ = proc.communicate()
            # Output went to pipe, not to the PTY
        """)

        child = pexpect.spawn(
            "/bin/bash",
            ["-c", f'{sys.executable} -c {_shell_quote(agent_script)}'],
            logfile=log_writer,
            timeout=10,
        )

        _wait_for_exit(child)
        log_writer.close()

        content = log_path.read_text()
        assert "PIPE_CAPTURED_MARKER" not in content, (
            f"PIPE-captured output should NOT reach the PTY log. "
            f"If this fails, stdout=PIPE is leaking to the PTY. "
            f"Log content: {content!r}"
        )

    def test_inherited_vs_pipe_contrast(self, tmp_path: Path) -> None:
        """Side-by-side: inherited stdout reaches PTY, PIPE stdout does not.

        This is the definitive regression test. If someone re-adds PIPE
        capture to AgentRunner, this test catches the break.
        """
        # -- Inherited path (production, should capture) --
        inherited_log = tmp_path / "inherited.log"
        inherited_writer = CleaningLogWriter(inherited_log)

        inherited_script = textwrap.dedent("""\
            import os, subprocess, sys
            proc = subprocess.Popen(
                [sys.executable, "-c", "print('CONTRAST_MARKER')"],
                preexec_fn=os.setpgrp,
            )
            proc.wait()
        """)

        child = pexpect.spawn(
            "/bin/bash",
            ["-c", f'{sys.executable} -c {_shell_quote(inherited_script)}'],
            logfile=inherited_writer,
            timeout=10,
        )
        _wait_for_exit(child)
        inherited_writer.close()

        # -- PIPE path (old broken behavior, should NOT capture) --
        pipe_log = tmp_path / "pipe.log"
        pipe_writer = CleaningLogWriter(pipe_log)

        pipe_script = textwrap.dedent("""\
            import os, subprocess, sys
            proc = subprocess.Popen(
                [sys.executable, "-c", "print('CONTRAST_MARKER')"],
                stdout=subprocess.PIPE,
                preexec_fn=os.setpgrp,
            )
            proc.communicate()
        """)

        child = pexpect.spawn(
            "/bin/bash",
            ["-c", f'{sys.executable} -c {_shell_quote(pipe_script)}'],
            logfile=pipe_writer,
            timeout=10,
        )
        _wait_for_exit(child)
        pipe_writer.close()

        inherited_content = inherited_log.read_text()
        pipe_content = pipe_log.read_text()

        assert "CONTRAST_MARKER" in inherited_content, (
            f"Inherited stdout MUST reach PTY log. Content: {inherited_content!r}"
        )
        assert "CONTRAST_MARKER" not in pipe_content, (
            f"PIPE stdout must NOT reach PTY log (this was the #4057 bug). "
            f"Content: {pipe_content!r}"
        )


@pytest.mark.xdist_group("pty")
class TestAnsiCleaningThroughPty:
    """ANSI codes emitted by a setpgrp child are cleaned by CleaningLogWriter."""

    def test_ansi_codes_stripped_from_session_log(self, tmp_path: Path) -> None:
        """ANSI color codes in child output are stripped in the log file."""
        log_path = tmp_path / "ui-session.log"
        log_writer = CleaningLogWriter(log_path)

        agent_script = textwrap.dedent("""\
            import os, subprocess, sys
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 r"print('\\x1b[32mCOLORED_OUTPUT\\x1b[0m')"],
                preexec_fn=os.setpgrp,
            )
            proc.wait()
        """)

        child = pexpect.spawn(
            "/bin/bash",
            ["-c", f'{sys.executable} -c {_shell_quote(agent_script)}'],
            logfile=log_writer,
            timeout=10,
        )

        _wait_for_exit(child)
        log_writer.close()

        content = log_path.read_text()
        assert "COLORED_OUTPUT" in content, (
            f"Marker text missing after ANSI stripping. Content: {content!r}"
        )
        assert "\x1b" not in content, (
            f"ANSI escape sequences survived cleaning. Content: {content!r}"
        )


@pytest.mark.xdist_group("pty")
class TestSigttinPreventionWithDevnullStdin:
    """Regression tests for SIGTTIN prevention.

    When a Popen child uses preexec_fn=os.setpgrp inside a pexpect PTY,
    the child is in a background process group. If the child reads from
    the inherited PTY stdin, the kernel sends SIGTTIN and stops the process.

    The fix: stdin=subprocess.DEVNULL prevents any stdin read from triggering
    SIGTTIN. These tests verify output still flows correctly with DEVNULL.
    """

    def test_setpgrp_with_devnull_stdin_produces_output(self, tmp_path: Path) -> None:
        """Popen(setpgrp, stdin=DEVNULL) inside pexpect → output reaches log.

        This is the immediate fix for the SIGTTIN bug. The child gets its own
        process group (setpgrp) and stdin is /dev/null, so no SIGTTIN can occur.
        """
        log_path = tmp_path / "ui-session.log"
        log_writer = CleaningLogWriter(log_path)

        agent_script = textwrap.dedent("""\
            import os, subprocess, sys
            proc = subprocess.Popen(
                [sys.executable, "-c", "print('DEVNULL_STDIN_MARKER')"],
                stdin=subprocess.DEVNULL,
                preexec_fn=os.setpgrp,
            )
            proc.wait()
        """)

        child = pexpect.spawn(
            "/bin/bash",
            ["-c", f'{sys.executable} -c {_shell_quote(agent_script)}'],
            logfile=log_writer,
            timeout=10,
        )

        _wait_for_exit(child)
        log_writer.close()

        content = log_path.read_text()
        assert "DEVNULL_STDIN_MARKER" in content, (
            f"setpgrp + stdin=DEVNULL child output did not reach ui-session.log. "
            f"Log content: {content!r}"
        )

    def test_pipe_tee_with_devnull_stdin_reaches_session_log(self, tmp_path: Path) -> None:
        """PIPE+tee+DEVNULL+setpgrp → output reaches log.

        NOTE: This was the vendored AgentRunner's old pattern. Production now
        inherits stdout directly for real-time streaming. This test verifies
        that the tee mechanism works correctly (still used for stderr relay).
        """
        log_path = tmp_path / "ui-session.log"
        log_writer = CleaningLogWriter(log_path)

        agent_script = textwrap.dedent("""\
            import os, subprocess, sys, threading

            def _tee(source, dest, chunks):
                while True:
                    chunk = source.read(4096)
                    if not chunk:
                        break
                    dest.write(chunk)
                    dest.flush()
                    chunks.append(chunk)

            proc = subprocess.Popen(
                [sys.executable, "-c", "print('DEVNULL_TEE_MARKER')"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                preexec_fn=os.setpgrp,
            )
            chunks = []
            t = threading.Thread(target=_tee, args=(proc.stdout, sys.stdout.buffer, chunks))
            t.start()
            proc.wait()
            t.join(timeout=5)
        """)

        child = pexpect.spawn(
            "/bin/bash",
            ["-c", f'{sys.executable} -c {_shell_quote(agent_script)}'],
            logfile=log_writer,
            timeout=10,
        )

        _wait_for_exit(child)
        log_writer.close()

        content = log_path.read_text()
        assert "DEVNULL_TEE_MARKER" in content, (
            f"PIPE+tee+DEVNULL output did not reach PTY log. "
            f"Log content: {content!r}"
        )


@pytest.mark.xdist_group("pty")
class TestAgentRunnerConfigIntegration:
    """Verify AgentRunner's actual subprocess configuration is PTY-compatible.

    These tests import the real AgentRunner and verify its Popen configuration
    doesn't break the PTY output chain.
    """

    def test_agent_runner_inherits_stdout_tees_stderr(self) -> None:
        """Vendored AgentRunner inherits stdout and tees stderr to parent.

        Stdout inherits the parent PTY for real-time streaming to ui-session.log.
        Only stderr is captured via PIPE for provider error classification;
        a tee thread relays it to sys.stderr.buffer.
        """
        import inspect
        from issue_orchestrator._vendor.agent_runner.runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "capture_output=True" not in source, (
            "AgentRunner must not use capture_output=True"
        )
        assert "stdout=subprocess.PIPE" not in source, (
            "AgentRunner must NOT pipe stdout — inherit for real-time streaming"
        )
        assert "sys.stderr.buffer" in source, (
            "AgentRunner must tee stderr to sys.stderr.buffer for PTY passthrough"
        )
        assert "_tee_stream" in source, (
            "AgentRunner must use _tee_stream to relay stderr to parent"
        )

    def test_agent_runner_does_not_use_setsid(self) -> None:
        """AgentRunner must NOT use start_new_session=True.

        setpgrp (process group) is correct; setsid (new session) is overkill
        and loses the controlling terminal.
        """
        import inspect
        from issue_orchestrator._vendor.agent_runner.runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "start_new_session=True" not in source, (
            "AgentRunner must not use start_new_session=True — "
            "use preexec_fn=os.setpgrp instead (see #4057)"
        )

    def test_agent_runner_uses_agent_preexec(self) -> None:
        """AgentRunner must use preexec_fn=_agent_preexec for process group isolation
        and SIGTTIN/SIGTTOU immunity."""
        import inspect
        from issue_orchestrator._vendor.agent_runner.runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "_agent_preexec" in source, (
            "AgentRunner must use preexec_fn=_agent_preexec for process group "
            "isolation and SIGTTIN/SIGTTOU immunity"
        )

    def test_agent_runner_uses_devnull_stdin(self) -> None:
        """Vendored AgentRunner must use stdin=subprocess.DEVNULL.

        Without DEVNULL, setpgrp puts the child in a background process group
        and any stdin read from the inherited PTY triggers SIGTTIN → process
        stopped → no output. This is the root cause of the empty ui-session.log
        bug when running inside SubprocessPlugin's pexpect PTY.
        """
        import inspect
        from issue_orchestrator._vendor.agent_runner.runner import AgentRunner

        source = inspect.getsource(AgentRunner)
        assert "subprocess.DEVNULL" in source, (
            "AgentRunner must use stdin=subprocess.DEVNULL to prevent SIGTTIN. "
            "Without it, setpgrp + inherited PTY stdin → SIGTTIN → process stopped."
        )
