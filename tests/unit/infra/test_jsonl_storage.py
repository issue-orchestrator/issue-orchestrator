"""Public JSONL storage behavior at the real filesystem/syscall boundary."""

from __future__ import annotations

import errno
import fcntl
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from issue_orchestrator.infra.jsonl_storage import append_jsonl, read_jsonl_snapshot
from tests.process_group_run import run_in_process_group
from tests.unit.threading_helpers import join_or_fail, run_in_thread, wait_for_event


def test_snapshot_releases_lock_before_bulk_read_and_excludes_later_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "journal.jsonl"
    append_jsonl(path, {"row": 0})
    original = path.read_bytes()
    reading, release = threading.Event(), threading.Event()
    real_read = os.read

    def paused_read(fd: int, size: int) -> bytes:
        reading.set()
        wait_for_event(release, 10, label="release bulk read")
        # Short OS reads must make progress within the fixed byte budget.
        return real_read(fd, min(size, 3))

    monkeypatch.setattr(os, "read", paused_read)
    reader, result = run_in_thread(read_jsonl_snapshot, path)
    try:
        wait_for_event(reading, 10, label="bulk read started")
        append_jsonl(path, {"row": 1})
    finally:
        release.set()
        join_or_fail(reader, 10)
    assert result.unwrap() == original
    assert path.read_bytes() == original + b'{"row": 1}\n'


def test_shared_extent_capture_excludes_writer_but_allows_other_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "journal.jsonl"
    append_jsonl(path, {"row": 0})
    clock = iter([0.0, 5.0])
    with path.open("rb") as holder:
        fcntl.flock(holder, fcntl.LOCK_SH)
        assert read_jsonl_snapshot(path) == path.read_bytes()
        with monkeypatch.context() as patch:
            patch.setattr(time, "monotonic", lambda: next(clock))
            with pytest.raises(TimeoutError, match="publication lock"):
                append_jsonl(path, {"row": 1})
    append_jsonl(path, {"row": 2})
    assert path.read_bytes() == b'{"row": 0}\n{"row": 2}\n'


def test_lock_contention_uses_a_fixed_deadline_without_busy_spinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "journal.jsonl"
    append_jsonl(path, {"row": 0})
    clock = iter([0.0, 4.999, 5.0])
    waits: list[float] = []
    with path.open("rb") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with monkeypatch.context() as patch:
            patch.setattr(time, "monotonic", lambda: next(clock))
            patch.setattr(time, "sleep", waits.append)
            with pytest.raises(TimeoutError) as caught:
                read_jsonl_snapshot(path)
    assert caught.value.errno == errno.ETIMEDOUT
    assert waits == [pytest.approx(0.001)]


@pytest.mark.parametrize("operation", ["append", "snapshot"])
def test_non_contention_lock_errors_propagate_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    path = tmp_path / "journal.jsonl"
    append_jsonl(path, {"row": 0})
    failure = OSError(errno.EBADF, "flock failed")
    calls: list[int] = []

    def fail_lock(fd: int, flags: int) -> None:
        calls.append(flags)
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(fcntl, "flock", fail_lock)
        with pytest.raises(OSError) as caught:
            if operation == "append":
                append_jsonl(path, {"row": 1})
            else:
                read_jsonl_snapshot(path)
    assert caught.value is failure
    assert len(calls) == 1
    assert read_jsonl_snapshot(path) == b'{"row": 0}\n'


@pytest.mark.parametrize("syscall", ["fstat", "read"])
def test_snapshot_os_errors_after_open_are_not_mistaken_for_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, syscall: str,
) -> None:
    path = tmp_path / "journal.jsonl"
    append_jsonl(path, {"row": 0})
    failure = FileNotFoundError(errno.ENOENT, "IO failure after opening")

    def fail_io(*args: object) -> None:
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(os, syscall, fail_io)
        with pytest.raises(FileNotFoundError) as caught:
            read_jsonl_snapshot(path)
    assert caught.value is failure
    # Both the lock and descriptor were released even on failed extent capture.
    append_jsonl(path, {"row": 1})
    assert read_jsonl_snapshot(path) == b'{"row": 0}\n{"row": 1}\n'


def test_shortened_snapshot_fails_instead_of_returning_a_smaller_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "journal.jsonl"
    append_jsonl(path, {"row": 0})
    real_read = os.read

    def truncate_before_read(fd: int, size: int) -> bytes:
        path.write_bytes(b"")
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", truncate_before_read)
    with pytest.raises(OSError, match="shortened below") as caught:
        read_jsonl_snapshot(path)
    assert caught.value.errno == errno.EIO


def test_absent_snapshot_does_not_create_storage(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "journal.jsonl"
    assert read_jsonl_snapshot(path) == b""
    assert not path.parent.exists()


def test_process_exit_releases_publication_lock_without_python_cleanup(tmp_path: Path) -> None:
    """Exit inside os.write: neither context-manager nor finally cleanup runs."""
    path = tmp_path / "journal.jsonl"
    script = '''
import os
import sys
from pathlib import Path
from issue_orchestrator.infra.jsonl_storage import append_jsonl
real_write = os.write

def exit_during_write(fd, line):
    real_write(fd, line)
    os._exit(23)

os.write = exit_during_write
append_jsonl(Path(sys.argv[1]), {"row": 0})
'''
    result = run_in_process_group([sys.executable, "-c", script, str(path)], timeout=10)
    assert result.returncode == 23
    assert read_jsonl_snapshot(path) == b'{"row": 0}\n'
    append_jsonl(path, {"row": 1})
    assert read_jsonl_snapshot(path) == b'{"row": 0}\n{"row": 1}\n'
