"""Filesystem-backed session output adapter.

Implements the SessionOutput port for local filesystem storage.
All session artifacts are stored in:

    <worktree>/.issue-orchestrator/sessions/<run_id>__<session_name>/
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..ports.session_output import (
    ReviewExchangeSummary,
    SessionRun,
    ValidationRecord,
    ValidationState,
)

logger = logging.getLogger(__name__)

# Directory and file names
SESSION_OUTPUT_DIR = "sessions"
SESSION_LOG_NAME = "session.log"
PANE_LOG_NAME = "pane.log"
MANIFEST_NAME = "manifest.json"
LATEST_NAME = "latest.json"
INDEX_NAME = "index.json"
ROOT_LATEST_NAME = "session-latest.json"
ORCHESTRATOR_TAIL_NAME = "orchestrator-tail.log"
CLAUDE_SESSION_PATH_NAME = "claude-session.path"
CLAUDE_SESSION_LOG_NAME = "claude-session.jsonl"

# Validation artifact names
VALIDATION_RECORD_NAME = "validation-record.json"
VALIDATION_STDOUT_NAME = "validation-stdout.log"
VALIDATION_STDERR_NAME = "validation-stderr.log"
VALIDATION_STATE_NAME = "validation-state.json"
VALIDATION_ERRORS_NAME = "validation-errors.txt"
RETRY_PROMPT_NAME = "retry-prompt.md"

# Other artifact names
WORKTREE_NOTE_NAME = "worktree.json"
SESSION_IDENTITY_NAME = "session-identity.json"

# Review exchange artifacts
REVIEW_EXCHANGE_DIR_NAME = "review-exchange"
REVIEW_EXCHANGE_SUMMARY_NAME = "summary.json"


class FileSystemSessionOutput:
    """Filesystem-backed implementation of SessionOutput port.

    All artifacts for a session are stored in a single run directory:
        <worktree>/.issue-orchestrator/sessions/<run_id>__<session_name>/
    """

    # -------------------------------------------------------------------------
    # Run Lifecycle
    # -------------------------------------------------------------------------

    def sessions_base_dir(self, worktree_path: Path) -> Path:
        """Get the base sessions directory for a worktree."""
        return worktree_path / ".issue-orchestrator" / SESSION_OUTPUT_DIR

    def start_run(
        self,
        worktree_path: Path,
        session_name: str,
        issue_number: int | None = None,
        agent_label: str | None = None,
        backend: str | None = None,
        claude_log_dir: str | None = None,
        orchestrator_log: str | None = None,
        completion_path: str | None = None,
    ) -> SessionRun:
        """Create a new run directory and initial manifest."""
        run_id = self._run_timestamp()
        base_dir = self._ensure_base_dir(worktree_path)
        run_dir = base_dir / self._run_dir_name(session_name, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        # Create symlink to latest run
        symlink_path = base_dir / session_name
        self._ensure_symlink(symlink_path, run_dir)

        log_path = run_dir / SESSION_LOG_NAME
        manifest_path = run_dir / MANIFEST_NAME
        started_at = datetime.now(timezone.utc).isoformat()

        manifest = {
            "session_name": session_name,
            "run_id": run_id,
            "started_at": started_at,
            "issue_number": issue_number,
            "agent_label": agent_label,
            "backend": backend,
            "worktree": str(worktree_path),
            "run_dir": str(run_dir),
            "log_path": str(log_path),
            "claude_log_dir": claude_log_dir,
            "orchestrator_log": orchestrator_log,
            "completion_path": completion_path,
            "diagnostic_path": None,
        }
        self._write_json(manifest_path, manifest)

        if claude_log_dir:
            self._write_text(run_dir / "claude-log.path", claude_log_dir)
        if orchestrator_log:
            self._write_text(run_dir / "orchestrator-log.path", orchestrator_log)

        self._update_latest(worktree_path, manifest)
        self._append_index(worktree_path, manifest)

        return SessionRun(
            session_name=session_name,
            run_id=run_id,
            run_dir=run_dir,
            log_path=log_path,
            manifest_path=manifest_path,
            started_at=started_at,
        )

    def find_run_dir(
        self,
        worktree_path: Path,
        session_name: str | None = None,
    ) -> Path | None:
        """Find the latest run directory for a session."""
        base_dir = self.sessions_base_dir(worktree_path)
        if not base_dir.exists():
            return None

        if session_name:
            # Check for legacy non-timestamped directory
            legacy = base_dir / session_name
            if legacy.exists() and legacy.is_dir() and not legacy.is_symlink():
                return legacy

            # Find timestamped directories matching session name
            candidates = sorted(
                [
                    d
                    for d in base_dir.iterdir()
                    if d.is_dir() and d.name.endswith(f"__{session_name}")
                ],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            return candidates[0] if candidates else None

        # No session name - find most recent run
        latest = self._load_latest(worktree_path)
        if latest:
            run_dir = Path(latest["run_dir"])
            if run_dir.exists():
                return run_dir

        candidates = sorted(
            [d for d in base_dir.iterdir() if d.is_dir() and not d.is_symlink()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def ensure_run_dir(
        self,
        worktree_path: Path,
        session_name: str,
    ) -> Path:
        """Get existing run dir or create a minimal one."""
        existing = self.find_run_dir(worktree_path, session_name=session_name)
        if existing:
            return existing
        run = self.start_run(worktree_path, session_name)
        return run.run_dir

    def prune_runs(
        self,
        worktree_path: Path,
        keep: int,
    ) -> list[Path]:
        """Delete old runs, keeping the last N."""
        if keep <= 0:
            return []

        base_dir = self.sessions_base_dir(worktree_path)
        if not base_dir.exists():
            return []

        runs = sorted(
            [d for d in base_dir.iterdir() if d.is_dir() and not d.is_symlink()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        removed: list[Path] = []
        for run_dir in runs[keep:]:
            try:
                self._delete_tree(run_dir)
                removed.append(run_dir)
            except OSError:
                continue

        if removed:
            self._prune_index(worktree_path, removed)
            self._refresh_latest(worktree_path)

        return removed

    def list_runs(
        self,
        worktree_path: Path,
    ) -> list[dict[str, Any]]:
        """List all runs in a worktree, sorted by start time (oldest first).

        Returns a list of run summaries from the index, each containing:
        - session_name: The phase name (e.g., "coding-1", "review-1")
        - run_id: Timestamp identifier
        - started_at: ISO timestamp
        - issue_number: Associated issue
        - run_dir: Path to run directory
        - agent_label: Agent type used
        - status: Derived status (completed, in_progress, failed)
        """
        index_path = self.sessions_base_dir(worktree_path) / INDEX_NAME
        index = self._read_json(index_path)
        if not index or "runs" not in index:
            return []

        runs = []
        for run_info in index["runs"]:
            run_dir = run_info.get("run_dir")
            if run_dir and Path(run_dir).exists():
                # Enhance with status from manifest
                manifest = self.read_manifest(Path(run_dir))
                if manifest:
                    run_info = {**run_info}  # Copy to avoid mutating index
                    run_info["status"] = self._derive_run_status(manifest, Path(run_dir))
                    run_info["ended_at"] = manifest.get("ended_at")
                    run_info["outcome"] = manifest.get("outcome")
                    run_info["validation_passed"] = manifest.get("validation_passed")
                runs.append(run_info)

        # Sort by started_at (oldest first for linear display)
        runs.sort(key=lambda r: r.get("started_at", ""))
        return runs

    def _derive_run_status(self, manifest: dict[str, Any], run_dir: Path) -> str:
        """Derive the status of a run from its manifest and artifacts."""
        if manifest.get("ended_at"):
            outcome = manifest.get("outcome", "unknown")
            if outcome in ("completed", "blocked", "timeout"):
                validation = manifest.get("validation_passed")
                if validation is False:
                    return "validation_failed"
                return outcome
            return outcome
        # Check if session is still running (no completion)
        completion_path = manifest.get("completion_path")
        if completion_path:
            full_path = run_dir.parent.parent / completion_path
            if full_path.exists():
                return "completed"
        return "in_progress"

    # -------------------------------------------------------------------------
    # Manifest
    # -------------------------------------------------------------------------

    def update_manifest(
        self,
        run_dir: Path,
        updates: dict[str, Any],
    ) -> None:
        """Update the manifest with additional data."""
        manifest_path = run_dir / MANIFEST_NAME
        manifest = self._read_json(manifest_path) or {}
        manifest.update(updates)
        self._write_json(manifest_path, manifest)

    def read_manifest(
        self,
        run_dir: Path,
    ) -> dict[str, Any] | None:
        """Read the manifest from a run directory."""
        return self._read_json(run_dir / MANIFEST_NAME)

    # -------------------------------------------------------------------------
    # Validation Artifacts
    # -------------------------------------------------------------------------

    def write_validation_record(
        self,
        run_dir: Path,
        record: ValidationRecord,
    ) -> Path:
        """Write a validation record to the run directory."""
        record_path = run_dir / VALIDATION_RECORD_NAME
        self._write_json(record_path, record.to_dict())
        self.update_manifest(run_dir, {"validation_record_path": str(record_path)})
        logger.debug("Wrote validation record to %s", record_path)
        return record_path

    def read_validation_record(
        self,
        run_dir: Path,
    ) -> ValidationRecord | None:
        """Read validation record from run directory."""
        record_path = run_dir / VALIDATION_RECORD_NAME
        data = self._read_json(record_path)
        if not data:
            return None
        try:
            return ValidationRecord.from_dict(data)
        except (KeyError, TypeError) as e:
            logger.warning("Failed to parse validation record at %s: %s", record_path, e)
            return None

    def write_validation_output(
        self,
        run_dir: Path,
        stdout: str,
        stderr: str,
    ) -> tuple[Path, Path]:
        """Write validation stdout/stderr to run directory."""
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / VALIDATION_STDOUT_NAME
        stderr_path = run_dir / VALIDATION_STDERR_NAME

        self._write_text(stdout_path, stdout)
        self._write_text(stderr_path, stderr)

        self.update_manifest(
            run_dir,
            {
                "validation_stdout": str(stdout_path),
                "validation_stderr": str(stderr_path),
            },
        )

        return stdout_path, stderr_path

    # -------------------------------------------------------------------------
    # Retry State
    # -------------------------------------------------------------------------

    def write_validation_state(
        self,
        run_dir: Path,
        state: ValidationState,
    ) -> Path:
        """Write validation retry state to run directory."""
        state_path = run_dir / VALIDATION_STATE_NAME

        data = asdict(state)
        if not data.get("created_at"):
            data["created_at"] = self._now_iso()
        data["updated_at"] = self._now_iso()

        self._write_json(state_path, data)
        logger.info(
            "Wrote validation state to %s (retry_count=%d)", state_path, state.retry_count
        )
        return state_path

    def read_validation_state(
        self,
        run_dir: Path,
    ) -> ValidationState | None:
        """Read validation retry state from run directory."""
        state_path = run_dir / VALIDATION_STATE_NAME
        data = self._read_json(state_path)
        if not data:
            return None

        return ValidationState(
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            validation_cmd=data.get("validation_cmd"),
            last_error=data.get("last_error"),
            last_error_file=data.get("last_error_file"),
            original_prompt_file=data.get("original_prompt_file"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )

    def write_validation_errors(
        self,
        run_dir: Path,
        validation_cmd: str,
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> Path:
        """Write human-readable validation errors to run directory."""
        errors_path = run_dir / VALIDATION_ERRORS_NAME

        content = f"""=== VALIDATION FAILED ===
