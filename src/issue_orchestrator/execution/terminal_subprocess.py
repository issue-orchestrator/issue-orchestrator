"""Terminal plugin that runs agent sessions as subprocesses.

This provides a tmux-free execution option while still emitting a session log
per worktree for debugging and session health checks.

Uses pexpect for robust PTY handling. This solves the race condition where
fast-exiting processes (like printf) would exit before their output was fully
readable from the PTY buffer. pexpect correctly waits for all output before
signaling EOF, avoiding data loss under parallel test execution load.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from ..control.isolation import build_isolation_prefix

if TYPE_CHECKING:
    import pexpect
from ..infra.env import get_env
from ..infra.hooks.hookspec import hookimpl
from ..infra.repo_identity import state_dir
from ..infra.sqlite_connection import open_sqlite
from .session_output_adapter import FileSystemSessionOutput

logger = logging.getLogger(__name__)


@dataclass
class _SessionRecord:
    session_name: str
    issue_number: int
    worktree_path: str
    pid: int
    started_at: str
    log_path: str
    tab_name: str
    is_review: bool


class _SubprocessRegistry:
    """Persist subprocess sessions for restart discovery."""

    def __init__(self, repo_root: Path) -> None:
        self._state_dir = state_dir(repo_root)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._state_dir / "session_registry.sqlite"
        self._legacy_db_path = self._state_dir / "subprocess_sessions.sqlite"
        self._legacy_dir = self._state_dir / "subprocess_sessions"
        self._legacy_index = self._state_dir / "subprocess_sessions.json"
        self._legacy_backup = self._legacy_index.with_suffix(".json.bak")
        self._migrate_legacy_db_if_needed()
        self._ensure_db()
        self._migrate_legacy_if_needed()

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite(self._db_path)
        return open_sqlite(self._db_path)

    def _ensure_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_name TEXT PRIMARY KEY,
                    issue_number INTEGER NOT NULL,
                    worktree_path TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    log_path TEXT NOT NULL,
                    tab_name TEXT NOT NULL,
                    is_review INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def _migrate_legacy_db_if_needed(self) -> None:
        if not self._legacy_db_path.exists() or self._db_path.exists():
            return
        try:
            self._legacy_db_path.replace(self._db_path)
        except Exception as exc:
            logger.warning("Failed to migrate legacy session DB: %s", exc)

    def _migrate_legacy_if_needed(self) -> None:
        if self._db_path.exists() and self._has_rows():
            return
        legacy_records = self._load_legacy_records()
        if not legacy_records:
            return
        for record in legacy_records.values():
            self.upsert(record)

    def _has_rows(self) -> bool:
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT 1 FROM sessions LIMIT 1")
                return cur.fetchone() is not None
        except sqlite3.DatabaseError:
            self._handle_corrupt_db()
            return False

    def _handle_corrupt_db(self) -> None:
        corrupt_path = self._db_path.with_suffix(".sqlite.corrupt")
        try:
            if self._db_path.exists():
                self._db_path.replace(corrupt_path)
        except Exception:
            pass
        self._ensure_db()

    def _load_legacy_records(self) -> dict[str, _SessionRecord]:
        if self._legacy_index.exists() or self._legacy_backup.exists():
            for path in (self._legacy_index, self._legacy_backup):
                if not path.exists():
                    continue
                try:
                    raw = json.loads(path.read_text())
                except Exception:
                    continue
                records = self._records_from_payload(raw)
                if records:
                    return records
        if self._legacy_dir.exists():
            records: dict[str, _SessionRecord] = {}
            for path in sorted(self._legacy_dir.glob("*.json")):
                try:
                    raw = json.loads(path.read_text())
                    record = _SessionRecord(**raw)
                    records[record.session_name] = record
                except Exception:
                    continue
            return records
        return {}

    def _records_from_payload(self, raw: dict) -> dict[str, _SessionRecord]:
        records: dict[str, _SessionRecord] = {}
        for name, data in raw.items():
            try:
                records[name] = _SessionRecord(**data)
            except TypeError:
                continue
        return records

    def load(self) -> dict[str, _SessionRecord]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT session_name, issue_number, worktree_path, pid, started_at, log_path, tab_name, is_review "
                    "FROM sessions"
                )
                records = {}
                for row in cur.fetchall():
                    records[row[0]] = _SessionRecord(
                        session_name=row[0],
                        issue_number=row[1],
                        worktree_path=row[2],
                        pid=row[3],
                        started_at=row[4],
                        log_path=row[5],
                        tab_name=row[6],
                        is_review=bool(row[7]),
                    )
                return records
        except sqlite3.DatabaseError:
            logger.warning("Failed to read subprocess registry db: %s", self._db_path)
            self._handle_corrupt_db()
            return {}

    def upsert(self, record: _SessionRecord) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (session_name, issue_number, worktree_path, pid, started_at, log_path, tab_name, is_review)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_name) DO UPDATE SET
                        issue_number=excluded.issue_number,
                        worktree_path=excluded.worktree_path,
                        pid=excluded.pid,
                        started_at=excluded.started_at,
                        log_path=excluded.log_path,
                        tab_name=excluded.tab_name,
                        is_review=excluded.is_review
                    """,
                    (
                        record.session_name,
                        record.issue_number,
                        record.worktree_path,
                        record.pid,
                        record.started_at,
                        record.log_path,
                        record.tab_name,
                        1 if record.is_review else 0,
                    ),
                )
                conn.commit()
        except sqlite3.DatabaseError:
            self._handle_corrupt_db()

    def remove(self, session_name: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM sessions WHERE session_name = ?", (session_name,))
                conn.commit()
        except sqlite3.DatabaseError:
            self._handle_corrupt_db()

    def clear(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM sessions")
                conn.commit()
        except sqlite3.DatabaseError:
            self._handle_corrupt_db()


class SubprocessPlugin:
    """Terminal plugin that uses subprocesses instead of tmux.

    Uses pexpect for robust PTY handling. This ensures all output is captured
    even for fast-exiting processes, avoiding the race condition where processes
    exit before their output is readable from the PTY buffer.
    """

    def __init__(self) -> None:
        repo_root = Path(get_env("REPO_ROOT") or Path.cwd()).resolve()
        self._registry = _SubprocessRegistry(repo_root)
        self._children: dict[str, "pexpect.spawn"] = {}  # pexpect child processes
        self._watcher_threads: dict[str, threading.Thread] = {}  # Process watchers
        self._log_files: dict[str, BinaryIO] = {}  # Open log file handles
        allow_stdin_val = get_env("SUBPROCESS_ALLOW_STDIN") or ""
        self._allow_stdin = allow_stdin_val.lower() in {"1", "true", "yes"}

    def _session_log_path(self, working_dir: Path, session_name: str) -> Path:
        session_output = FileSystemSessionOutput()
        run_dir = session_output.ensure_run_dir(working_dir, session_name)
        return run_dir / "session.log"

    def _build_process_command(self, command: str, working_dir: Path) -> str:
        """Build the full command with path and isolation prefix."""
        # Use package-relative path so the orchestrator's own scripts dir is
        # found even when the target repo is a foreign (non-orchestrator) repo.
        wrapper_dir = Path(__file__).resolve().parents[1] / "scripts"
        venv_bin = working_dir / ".venv" / "bin"
        path_prefix = f"{venv_bin}:{wrapper_dir}:{os.environ.get('PATH', '')}"
        isolation_prefix = build_isolation_prefix(working_dir, scrub_env=True, isolate_home=False)
        return f'cd "{working_dir}" && export PATH="{path_prefix}" && {isolation_prefix}{command}'

    def _start_process_watcher(self, child: "pexpect.spawn", session_name: str, log_file: BinaryIO) -> None:
        """Start a thread that waits for the process to complete and closes resources."""
        import pexpect as pexp  # Lazy import to avoid circular dependency with agent-done

        def _watch():
            try:
                # Wait for EOF - pexpect guarantees all output is read before returning
                child.expect(pexp.EOF, timeout=None)
            except pexp.TIMEOUT:
                pass
            except pexp.ExceptionPexpect:
                pass
            finally:
                # Close child and flush log
                try:
                    child.close()
                except Exception:
                    pass
                try:
                    log_file.close()
                except Exception:
                    pass
                self._log_files.pop(session_name, None)

        thread = threading.Thread(target=_watch, daemon=True)
        self._watcher_threads[session_name] = thread
        thread.start()

    def _start_process(self, command: str, working_dir: Path, session_name: str) -> "pexpect.spawn":
        """Start a subprocess with pexpect, capturing output to log file.

        Uses pexpect.spawn which creates a PTY. This is required for interactive
        programs like Claude that behave differently without a TTY.

        Note: Python 3.14+ warns about forkpty() in multi-threaded processes.
        Tests using this code are grouped in xdist_group("pty") to run sequentially,
        and the warning is suppressed in pyproject.toml filterwarnings.
        """
        import pexpect

        full_cmd = self._build_process_command(command, working_dir)
        log_path = self._session_log_path(working_dir, session_name)

        # Open log file for binary writing (pexpect writes bytes)
        log_file = open(log_path, "wb", buffering=0)
        self._log_files[session_name] = log_file

        # Use pexpect.spawn which handles PTY correctly, including EOF timing
        # The logfile parameter captures all output automatically
        child = pexpect.spawn(
            "/bin/bash",
            ["-lc", full_cmd],
            cwd=str(working_dir),
            logfile=log_file,
            timeout=None,  # No timeout - sessions run until completion
        )

        self._children[session_name] = child
        self._start_process_watcher(child, session_name, log_file)
        return child

    def _process_alive(self, pid: int, session_name: str | None = None) -> bool:
        """Check if a process is still running."""
        child = self._children.get(session_name) if session_name else None
        if child is not None:
            try:
                return child.isalive()
            except (ChildProcessError, OSError):
                # ptyprocess can race with waitpid() under parallel load
                # and raise when the child has already been reaped.
                return False
        # Fall back to kill(0) check for processes we don't track directly
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _kill_process(self, pid: int, session_name: str | None = None) -> None:
        """Kill a process, trying process group first."""
        child = self._children.get(session_name) if session_name else None
        if child is not None:
            try:
                child.terminate(force=True)
                return
            except Exception:
                pass
        # Fall back to manual kill
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                return

    @hookimpl
    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool | None:
        logger.info(
            "[subprocess] create_session called: session_id=%s session_name=%r",
            session_id,
            session_name,
        )
        worktree = Path(working_dir)
        if self.session_exists(session_id, session_name):
            return False

        child = self._start_process(command, worktree, session_name)
        is_review = session_name.startswith("review-")
        tab_name = title or session_name
        if is_review:
            try:
                pr_num = int(session_name.replace("review-", ""))
                tab_name = f"Review PR #{pr_num}"
            except ValueError:
                tab_name = session_name

        # pexpect.spawn.pid is None only before spawn() completes, which can't
        # happen here since we just created the child
        assert child.pid is not None, "pexpect child has no pid"
        record = _SessionRecord(
            session_name=session_name,
            issue_number=session_id,
            worktree_path=str(worktree.resolve()),
            pid=child.pid,
            started_at=datetime.now().isoformat(),
            log_path=str(self._session_log_path(worktree, session_name)),
            tab_name=tab_name,
            is_review=is_review,
        )
        self._registry.upsert(record)
        return True

    def _wait_for_watcher_thread(self, session_name: str, timeout: float = 2.0) -> None:
        """Wait for the watcher thread to finish and clean up resources."""
        thread = self._watcher_threads.get(session_name)
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._watcher_threads.pop(session_name, None)

    def _cleanup_session(self, session_name: str) -> None:
        """Clean up all resources for a session."""
        self._wait_for_watcher_thread(session_name)
        self._children.pop(session_name, None)
        # Close log file if still open
        log_file = self._log_files.pop(session_name, None)
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass

    @hookimpl
    def session_exists(self, session_id: int, session_name: str) -> bool | None:
        records = self._registry.load()
        record = records.get(session_name)
        if not record:
            return False
        if self._process_alive(record.pid, session_name):
            return True
        # Process is dead - wait for watcher thread to finish flushing output
        self._cleanup_session(session_name)
        self._registry.remove(session_name)
        return False

    @hookimpl
    def session_exists_by_name(self, session_name: str) -> bool | None:
        return self.session_exists(0, session_name)

    @hookimpl
    def kill_session(self, session_id: int, session_name: str) -> bool | None:
        records = self._registry.load()
        record = records.get(session_name)
        if not record:
            return False
        self._kill_process(record.pid, session_name)
        self._cleanup_session(session_name)
        self._registry.remove(session_name)
        return True

    @hookimpl
    def discover_running_sessions(self) -> list[dict] | None:
        records = self._registry.load()
        running: list[dict] = []
        for record in records.values():
            if self._process_alive(record.pid, record.session_name):
                running.append({
                    "issue_number": record.issue_number,
                    "tab_name": record.tab_name,
                    "is_review": record.is_review,
                })
            else:
                self._cleanup_session(record.session_name)
                self._registry.remove(record.session_name)
        return running

    @hookimpl
    def cleanup_idle_sessions(self) -> int | None:
        records = self._registry.load()
        cleaned = 0
        for record in list(records.values()):
            if not self._process_alive(record.pid, record.session_name):
                self._cleanup_session(record.session_name)
                self._registry.remove(record.session_name)
                cleaned += 1
        return cleaned

    @hookimpl
    def get_session_output(self, session_id: int, lines: int, session_name: str) -> str | None:
        record = self._registry.load().get(session_name)
        if not record:
            return None
        log_path = Path(record.log_path)
        if not log_path.exists():
            return ""
        try:
            content = log_path.read_text()
        except Exception:
            return ""
        output_lines = content.splitlines()
        return "\n".join(output_lines[-lines:]) if output_lines else ""

    @hookimpl
    def send_to_session(self, session_id: int, text: str, session_name: str) -> bool | None:
        if not self._allow_stdin:
            return False
        child = self._children.get(session_name)
        if not child:
            return False
        try:
            child.sendline(text)
            return True
        except Exception:
            if not self._process_alive(child.pid or 0, session_name):
                return False
            try:
                child.sendline(text)
                return True
            except Exception:
                return False

    @hookimpl
    def send_to_session_by_name(self, session_name: str, text: str) -> bool | None:
        return self.send_to_session(0, text, session_name)

    @hookimpl
    def focus_session(self, session_id: int, session_name: str) -> bool | None:
        return False

    @hookimpl
    def on_orchestrator_startup(self) -> None:
        logger.info("[subprocess] Terminal backend ready (pexpect).")

    @hookimpl
    def on_orchestrator_shutdown(self) -> None:
        records = self._registry.load()
        for record in records.values():
            if self._process_alive(record.pid, record.session_name):
                self._kill_process(record.pid, record.session_name)
        # Wait for all watcher threads to finish
        for session_name in list(self._watcher_threads.keys()):
            self._wait_for_watcher_thread(session_name, timeout=1.0)
        # Close any remaining log files
        for log_file in list(self._log_files.values()):
            try:
                log_file.close()
            except Exception:
                pass
        self._log_files.clear()
        self._children.clear()
        self._registry.clear()

    @hookimpl
    def terminal_health_check(self) -> dict | None:
        return {
            "healthy": True,
            "server_running": True,
            "session_exists": bool(self._registry.load()),
            "error": None,
        }
