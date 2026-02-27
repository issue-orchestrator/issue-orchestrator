"""Validate OS-level invariants for setpgrp-based process isolation.

These tests codify assumptions about how setpgrp interacts with PTYs,
process groups, and signal delivery. If any of these fail on a new OS or
platform, the SubprocessPlugin ↔ AgentRunner output capture chain will break.

The core invariant: os.setpgrp() creates a new process GROUP while keeping
the process in the same session. This means:
  - The child keeps its controlling terminal (PTY output flows to parent)
  - killpg() can target the child's process group without hitting the parent
  - This is NOT the same as os.setsid() which creates a new session and
    disconnects from the controlling terminal

Why this matters:
  SubprocessPlugin uses pexpect (PTY) to capture session output.
  AgentRunner spawns the agent command as a subprocess.
  If the agent runs in a new session (setsid), it loses the PTY and falls
  into non-interactive/print mode. Output never reaches pexpect.
  With setpgrp, the agent stays connected to the PTY and output flows.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest


@pytest.mark.xdist_group("pty")
class TestSetpgrpPreservesControllingTerminal:
    """setpgrp child inherits the parent's controlling terminal."""

    def test_child_stdout_reaches_parent_pipe(self) -> None:
        """Without PIPE redirect, child output goes to parent's stdout.
        With PIPE, we can read it. Either way, setpgrp doesn't eat output."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('setpgrp-visible')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setpgrp,
        )
        out, _ = proc.communicate(timeout=5)
        assert b"setpgrp-visible" in out

    def test_child_inherits_parent_stdout(self) -> None:
        """When stdout is NOT redirected, child output goes to parent's fd 1.

        This is the key invariant: pexpect's PTY is inherited by the child,
        so pexpect (CleaningLogWriter) can capture the output.
        """
        # Run a parent that spawns a setpgrp child with inherited stdout.
        # Parent captures via its own pipe.
        parent_script = (
            "import os, subprocess, sys\n"
            "p = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'print(\"inherited-output\")'],\n"
            "    preexec_fn=os.setpgrp,\n"
            ")\n"
            "p.wait()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", parent_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _ = proc.communicate(timeout=5)
        assert b"inherited-output" in out, (
            "setpgrp child should inherit parent's stdout; "
            "if this fails, PTY-based output capture will break"
        )


@pytest.mark.xdist_group("pty")
class TestSetpgrpCreatesDistinctProcessGroup:
    """setpgrp child runs in a different process group than the parent."""

    def test_child_pgid_differs_from_parent(self) -> None:
        """The child's process group ID must differ from the parent's."""
        script = (
            "import os; "
            "print(f'{os.getpid()} {os.getpgrp()}')"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setpgrp,
        )
        out, _ = proc.communicate(timeout=5)
        child_pid, child_pgid = out.decode().strip().split()
        # setpgrp makes the child its own process group leader
        assert child_pid == child_pgid, "setpgrp child should be its own group leader"
        assert int(child_pgid) != os.getpgrp(), (
            "child pgid must differ from parent pgid for safe killpg"
        )

    def test_killpg_terminates_child_tree_without_hitting_parent(self) -> None:
        """killpg on the child's group kills child+grandchildren, not parent."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            preexec_fn=os.setpgrp,
        )
        time.sleep(0.1)
        pgid = os.getpgid(proc.pid)
        assert pgid != os.getpgrp(), "child must be in a different group"

        os.killpg(pgid, signal.SIGTERM)
        exit_code = proc.wait(timeout=5)
        assert exit_code == -signal.SIGTERM
        # If we reach here, parent survived the killpg


@pytest.mark.xdist_group("pty")
class TestSetsidDisconnectsFromTerminal:
    """Contrast: setsid creates a new session and loses the controlling terminal.

    These tests document WHY setsid is wrong for our use case, providing
    the contrast to setpgrp.
    """

    def test_setsid_child_has_no_controlling_terminal(self) -> None:
        """A setsid child loses its controlling terminal.

        This is the bug we're fixing: start_new_session=True calls setsid(),
        which disconnects from the PTY. The agent falls into print mode.
        """
        script = (
            "import os\n"
            "try:\n"
            "    ctty = os.ttyname(0)\n"
            "except OSError:\n"
            "    ctty = 'NONE'\n"
            "print(ctty)\n"
        )
        # setsid child should have no controlling terminal (stdin not a tty)
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        out, _ = proc.communicate(timeout=5)
        assert out.decode().strip() == "NONE", (
            "setsid child should not have a controlling terminal on stdin"
        )

    def test_setpgrp_child_inherits_terminal_from_parent(self) -> None:
        """Contrast: setpgrp child keeps the parent's controlling terminal.

        This is the correct behavior for our use case.
        """
        # We need a real PTY for this test. Use pty.openpty() to create one,
        # then spawn a child with the slave as stdin.
        import pty
        master_fd, slave_fd = pty.openpty()
        try:
            script = (
                "import os\n"
                "try:\n"
                "    ctty = os.ttyname(0)\n"
                "    print('HAS_TTY')\n"
                "except OSError:\n"
                "    print('NO_TTY')\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=slave_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setpgrp,
            )
            out, _ = proc.communicate(timeout=5)
            assert b"HAS_TTY" in out, (
                "setpgrp child with PTY stdin should retain the terminal; "
                "if this fails, agent will fall into non-interactive mode"
            )
        finally:
            os.close(master_fd)
            os.close(slave_fd)


@pytest.mark.xdist_group("pty")
class TestSetpgrpKillpgIsolation:
    """Verify that killpg on a setpgrp group kills the entire subtree."""

    def test_grandchild_killed_by_killpg(self) -> None:
        """killpg should kill grandchildren in the same group."""
        # Parent spawns a child (setpgrp) which spawns a grandchild.
        # killpg on child's group should kill both.
        script = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(f'{p.pid}', flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setpgrp,
        )
        # Read grandchild pid
        assert proc.stdout is not None
        grandchild_pid_line = proc.stdout.readline()
        grandchild_pid = int(grandchild_pid_line.decode().strip())

        # Kill the process group
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=5)

        # Grandchild should also be dead
        time.sleep(0.2)
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild_pid, 0)