Command: {validation_cmd}
Exit code: {exit_code}
Timestamp: {self._now_iso()}

=== STDERR ===
{stderr}

=== STDOUT ===
{stdout}
"""
        self._write_text(errors_path, content)
        logger.info("Wrote validation errors to %s", errors_path)
        return errors_path

    def write_retry_prompt(
        self,
        run_dir: Path,
        content: str,
    ) -> Path:
        """Write retry prompt for agent to run directory."""
        prompt_path = run_dir / RETRY_PROMPT_NAME
        self._write_text(prompt_path, content)
        logger.info("Wrote retry prompt to %s", prompt_path)
        return prompt_path

    def clear_retry_state(
        self,
        run_dir: Path,
    ) -> None:
        """Clear retry state files from run directory."""
        state_path = run_dir / VALIDATION_STATE_NAME
        prompt_path = run_dir / RETRY_PROMPT_NAME

        for path in [state_path, prompt_path]:
            if path.exists():
                try:
                    path.unlink()
                    logger.debug("Removed %s", path)
                except OSError as e:
                    logger.warning("Failed to remove %s: %s", path, e)

        # Keep validation-errors.txt for debugging

    # -------------------------------------------------------------------------
    # Diagnostics
    # -------------------------------------------------------------------------

    def write_diagnostic(
        self,
        run_dir: Path,
        diagnostic: dict[str, Any],
        prefix: str = "failure-diagnostic",
    ) -> Path:
        """Write a diagnostic file to run directory."""
        run_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
        filename = f"{prefix}-{timestamp}.json"
        diagnostic_path = run_dir / filename

        self._write_json(diagnostic_path, diagnostic)
        self.update_manifest(run_dir, {"diagnostic_path": str(diagnostic_path)})
        logger.info("Wrote diagnostic to %s", diagnostic_path)
        return diagnostic_path

    # -------------------------------------------------------------------------
    # Review Exchange Artifacts
    # -------------------------------------------------------------------------

    def store_review_exchange_summary(
        self,
        worktree_path: Path,
        session_name: str,
        summary: dict[str, Any],
        validation_record_path: Path | None = None,
    ) -> ReviewExchangeSummary:
        run_dir = self.ensure_run_dir(worktree_path, session_name)
        exchange_dir = run_dir / REVIEW_EXCHANGE_DIR_NAME
        exchange_dir.mkdir(parents=True, exist_ok=True)
        summary_path = exchange_dir / REVIEW_EXCHANGE_SUMMARY_NAME
        self._write_json(summary_path, summary)

        stored_validation: Path | None = None
        if validation_record_path and validation_record_path.exists():
            stored_validation = run_dir / VALIDATION_RECORD_NAME
            try:
                shutil.copy2(validation_record_path, stored_validation)
            except OSError:
                logger.debug("Failed to copy validation record to %s", stored_validation)
                stored_validation = None

        updates = {
            "review_exchange_dir": str(exchange_dir),
            "review_exchange_summary_path": str(summary_path),
        }
        if stored_validation:
            updates["validation_record_path"] = str(stored_validation)
        self.update_manifest(run_dir, updates)

        return ReviewExchangeSummary(
            summary=summary,
            exchange_dir=exchange_dir,
            summary_path=summary_path,
            validation_record_path=stored_validation,
        )

    def load_review_exchange_summary(
        self,
        worktree_path: Path,
        session_name: str,
    ) -> ReviewExchangeSummary | None:
        run_dir = self.find_run_dir(worktree_path, session_name=session_name)
        if not run_dir:
            return None
        manifest = self.read_manifest(run_dir) or {}
        manifest_dir = manifest.get("review_exchange_dir")
        exchange_dir = Path(manifest_dir) if manifest_dir else run_dir / REVIEW_EXCHANGE_DIR_NAME
        summary_path = exchange_dir / REVIEW_EXCHANGE_SUMMARY_NAME
        if not summary_path.exists():
            return None
        summary = self._read_json(summary_path)
        if not isinstance(summary, dict):
            return None
        validation_path = run_dir / VALIDATION_RECORD_NAME
        return ReviewExchangeSummary(
            summary=summary,
            exchange_dir=exchange_dir,
            summary_path=summary_path,
            validation_record_path=validation_path if validation_path.exists() else None,
        )

    # -------------------------------------------------------------------------
    # Session Metadata
    # -------------------------------------------------------------------------

    def write_worktree_note(
        self,
        run_dir: Path,
        payload: dict[str, Any],
    ) -> Path:
        """Write worktree metadata to run directory."""
        note_path = run_dir / WORKTREE_NOTE_NAME
        self._write_json(note_path, payload)
        return note_path

    def write_session_identity(
        self,
        run_dir: Path,
        payload: dict[str, Any],
    ) -> Path:
        """Write session identity to run directory."""
        identity_path = run_dir / SESSION_IDENTITY_NAME
        self._write_json(identity_path, payload)
        return identity_path

    # -------------------------------------------------------------------------
    # Log Access
    # -------------------------------------------------------------------------

    def get_log_path(
        self,
        worktree_path: Path,
        session_name: str,
    ) -> Path | None:
        """Get the session log path for a session."""
        run_dir = self.find_run_dir(worktree_path, session_name=session_name)
        if not run_dir:
            return None

        for filename in (SESSION_LOG_NAME, PANE_LOG_NAME):
            candidate = run_dir / filename
            if candidate.exists():
                return candidate

        return run_dir / SESSION_LOG_NAME

    def attach_claude_log(
        self,
        run_dir: Path,
    ) -> Path | None:
        """Find and attach Claude log to run directory."""
        log_path, session_id = self._select_claude_log_for_run(run_dir)
        if not log_path:
            return None

        if not session_id:
            session_id = log_path.stem

        updates = {
            "claude_log_path": str(log_path),
            "claude_session_id": session_id,
        }
        self.update_manifest(run_dir, updates)

        try:
            self._write_text(run_dir / CLAUDE_SESSION_PATH_NAME, str(log_path))
        except OSError:
            return log_path

        self._ensure_symlink(run_dir / CLAUDE_SESSION_LOG_NAME, log_path)
        return log_path

    def write_orchestrator_tail(
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

        # Get run_id and started_at from manifest
        run_id = None
        started_at = None
        manifest = self.read_manifest(run_dir)
        if manifest:
            run_id = manifest.get("run_id")
            started_at = manifest.get("started_at")

        try:
            lines = log_path.read_text(errors="ignore").splitlines()
        except OSError:
            return None

        if not lines:
            return None

        issue_token = f"issue-{issue_number}"
        session_token = f"session_id={session_name}"

        # Try to find session start by run_id marker
        segment = lines
        found_marker = False
        if run_id:
            marker = f"run_id={run_id}"
            for idx in range(len(lines) - 1, -1, -1):
                if "SESSION_RUN_START" in lines[idx] and marker in lines[idx]:
                    segment = lines[idx:]
                    found_marker = True
                    break

        # Fallback: filter by timestamp if marker not found
        if not found_marker and started_at:
            segment = self._filter_lines_by_timestamp(lines, started_at)

        scoped = [
            line
            for line in segment[-2000:]
            if issue_token in line or session_token in line
        ]
        if not scoped:
            scoped = lines[-max_lines:]

        tail_lines = scoped[-max_lines:]
        tail_path = run_dir / ORCHESTRATOR_TAIL_NAME
        self._write_text(tail_path, "\n".join(tail_lines))
        self.update_manifest(run_dir, {"orchestrator_tail": str(tail_path)})
        return tail_path

    # -------------------------------------------------------------------------
    # Private Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _run_timestamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _run_dir_name(session_name: str, run_id: str) -> str:
        return f"{run_id}__{session_name}"

    @staticmethod
    def _session_name_from_run_dir(name: str) -> str | None:
        if "__" not in name:
            return name
        return name.split("__", 1)[1]

    def _ensure_base_dir(self, worktree_path: Path) -> Path:
        base_dir = self.sessions_base_dir(worktree_path)
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir

    @staticmethod
    def _ensure_symlink(symlink_path: Path, target: Path) -> None:
        try:
            if symlink_path.is_symlink() or symlink_path.exists():
                if symlink_path.resolve() == target.resolve():
                    return
                if symlink_path.is_dir() and not symlink_path.is_symlink():
                    return
                symlink_path.unlink()
            symlink_path.symlink_to(target, target_is_directory=True)
        except OSError:
            return

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            if not path.exists():
                return None
            return json.loads(path.read_text())
        except Exception:
            return None

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    @staticmethod
    def _delete_tree(path: Path) -> None:
        for child in path.iterdir():
            if child.is_dir():
                FileSystemSessionOutput._delete_tree(child)
            else:
                child.unlink()
        path.rmdir()

    def _update_latest(self, worktree_path: Path, manifest: dict[str, Any]) -> None:
        payload = {
            "session_name": manifest.get("session_name"),
            "run_id": manifest.get("run_id"),
            "started_at": manifest.get("started_at"),
            "issue_number": manifest.get("issue_number"),
            "run_dir": manifest.get("run_dir"),
            "log_path": manifest.get("log_path"),
        }
        latest_path = self.sessions_base_dir(worktree_path) / LATEST_NAME
        self._write_json(latest_path, payload)
        self._write_json(
            worktree_path / ".issue-orchestrator" / ROOT_LATEST_NAME, payload
        )

    def _load_latest(self, worktree_path: Path) -> dict[str, Any] | None:
        latest_path = self.sessions_base_dir(worktree_path) / LATEST_NAME
        return self._read_json(latest_path)

    def _append_index(self, worktree_path: Path, manifest: dict[str, Any]) -> None:
        index_path = self.sessions_base_dir(worktree_path) / INDEX_NAME
        index = self._read_json(index_path) or {"runs": []}
        index["runs"].append(
            {
                "session_name": manifest.get("session_name"),
                "run_id": manifest.get("run_id"),
                "started_at": manifest.get("started_at"),
                "issue_number": manifest.get("issue_number"),
                "run_dir": manifest.get("run_dir"),
                "backend": manifest.get("backend"),
                "agent_label": manifest.get("agent_label"),
            }
        )
        self._write_json(index_path, index)

    def _prune_index(self, worktree_path: Path, removed: list[Path]) -> None:
        index_path = self.sessions_base_dir(worktree_path) / INDEX_NAME
        index = self._read_json(index_path)
        if not index or "runs" not in index:
            return
        removed_set = {str(p) for p in removed}
        index["runs"] = [r for r in index["runs"] if r.get("run_dir") not in removed_set]
        self._write_json(index_path, index)

    def _refresh_latest(self, worktree_path: Path) -> None:
        base_dir = self.sessions_base_dir(worktree_path)
        if not base_dir.exists():
            return

        runs = sorted(
            [d for d in base_dir.iterdir() if d.is_dir() and not d.is_symlink()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not runs:
            return

        latest_run = runs[0]
        manifest = self.read_manifest(latest_run)
        if manifest:
            self._update_latest(worktree_path, manifest)
            session_name = manifest.get("session_name")
            if session_name:
                self._ensure_symlink(base_dir / session_name, latest_run)

    def _select_claude_log_for_run(
        self, run_dir: Path
    ) -> tuple[Path | None, str | None]:
        manifest = self.read_manifest(run_dir) or {}
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
                        parsed = FileSystemSessionOutput._parse_iso_timestamp(timestamp)
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

    # -------------------------------------------------------------------------
    # Utility Methods (for finding run dirs by issue, etc.)
    # -------------------------------------------------------------------------

    def find_run_dir_for_issue(
        self, worktree_path: Path, issue_number: int
    ) -> Path | None:
        """Find the most recent run dir for an issue inside a worktree."""
        base_dir = self.sessions_base_dir(worktree_path)
        if not base_dir.exists():
            return None

        suffixes = (
            f"__issue-{issue_number}",
            f"__review-{issue_number}",
            f"__rework-{issue_number}",
        )
        candidates: list[Path] = []

        for run_dir in base_dir.iterdir():
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            name = run_dir.name
            if name.endswith(suffixes):
                candidates.append(run_dir)
                continue
            manifest = self.read_manifest(run_dir)
            if manifest and manifest.get("issue_number") == issue_number:
                candidates.append(run_dir)

        if not candidates:
            return None

        return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]

    def session_name_from_path(self, rel_path: str | None) -> str | None:
        """Extract session name from a path containing sessions directory."""
        if not rel_path:
            return None
        parts = Path(rel_path).parts
        try:
            idx = parts.index(SESSION_OUTPUT_DIR)
        except ValueError:
            return None
        if idx + 1 >= len(parts):
            return None
        run_dir_name = parts[idx + 1]
        return self._session_name_from_run_dir(run_dir_name)

    def ensure_claude_log_attached(
        self,
        worktree_path: Path,
        session_name: str | None = None,
    ) -> Path | None:
        """Ensure Claude log is attached to the run directory.

        Finds the run directory first, then attaches the Claude log.

        Args:
            worktree_path: Path to the worktree
            session_name: Optional session name to find

        Returns:
            Path to Claude log, or None if not found
        """
        run_dir = self.find_run_dir(worktree_path, session_name)
        if not run_dir:
            return None

        # Check if already attached
        manifest = self.read_manifest(run_dir) or {}
        existing = manifest.get("claude_log_path")
        if existing:
            return Path(existing)

        return self.attach_claude_log(run_dir)

    def find_latest_session_log_path(self, worktree_path: Path) -> Path | None:
        """Find the most recently updated session log in a worktree.

        Args:
            worktree_path: Path to the worktree

        Returns:
            Path to session log, or None if not found
        """
        run_dir = self.find_run_dir(worktree_path)
        if not run_dir:
            return None

        for filename in (SESSION_LOG_NAME, PANE_LOG_NAME):
            candidate = run_dir / filename
            if candidate.exists():
                return candidate

        return None


# -----------------------------------------------------------------------------
# Module-level utility functions
# -----------------------------------------------------------------------------

def find_run_dir_for_issue(
    worktree_bases: list[Path],
    repo_name: str,
    issue_number: int,
) -> tuple[Path | None, Path | None]:
    """Locate the latest run dir and worktree path for an issue.

    Searches across multiple worktree bases to find the most recent
    session run directory for a given issue.

    Args:
        worktree_bases: List of directories to search for worktrees
        repo_name: Repository name for worktree naming convention
        issue_number: Issue number to find

    Returns:
        Tuple of (run_dir, worktree_path), or (None, None) if not found
    """
    session_output = FileSystemSessionOutput()
    seen: set[Path] = set()

    for base in worktree_bases:
        if not base or not base.exists():
            continue
        base = base.resolve()
        if base in seen:
            continue
        seen.add(base)

        # Try direct match first
        candidate = base / f"{repo_name}-{issue_number}"
        if candidate.exists():
            run_dir = session_output.find_run_dir_for_issue(candidate, issue_number)
            if run_dir:
                return run_dir, candidate

        # Search all worktrees in this base
        for worktree_path in base.glob(f"{repo_name}-*"):
            if not worktree_path.is_dir():
                continue
            run_dir = session_output.find_run_dir_for_issue(worktree_path, issue_number)
            if run_dir:
                return run_dir, worktree_path

    return None, None


def session_output_dir(worktree_path: Path, session_name: str) -> Path:
    """Return the stable session directory (symlink to latest run).

    This is a convenience function for constructing session directory paths.

    Args:
        worktree_path: Path to the worktree
        session_name: Name of the session

    Returns:
        Path to the session directory
    """
    return worktree_path / ".issue-orchestrator" / SESSION_OUTPUT_DIR / session_name
