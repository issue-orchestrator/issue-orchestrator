"""Log artifact helpers for filesystem-backed session output."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..infra.terminal_recording import TERMINAL_RECORDING_FILENAME

logger = logging.getLogger(__name__)

TERMINAL_RECORDING_NAME = TERMINAL_RECORDING_FILENAME
LEGACY_UI_LOG_NAME = "ui-session.log"
PANE_LOG_NAME = "pane.log"
ORCHESTRATOR_TAIL_NAME = "orchestrator-tail.log"
CLAUDE_SESSION_PATH_NAME = "claude-session.path"
CLAUDE_SESSION_LOG_NAME = "claude-session.jsonl"

FindRunDir = Callable[[Path, str | None], Path | None]
ReadManifest = Callable[[Path], dict[str, Any] | None]
UpdateManifest = Callable[[Path, dict[str, Any]], None]
WriteText = Callable[[Path, str], None]
EnsureSymlink = Callable[[Path, Path], None]


class SessionLogArtifacts:
    """Owns run-scoped session log discovery and log enrichment artifacts."""

    def __init__(
        self,
        *,
        find_run_dir: FindRunDir,
        read_manifest: ReadManifest,
        update_manifest: UpdateManifest,
        write_text: WriteText,
        ensure_symlink: EnsureSymlink,
    ) -> None:
        self._find_run_dir = find_run_dir
        self._read_manifest = read_manifest
        self._update_manifest = update_manifest
        self._write_text = write_text
        self._ensure_symlink = ensure_symlink

    def get_log_path(
        self,
        worktree_path: Path,
        session_name: str,
    ) -> Path | None:
        """Get the canonical session artifact path for a session."""
        run_dir = self._find_run_dir(worktree_path, session_name)
        if not run_dir:
            return None
        return self._find_log_in_run_dir(run_dir)

    def get_log_path_for_run_dir(self, run_dir: Path) -> Path | None:
        """Get the canonical session artifact path for a known run directory."""
        return self._find_log_in_run_dir(run_dir)

    def attach_claude_log(self, run_dir: Path) -> Path | None:
        """Find and attach Claude log to run directory."""
        return self._attach_selected_claude_log(run_dir)

    def attach_claude_log_for_run(self, run_dir: Path) -> Path | None:
        """Attach the canonical Claude JSONL artifact for a run and return its path."""
        return self._attach_selected_claude_log(run_dir)

    def write_orchestrator_tail(  # noqa: C901
        self,
        run_dir: Path,
        log_path: Path,
        issue_number: int,
        session_name: str,
        max_lines: int = 400,
    ) -> Path | None:
        """Write filtered orchestrator log tail to run directory."""
        if not log_path.exists():
            return None

        run_id = None
        started_at = None
        manifest = self._read_manifest(run_dir)
        if manifest:
            run_id = manifest.get("run_id")
            started_at = manifest.get("started_at")

        try:
            lines = log_path.read_text(errors="ignore").splitlines()
        except OSError:
            return None

        if not lines:
            return None

        issue_patterns = (
            re.compile(rf"\[issue-{issue_number}\]"),
            re.compile(rf"\bissue={issue_number}\b"),
            re.compile(rf"\bissue_number={issue_number}\b"),
            re.compile(rf"\bissue_key=[^\s:]+:{issue_number}\b"),
            re.compile(rf"\bissue-{issue_number}\b"),
            re.compile(rf"/issues/{issue_number}\b"),
        )
        session_patterns = (
            re.compile(rf"\bsession={re.escape(session_name)}\b"),
            re.compile(rf"\bsession_id={re.escape(session_name)}\b"),
        )

        segment = lines
        found_marker = False
        if run_id:
            marker = f"run_id={run_id}"
            for idx in range(len(lines) - 1, -1, -1):
                if "SESSION_RUN_START" in lines[idx] and marker in lines[idx]:
                    segment = lines[idx:]
                    found_marker = True
                    break

        if not found_marker and started_at:
            segment = self._filter_lines_by_timestamp(lines, started_at)

        scoped = []
        for line in segment[-2000:]:
            if any(pattern.search(line) for pattern in session_patterns):
                scoped.append(line)
                continue
            if any(pattern.search(line) for pattern in issue_patterns):
                scoped.append(line)
        if not scoped:
            return None

        tail_lines = scoped[-max_lines:]
        tail_path = run_dir / ORCHESTRATOR_TAIL_NAME
        self._write_text(tail_path, "\n".join(tail_lines))
        self._update_manifest(run_dir, {"orchestrator_tail": str(tail_path)})
        return tail_path

    def ensure_claude_log_attached(
        self,
        worktree_path: Path,
        session_name: str | None = None,
    ) -> Path | None:
        """Ensure Claude log is attached to the run directory."""
        run_dir = self._find_run_dir(worktree_path, session_name)
        if not run_dir:
            return None

        manifest = self._read_manifest(run_dir) or {}
        existing = manifest.get("claude_log_path")
        if existing:
            return Path(existing)

        return self.attach_claude_log(run_dir)

    def find_latest_session_log_path(self, worktree_path: Path) -> Path | None:
        """Find the most recently updated session log in a worktree."""
        run_dir = self._find_run_dir(worktree_path, None)
        if not run_dir:
            return None
        return self._find_log_in_run_dir(run_dir)

    def _attach_selected_claude_log(self, run_dir: Path) -> Path | None:
        log_path, session_id = self._select_claude_log_for_run(run_dir)
        if not log_path:
            return None

        if not session_id:
            session_id = log_path.stem

        self._update_manifest(
            run_dir,
            {
                "claude_log_path": str(log_path),
                "claude_session_id": session_id,
            },
        )

        try:
            self._write_text(run_dir / CLAUDE_SESSION_PATH_NAME, str(log_path))
        except OSError:
            return log_path

        self._ensure_symlink(run_dir / CLAUDE_SESSION_LOG_NAME, log_path)
        return log_path

    def _select_claude_log_for_run(
        self, run_dir: Path
    ) -> tuple[Path | None, str | None]:
        manifest = self._read_manifest(run_dir) or {}
        claude_dir = manifest.get("claude_log_dir")
        if not claude_dir:
            return None, None

        log_dir = Path(claude_dir)
        if not log_dir.exists():
            return None, None

        candidates = list(log_dir.glob("*.jsonl"))
        if not candidates:
            return None, None

        started_at = manifest.get("started_at")
        parsed_candidates: list[tuple[Path, float, str | None]] = []

        for path in candidates:
            timestamp, session_id = self._read_claude_log_metadata(path)
            score = path.stat().st_mtime
            if timestamp:
                score = timestamp.timestamp()
            parsed_candidates.append((path, score, session_id))

        if started_at:
            try:
                started_dt = datetime.fromisoformat(started_at)
                started_ts = started_dt.timestamp()
                tolerance_s = 5.0
                after_start = [
                    (path, score - started_ts, session_id)
                    for path, score, session_id in parsed_candidates
                    if score - started_ts >= -tolerance_s
                ]
                if after_start:
                    after_start.sort(key=lambda item: item[1])
                    selected = after_start[0]
                    return selected[0], selected[2]
            except (ValueError, OSError):
                pass

        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return candidates[0], None

    @staticmethod
    def _read_claude_log_metadata(log_path: Path) -> tuple[datetime | None, str | None]:
        try:
            with log_path.open("r") as handle:
                for idx, line in enumerate(handle):
                    if idx > 50:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    timestamp = payload.get("timestamp")
                    session_id = payload.get("sessionId") or payload.get("session_id")
                    if timestamp:
                        parsed = SessionLogArtifacts._parse_iso_timestamp(timestamp)
                        return parsed, session_id
                    if session_id:
                        return None, session_id
        except OSError:
            return None, None
        return None, None

    @staticmethod
    def _parse_iso_timestamp(value: str) -> datetime | None:
        try:
            if value.endswith("Z"):
                value = value.replace("Z", "+00:00")
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _filter_lines_by_timestamp(lines: list[str], started_at: str) -> list[str]:
        """Filter log lines to only those after the given ISO timestamp."""
        try:
            start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, AttributeError):
            return lines

        timestamp_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

        result = []
        for line in lines:
            match = timestamp_pattern.match(line)
            if match:
                line_ts = match.group(1)
                if line_ts >= start_str:
                    result.append(line)
            elif result:
                result.append(line)

        return result if result else lines

    @staticmethod
    def _find_log_in_run_dir(run_dir: Path) -> Path | None:
        """Find the best log file in a run directory.

        Prefer the canonical terminal recording, falling back to legacy logs.
        """
        for filename in (TERMINAL_RECORDING_NAME, LEGACY_UI_LOG_NAME, PANE_LOG_NAME):
            candidate = run_dir / filename
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate

        return None
