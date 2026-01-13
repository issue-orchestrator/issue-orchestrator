"""Session output paths for per-session logs and artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SESSION_OUTPUT_DIR = "sessions"
SESSION_LOG_NAME = "session.log"
PANE_LOG_NAME = "pane.log"
WORKTREE_NOTE_NAME = "worktree.json"
SESSION_MANIFEST_NAME = "manifest.json"
SESSION_LATEST_NAME = "latest.json"
SESSION_INDEX_NAME = "index.json"
SESSION_LATEST_ROOT_NAME = "session-latest.json"
ORCHESTRATOR_TAIL_NAME = "orchestrator-tail.log"
CLAUDE_SESSION_PATH_NAME = "claude-session.path"
CLAUDE_SESSION_LOG_NAME = "claude-session.jsonl"


@dataclass(frozen=True)
class SessionRun:
    """Represents a single session run directory."""
    session_name: str
    run_id: str
    run_dir: Path
    log_path: Path
    manifest_path: Path
    started_at: str


class SessionOutputManager:
    """Manage per-session run directories and manifests."""

    @staticmethod
    def sessions_base_dir(worktree_path: Path) -> Path:
        return worktree_path / ".issue-orchestrator" / SESSION_OUTPUT_DIR

    @staticmethod
    def _run_timestamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")

    @classmethod
    def _run_dir_name(cls, session_name: str, run_id: str) -> str:
        return f"{run_id}__{session_name}"

    @classmethod
    def _session_name_from_run_dir(cls, name: str) -> str | None:
        if "__" not in name:
            return name
        return name.split("__", 1)[1]

    @classmethod
    def _ensure_base_dir(cls, worktree_path: Path) -> Path:
        base_dir = cls.sessions_base_dir(worktree_path)
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir

    @classmethod
    def start_run(
        cls,
        worktree_path: Path,
        session_name: str,
        issue_number: int | None = None,
        agent_label: str | None = None,
        backend: str | None = None,
        claude_log_dir: str | None = None,
        orchestrator_log: str | None = None,
        completion_path: str | None = None,
    ) -> SessionRun:
        """Create a new run directory and write its manifest."""
        run_id = cls._run_timestamp()
        base_dir = cls._ensure_base_dir(worktree_path)
        run_dir = base_dir / cls._run_dir_name(session_name, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        symlink_path = base_dir / session_name
        cls._ensure_symlink(symlink_path, run_dir)
        log_path = run_dir / SESSION_LOG_NAME
        manifest_path = run_dir / SESSION_MANIFEST_NAME
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
        _write_json(manifest_path, manifest)
        if claude_log_dir:
            _write_text(run_dir / "claude-log.path", claude_log_dir)
        if orchestrator_log:
            _write_text(run_dir / "orchestrator-log.path", orchestrator_log)
        cls._update_latest(worktree_path, manifest)
        cls._append_index(worktree_path, manifest)
        return SessionRun(
            session_name=session_name,
            run_id=run_id,
            run_dir=run_dir,
            log_path=log_path,
            manifest_path=manifest_path,
            started_at=started_at,
        )

    @classmethod
    def ensure_run_dir(cls, worktree_path: Path, session_name: str) -> Path:
        """Return an existing run dir for a session or create a minimal one."""
        existing = cls.find_latest_run_dir(worktree_path, session_name=session_name)
        if existing:
            return existing
        run = cls.start_run(worktree_path, session_name)
        return run.run_dir

    @classmethod
    def find_latest_run_dir(
        cls,
        worktree_path: Path,
        session_name: str | None = None,
    ) -> Path | None:
        base_dir = cls.sessions_base_dir(worktree_path)
        if not base_dir.exists():
            return None
        if session_name:
            legacy = base_dir / session_name
            if legacy.exists() and legacy.is_dir() and not legacy.is_symlink():
                return legacy
            candidates = sorted(
                [d for d in base_dir.iterdir() if d.is_dir() and d.name.endswith(f"__{session_name}")],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            return candidates[0] if candidates else None
        latest = cls._load_latest(worktree_path)
        if latest:
            return Path(latest["run_dir"])
        candidates = sorted(
            [d for d in base_dir.iterdir() if d.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    @classmethod
    def get_log_path(cls, worktree_path: Path, session_name: str) -> Path | None:
        run_dir = cls.find_latest_run_dir(worktree_path, session_name=session_name)
        if not run_dir:
            return None
        for filename in (SESSION_LOG_NAME, PANE_LOG_NAME):
            candidate = run_dir / filename
            if candidate.exists():
                return candidate
        return run_dir / SESSION_LOG_NAME

    @classmethod
    def prune_runs(cls, worktree_path: Path, keep: int) -> list[Path]:
        """Keep last N runs in this worktree; delete older ones."""
        if keep <= 0:
            return []
        base_dir = cls.sessions_base_dir(worktree_path)
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
                _delete_tree(run_dir)
                removed.append(run_dir)
            except OSError:
                continue
        if removed:
            cls._prune_index(worktree_path, removed)
            cls._refresh_latest(worktree_path)
        return removed

    @classmethod
    def update_manifest(cls, run_dir: Path, updates: dict[str, Any]) -> None:
        manifest_path = run_dir / SESSION_MANIFEST_NAME
        manifest = _read_json(manifest_path) or {}
        manifest.update(updates)
        _write_json(manifest_path, manifest)

    @classmethod
    def attach_claude_log(cls, worktree_path: Path, session_name: str) -> Path | None:
        run_dir = cls.find_latest_run_dir(worktree_path, session_name=session_name)
        if not run_dir:
            return None
        return cls._attach_claude_log_for_run(run_dir)

    @classmethod
    def _attach_claude_log_for_run(cls, run_dir: Path) -> Path | None:
        log_path, session_id = cls._select_claude_log_for_run(run_dir)
        if not log_path:
            return None
        if not session_id:
            session_id = log_path.stem
        updates = {
            "claude_log_path": str(log_path),
            "claude_session_id": session_id,
        }
        cls.update_manifest(run_dir, updates)
        try:
            _write_text(run_dir / CLAUDE_SESSION_PATH_NAME, str(log_path))
        except OSError:
            return log_path
        cls._ensure_symlink(run_dir / CLAUDE_SESSION_LOG_NAME, log_path)
        return log_path

    @classmethod
    def _select_claude_log_for_run(cls, run_dir: Path) -> tuple[Path | None, str | None]:
        manifest = _read_json(run_dir / SESSION_MANIFEST_NAME) or {}
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
            timestamp, session_id = cls._read_claude_log_metadata(path)
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
            except ValueError:
                pass
            except OSError:
                pass

        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return candidates[0], None

    @classmethod
    def _read_claude_log_metadata(cls, log_path: Path) -> tuple[datetime | None, str | None]:
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
                        parsed = cls._parse_iso_timestamp(timestamp)
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

    @classmethod
    def write_orchestrator_tail(
        cls,
        worktree_path: Path,
        session_name: str,
        log_path: Path,
        issue_number: int,
        max_lines: int = 400,
    ) -> Path | None:
        """Write an issue-scoped tail of the orchestrator log into the run dir."""
        if not log_path.exists():
            return None
        run_dir = cls.find_latest_run_dir(worktree_path, session_name=session_name)
        if not run_dir:
            return None
        run_id = None
        manifest = _read_json(run_dir / SESSION_MANIFEST_NAME)
        if manifest:
            run_id = manifest.get("run_id")
        try:
            lines = log_path.read_text(errors="ignore").splitlines()
        except OSError:
            return None
        if not lines:
            return None
        issue_token = f"issue-{issue_number}"
        session_token = f"session_id={session_name}"
        segment = lines
        if run_id:
            marker = f"run_id={run_id}"
            for idx in range(len(lines) - 1, -1, -1):
                if "SESSION_RUN_START" in lines[idx] and marker in lines[idx]:
                    segment = lines[idx:]
                    break
        scoped = [
            line for line in segment[-2000:]
            if issue_token in line or session_token in line
        ]
        if not scoped:
            scoped = lines[-max_lines:]
        tail_lines = scoped[-max_lines:]
        tail_path = run_dir / ORCHESTRATOR_TAIL_NAME
        _write_text(tail_path, "\n".join(tail_lines))
        cls.update_manifest(run_dir, {"orchestrator_tail": str(tail_path)})
        return tail_path

    @classmethod
    def session_name_from_path(cls, rel_path: str | None) -> str | None:
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
        return cls._session_name_from_run_dir(run_dir_name)

    @classmethod
    def run_dir_from_path(cls, rel_path: str | None) -> str | None:
        if not rel_path:
            return None
        parts = Path(rel_path).parts
        try:
            idx = parts.index(SESSION_OUTPUT_DIR)
        except ValueError:
            return None
        if idx + 1 >= len(parts):
            return None
        return parts[idx + 1]

    @classmethod
    def _ensure_symlink(cls, symlink_path: Path, target: Path) -> None:
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

    @classmethod
    def _append_index(cls, worktree_path: Path, manifest: dict[str, Any]) -> None:
        index_path = cls.sessions_base_dir(worktree_path) / SESSION_INDEX_NAME
        index = _read_json(index_path) or {"runs": []}
        index["runs"].append({
            "session_name": manifest.get("session_name"),
            "run_id": manifest.get("run_id"),
            "started_at": manifest.get("started_at"),
            "issue_number": manifest.get("issue_number"),
            "run_dir": manifest.get("run_dir"),
            "backend": manifest.get("backend"),
            "agent_label": manifest.get("agent_label"),
        })
        _write_json(index_path, index)

    @classmethod
    def _update_latest(cls, worktree_path: Path, manifest: dict[str, Any]) -> None:
        payload = {
            "session_name": manifest.get("session_name"),
            "run_id": manifest.get("run_id"),
            "started_at": manifest.get("started_at"),
            "issue_number": manifest.get("issue_number"),
            "run_dir": manifest.get("run_dir"),
            "log_path": manifest.get("log_path"),
        }
        latest_path = cls.sessions_base_dir(worktree_path) / SESSION_LATEST_NAME
        _write_json(latest_path, payload)
        _write_json(worktree_path / ".issue-orchestrator" / SESSION_LATEST_ROOT_NAME, payload)

    @classmethod
    def _load_latest(cls, worktree_path: Path) -> dict[str, Any] | None:
        latest_path = cls.sessions_base_dir(worktree_path) / SESSION_LATEST_NAME
        return _read_json(latest_path)

    @classmethod
    def _prune_index(cls, worktree_path: Path, removed: list[Path]) -> None:
        index_path = cls.sessions_base_dir(worktree_path) / SESSION_INDEX_NAME
        index = _read_json(index_path)
        if not index or "runs" not in index:
            return
        removed_set = {str(p) for p in removed}
        index["runs"] = [r for r in index["runs"] if r.get("run_dir") not in removed_set]
        _write_json(index_path, index)

    @classmethod
    def _refresh_latest(cls, worktree_path: Path) -> None:
        base_dir = cls.sessions_base_dir(worktree_path)
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
        manifest_path = latest_run / SESSION_MANIFEST_NAME
        manifest = _read_json(manifest_path)
        if manifest:
            cls._update_latest(worktree_path, manifest)
            session_name = manifest.get("session_name")
            if session_name:
                cls._ensure_symlink(base_dir / session_name, latest_run)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _delete_tree(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            _delete_tree(child)
        else:
            child.unlink()
    path.rmdir()


def session_output_dir(worktree_path: Path, session_name: str) -> Path:
    """Return the stable session directory (symlink to latest run)."""
    base_dir = SessionOutputManager.sessions_base_dir(worktree_path)
    return base_dir / session_name


def ensure_session_output_dir(worktree_path: Path, session_name: str) -> Path:
    """Create and return the per-session output directory."""
    base_dir = SessionOutputManager.sessions_base_dir(worktree_path)
    base_dir.mkdir(parents=True, exist_ok=True)
    symlink_path = base_dir / session_name
    run_dir = SessionOutputManager.ensure_run_dir(worktree_path, session_name)
    SessionOutputManager._ensure_symlink(symlink_path, run_dir)
    return symlink_path


def find_session_log_path(worktree_path: Path, session_name: str) -> Path | None:
    """Find the local session log for a session, if present."""
    return SessionOutputManager.get_log_path(worktree_path, session_name)


def find_latest_session_log_path(worktree_path: Path) -> Path | None:
    """Find the most recently updated local session log in a worktree."""
    run_dir = SessionOutputManager.find_latest_run_dir(worktree_path)
    if not run_dir:
        return None
    for filename in (SESSION_LOG_NAME, PANE_LOG_NAME):
        candidate = run_dir / filename
        if candidate.exists():
            return candidate
    return None
