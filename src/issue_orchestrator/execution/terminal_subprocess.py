"""Terminal plugin that runs agent sessions as subprocesses.

This provides a tmux-free execution option while still emitting a session log
per worktree for debugging and session health checks.
"""

from __future__ import annotations

import json
import logging
import os
import pty
import select
import signal
import subprocess
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..control.isolation import build_isolation_prefix
from ..infra.env import get_env
from ..infra.hooks.hookspec import hookimpl
from ..infra.repo_identity import state_dir
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
        self._db_path = self._state_dir / "subprocess_sessions.sqlite"
        self._legacy_dir = self._state_dir / "subprocess_sessions"
        self._legacy_index = self._state_dir / "subprocess_sessions.json"
        self._legacy_backup = self._legacy_index.with_suffix(".json.bak")
        self._ensure_db()
        self._migrate_legacy_if_needed()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

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
    """Terminal plugin that uses subprocesses instead of tmux."""

    def __init__(self) -> None:
        repo_root = Path(get_env("REPO_ROOT") or Path.cwd()).resolve()
        self._registry = _SubprocessRegistry(repo_root)
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._pty_masters: dict[str, int] = {}  # Store PTY master fds for stdin
        self._repo_root = repo_root
        allow_stdin_val = get_env("SUBPROCESS_ALLOW_STDIN") or ""
        self._allow_stdin = allow_stdin_val.lower() in {"1", "true", "yes"}

    def _session_name(self, session_id: int, session_name: Optional[str]) -> str:
        return session_name or f"issue-{session_id}"

    def _session_log_path(self, working_dir: Path, session_name: str) -> Path:
        session_output = FileSystemSessionOutput()
        run_dir = session_output.ensure_run_dir(working_dir, session_name)
        return run_dir / "session.log"

    def _build_process_command(self, command: str, working_dir: Path) -> str:
        """Build the full command with path and isolation prefix."""
        wrapper_dir = self._repo_root / "scripts"
        venv_bin = working_dir / ".venv" / "bin"
        path_prefix = f"{venv_bin}:{wrapper_dir}:{os.environ.get('PATH', '')}"
        isolation_prefix = build_isolation_prefix(working_dir, scrub_env=True, isolate_home=False)
        return f'cd "{working_dir}" && export PATH="{path_prefix}" && {isolation_prefix}{command}'

    def _setup_pty_master(self, session_name: str, master_fd: int) -> int:
        """Set up PTY master fd and return read fd."""
        read_fd = os.dup(master_fd)
        if self._allow_stdin:
            self._pty_masters[session_name] = master_fd
        else:
            os.close(master_fd)
        return read_fd

    def _drain_remaining_output(self, read_fd: int, log_file) -> None:
        """Drain any remaining output after process exits."""
        while True:
            ready, _, _ = select.select([read_fd], [], [], 0.1)
            if not ready:
                break
            data = os.read(read_fd, 4096)
            if not data:
                break
            log_file.write(data)

    def _start_output_copier(
        self, read_fd: int, log_path: Path, proc: subprocess.Popen[bytes]
    ) -> None:
        """Start a thread to copy PTY output to log file."""

        def _copy_output():
            try:
                with open(log_path, "ab", buffering=0) as log_file:
                    while True:
                        try:
                            ready, _, _ = select.select([read_fd], [], [], 1.0)
                            if ready:
                                data = os.read(read_fd, 4096)
                                if not data:
                                    break
                                log_file.write(data)
                            if proc.poll() is not None:
                                self._drain_remaining_output(read_fd, log_file)
                                break
                        except OSError:
                            break
            finally:
                try:
                    os.close(read_fd)
                except OSError:
                    pass

        thread = threading.Thread(target=_copy_output, daemon=True)
        thread.start()

    def _start_process(self, command: str, working_dir: Path, session_name: str) -> subprocess.Popen[bytes]:
        full_cmd = self._build_process_command(command, working_dir)
        log_path = self._session_log_path(working_dir, session_name)

        # Use PTY to force line-buffered output from the child process.
        master_fd, slave_fd = pty.openpty()

        proc = subprocess.Popen(
            ["/bin/bash", "-lc", full_cmd],
            cwd=str(working_dir),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)

        read_fd = self._setup_pty_master(session_name, master_fd)
        self._start_output_copier(read_fd, log_path, proc)

        self._processes[session_name] = proc
        return proc

    def _process_alive(self, pid: int, session_name: str | None = None) -> bool:
        proc = self._processes.get(session_name) if session_name else None
        if proc is not None:
            if proc.poll() is not None:
                return False
            return True
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _kill_process(self, pid: int) -> None:
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
        session_name: str | None = None,
    ) -> bool | None:
        name = self._session_name(session_id, session_name)
        worktree = Path(working_dir)
        if self.session_exists(session_id, name):
            return False

        proc = self._start_process(command, worktree, name)
        is_review = name.startswith("review-")
        tab_name = title or name
        if is_review:
            try:
                pr_num = int(name.replace("review-", ""))
                tab_name = f"Review PR #{pr_num}"
            except ValueError:
                tab_name = name

        record = _SessionRecord(
            session_name=name,
            issue_number=session_id,
            worktree_path=str(worktree.resolve()),
            pid=proc.pid,
            started_at=datetime.now().isoformat(),
            log_path=str(self._session_log_path(worktree, name)),
            tab_name=tab_name,
            is_review=is_review,
        )
        self._registry.upsert(record)
        return True

    @hookimpl
    def session_exists(self, session_id: int, session_name: str | None = None) -> bool | None:
        name = self._session_name(session_id, session_name)
        records = self._registry.load()
        record = records.get(name)
        if not record:
            return False
        if self._process_alive(record.pid, name):
            return True
        self._registry.remove(name)
        self._processes.pop(name, None)
        return False

    @hookimpl
    def session_exists_by_name(self, session_name: str) -> bool | None:
        return self.session_exists(0, session_name)

    @hookimpl
    def kill_session(self, session_id: int, session_name: str | None = None) -> bool | None:
        name = self._session_name(session_id, session_name)
        records = self._registry.load()
        record = records.get(name)
        if not record:
            return False
        self._kill_process(record.pid)
        self._registry.remove(name)
        self._processes.pop(name, None)
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
                self._registry.remove(record.session_name)
                self._processes.pop(record.session_name, None)
        return running

    @hookimpl
    def cleanup_idle_sessions(self) -> int | None:
        records = self._registry.load()
        cleaned = 0
        for record in list(records.values()):
            if not self._process_alive(record.pid, record.session_name):
                self._registry.remove(record.session_name)
                self._processes.pop(record.session_name, None)
                cleaned += 1
        return cleaned

    @hookimpl
    def get_session_output(self, session_id: int, lines: int, session_name: str | None = None) -> str | None:
        name = self._session_name(session_id, session_name)
        record = self._registry.load().get(name)
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
    def send_to_session(self, session_id: int, text: str, session_name: str | None = None) -> bool | None:
        if not self._allow_stdin:
            return False
        name = self._session_name(session_id, session_name)
        proc = self._processes.get(name)
        master_fd = self._pty_masters.get(name)
        if not proc or master_fd is None:
            return False
        try:
            os.write(master_fd, (text + "\n").encode())
            return True
        except OSError:
            return False

    @hookimpl
    def send_to_session_by_name(self, session_name: str, text: str) -> bool | None:
        return self.send_to_session(0, text, session_name)

    @hookimpl
    def focus_session(self, session_id: int, session_name: str | None = None) -> bool | None:
        return False

    @hookimpl
    def on_orchestrator_startup(self) -> None:
        logger.info("[subprocess] Terminal backend ready.")

    @hookimpl
    def on_orchestrator_shutdown(self) -> None:
        records = self._registry.load()
        for record in records.values():
            if self._process_alive(record.pid):
                self._kill_process(record.pid)
        # Close any open PTY master fds
        for master_fd in self._pty_masters.values():
            try:
                os.close(master_fd)
            except OSError:
                pass
        self._pty_masters.clear()
        self._registry.clear()

    @hookimpl
    def terminal_health_check(self) -> dict | None:
        return {
            "healthy": True,
            "server_running": True,
            "session_exists": bool(self._registry.load()),
            "error": None,
        }
