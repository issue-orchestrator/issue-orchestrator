"""Same-host kernel gate; lock files persist so contenders share one inode.

The directory must be repository-owned local state, not a network filesystem.
Separate opens make even same-process/thread reentry non-reentrant. The small
descriptor registry is resource ownership only: it closes inherited copies on
fork without unlocking the parent's open file description. Close-on-exec also
covers subprocess launchers that do not run Python's fork callbacks.
"""

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import threading

from ..domain.issue_disposition_gate import IssueDispositionGateStatus as Status


class _GateDescriptors:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fds: set[int] = set()
        os.register_at_fork(
            before=self._lock.acquire,
            after_in_parent=self._lock.release,
            after_in_child=self._after_fork,
        )

    def _after_fork(self) -> None:
        for fd in self._fds:
            os.close(fd)
        self._fds.clear()
        self._lock.release()

    def open(self, path: Path) -> int:
        # Register atomically with respect to fork: there must be no untracked
        # inherited descriptor between open() and registry insertion.
        with self._lock:
            fd = os.open(
                path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
            self._fds.add(fd)
            return fd

    def close(self, fd: int, owner_pid: int) -> None:
        if os.getpid() != owner_pid:
            # The at-fork handler already closed this copy. Never close a new
            # child-owned resource that reused the old descriptor number.
            return
        with self._lock:
            try:
                os.close(fd)
            finally:
                self._fds.remove(fd)


_DESCRIPTORS = _GateDescriptors()


class FileIssueDispositionMutationGate:
    def __init__(self, state_dir: Path) -> None:
        self._directory = state_dir / "issue-disposition-gates"

    @contextmanager
    def try_acquire(self, repo_slug: str, issue_number: int) -> Iterator[Status]:
        """Return BUSY immediately on contention; other OS failures propagate."""
        owner, separator, repo = repo_slug.partition("/")
        if (
            not separator
            or not owner
            or not repo
            or "/" in repo
            or repo_slug.strip() != repo_slug
        ):
            raise ValueError("repo_slug must identify owner/repository")
        if type(issue_number) is not int or issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        self._directory.mkdir(parents=True, exist_ok=True)
        # GitHub repository names are case-insensitive. Hashing keeps untrusted
        # identities out of paths without introducing delimiter collisions.
        digest = hashlib.sha256(repo_slug.casefold().encode()).hexdigest()
        path = self._directory / f"{digest}-{issue_number}.lock"
        with hold_mutation_file(path) as status:
            yield status


@contextmanager
def hold_mutation_file(path: Path) -> Iterator[Status]:
    """One non-reentrant kernel lock; callers own namespacing and validation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    owner_pid = os.getpid()
    fd = _DESCRIPTORS.open(path)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("disposition gate must be a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield Status.BUSY
        else:
            yield Status.ACQUIRED
    finally:
        # Close releases only our description. Never unlink: replacing the
        # inode could let a second process enter around a waiting contender.
        _DESCRIPTORS.close(fd, owner_pid)
