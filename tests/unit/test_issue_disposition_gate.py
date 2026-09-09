"""Real contention and process lifetime proofs for the disposition gate."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from issue_orchestrator.adapters.issue_disposition_gate import (
    FileIssueDispositionMutationGate,
)
from issue_orchestrator.domain.issue_disposition_gate import (
    IssueDispositionGateStatus as Status,
)
from issue_orchestrator.ports.issue_disposition_gate import IssueDispositionMutationGate
from tests.process_group_run import signal_group


def acquire_once(gate: IssueDispositionMutationGate, issue: int = 7) -> Status:
    with gate.try_acquire("owner/repo", issue) as status:
        return status


def test_sibling_projection_excludes_new_admission_across_instances(
    tmp_path: Path,
) -> None:
    projection = FileIssueDispositionMutationGate(tmp_path)
    admission = FileIssueDispositionMutationGate(tmp_path)
    with projection.try_acquire("owner/repo", 7) as status:
        assert status is Status.ACQUIRED
        assert acquire_once(admission) is Status.BUSY
        assert acquire_once(projection) is Status.BUSY
        with admission.try_acquire("OWNER/REPO", 7) as alias_status:
            assert alias_status is Status.BUSY
        assert acquire_once(admission, issue=8) is Status.ACQUIRED
        with admission.try_acquire("another/repo", 7) as other_status:
            assert other_status is Status.ACQUIRED
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert (
                executor.submit(acquire_once, admission).result(timeout=5)
                is Status.BUSY
            )
    assert acquire_once(admission) is Status.ACQUIRED


def test_failed_projection_releases_gate_without_replacing_inode(
    tmp_path: Path,
) -> None:
    gate = FileIssueDispositionMutationGate(tmp_path)
    with pytest.raises(RuntimeError, match="label write failed"):
        with gate.try_acquire("owner/repo", 7) as status:
            assert status is Status.ACQUIRED
            (lock_file,) = (tmp_path / "issue-disposition-gates").iterdir()
            original = lock_file.stat().st_ino
            raise RuntimeError("label write failed")
    assert acquire_once(gate) is Status.ACQUIRED
    assert lock_file.stat().st_ino == original


@pytest.mark.parametrize("repo", ["", "owner", "/repo", "owner/", "a/b/c", " a/b"])
def test_invalid_repository_refused_before_filesystem_write(
    tmp_path: Path, repo: str
) -> None:
    with pytest.raises(ValueError, match="repo_slug"):
        with FileIssueDispositionMutationGate(tmp_path).try_acquire(repo, 7):
            pytest.fail("invalid repository acquired a gate")
    assert not (tmp_path / "issue-disposition-gates").exists()


@pytest.mark.parametrize("issue", [0, -1, True])
def test_invalid_issue_refused_before_filesystem_write(
    tmp_path: Path, issue: int
) -> None:
    with pytest.raises(ValueError, match="issue_number"):
        with FileIssueDispositionMutationGate(tmp_path).try_acquire(
            "owner/repo", issue
        ):
            pytest.fail("invalid issue acquired a gate")
    assert not (tmp_path / "issue-disposition-gates").exists()


def test_unreadable_state_is_failure_not_contention(tmp_path: Path) -> None:
    (tmp_path / "issue-disposition-gates").write_text("not a directory")
    with pytest.raises(FileExistsError):
        acquire_once(FileIssueDispositionMutationGate(tmp_path))


@contextmanager
def child_gate(
    state_dir: Path, *, descendant: str = "none"
) -> Iterator[subprocess.Popen[str]]:
    script = """
import os, signal, subprocess, sys
from pathlib import Path
from issue_orchestrator.adapters.issue_disposition_gate import FileIssueDispositionMutationGate
signal.alarm(25)
with FileIssueDispositionMutationGate(Path(sys.argv[1])).try_acquire('owner/repo', 7) as status:
    print(status.value, flush=True)
    sys.stdin.readline()
    if sys.argv[2] == 'fork':
        pid = os.fork()
        if pid == 0:
            signal.alarm(25)
            print('descendant ready', flush=True)
            sys.stdin.readline()
            os._exit(0)
        os._exit(0)
    if sys.argv[2] == 'exec':
        subprocess.Popen([sys.executable, '-c',
            "import signal,sys; signal.alarm(25); print('descendant ready',flush=True); sys.stdin.readline()"],
            close_fds=False)
        os._exit(0)
    os._exit(0)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(state_dir), descendant],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        signal_group(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)


@pytest.mark.parametrize("descendant", ["none", "fork", "exec"])
def test_dead_owner_releases_without_child_retaining_gate(
    tmp_path: Path, descendant: str
) -> None:
    gate = FileIssueDispositionMutationGate(tmp_path)
    with child_gate(tmp_path, descendant=descendant) as process:
        assert process.stdout is not None
        assert process.stdin is not None
        assert process.stdout.readline().strip() == "acquired"
        assert acquire_once(gate) is Status.BUSY
        process.stdin.write("exit owner\n")
        process.stdin.flush()
        if descendant != "none":
            assert process.stdout.readline().strip() == "descendant ready"
        # Observe death without reaping: keep the owned process-group identity
        # reserved for cleanup while its still-live child waits on our pipe.
        import os

        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
        assert acquire_once(gate) is Status.ACQUIRED
