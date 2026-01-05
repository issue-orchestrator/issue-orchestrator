"""E2E logging utilities for progress tracking and log snapshots."""

import subprocess
from datetime import datetime
from pathlib import Path

from .orchestrator_process import E2E_LOG_DIR


# Derived log paths
E2E_PROGRESS_LOG = E2E_LOG_DIR / "pytest-progress.log"
E2E_CURRENT_TEST = E2E_LOG_DIR / "pytest-current-test.txt"
E2E_SNAPSHOT_LOG = E2E_LOG_DIR / "pytest-abort-snapshot.log"


def write_progress(event: str, nodeid: str = "", extra: str = "") -> None:
    """Persist pytest progress so aborted runs still have breadcrumbs."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{timestamp} {event}"
    if nodeid:
        line += f" {nodeid}"
    if extra:
        line += f" {extra}"
    try:
        with E2E_PROGRESS_LOG.open("a") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def tail_lines(path: Path, limit: int = 200) -> list[str]:
    """Get the last N lines of a file."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-limit:]


def find_recent_worktrees(limit: int = 3) -> list[Path]:
    """Find recent e2e worktrees for snapshotting."""
    candidates: list[Path] = []
    tmp_root = Path("/tmp/e2e-worktrees")
    if tmp_root.exists():
        candidates.extend([p for p in tmp_root.iterdir() if p.is_dir()])

    # pytest tmp worktrees live under /private/var/folders/*/*/T/pytest-of-*/.../worktrees/*
    tmp_parent = Path("/private/var/folders")
    if tmp_parent.exists():
        for pytest_dir in tmp_parent.glob("*/*/T/pytest-of-*"):
            for worktree_root in pytest_dir.glob("**/worktrees"):
                try:
                    for worktree in worktree_root.iterdir():
                        if worktree.is_dir():
                            candidates.append(worktree)
                except OSError:
                    continue

    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return candidates[:limit]


def claude_project_dir_for(worktree: Path) -> Path:
    """Get the Claude project directory for a worktree."""
    escaped = "-" + str(worktree).lstrip("/").replace("/", "-")
    return Path.home() / ".claude" / "projects" / escaped


def snapshot_logs(reason: str) -> None:
    """Persist tail snapshots of the latest logs for aborted/failed sessions."""
    try:
        latest_orch = max(E2E_LOG_DIR.glob("orchestrator-*.log"), key=lambda p: p.stat().st_mtime, default=None)
        latest_e2e = max(E2E_LOG_DIR.glob("e2e-*.log"), key=lambda p: p.stat().st_mtime, default=None)
    except OSError:
        latest_orch = None
        latest_e2e = None

    try:
        with E2E_SNAPSHOT_LOG.open("a") as handle:
            handle.write("=" * 60 + "\n")
            handle.write(f"SNAPSHOT reason={reason}\n")
            if latest_e2e:
                handle.write(f"[E2E] {latest_e2e}\n")
                for line in tail_lines(latest_e2e):
                    handle.write(line + "\n")
            if latest_orch:
                handle.write(f"[ORCH] {latest_orch}\n")
                for line in tail_lines(latest_orch):
                    handle.write(line + "\n")

            # Tmux context can explain stuck sessions
            tmux_list = None
            try:
                handle.write("[TMUX] list-windows\n")
                tmux_list = subprocess.run(
                    ["tmux", "list-windows", "-t", "orchestrator"],
                    capture_output=True,
                    text=True,
                )
                if tmux_list.returncode == 0:
                    handle.write(tmux_list.stdout.strip() + "\n")
                else:
                    handle.write(f"(tmux list-windows failed: {tmux_list.stderr.strip()})\n")
            except OSError:
                handle.write("(tmux not available)\n")

            # Capture recent pane output for each window to aid debugging
            if tmux_list and tmux_list.returncode == 0:
                try:
                    tmux_windows = []
                    for line in tmux_list.stdout.splitlines():
                        window_id = line.split(":")[0].strip()
                        if window_id:
                            tmux_windows.append(window_id)
                    for window_id in tmux_windows:
                        handle.write(f"[TMUX] capture-pane window={window_id}\n")
                        cap = subprocess.run(
                            ["tmux", "capture-pane", "-t", f"orchestrator:{window_id}", "-p", "-S", "-200"],
                            capture_output=True,
                            text=True,
                        )
                        if cap.returncode == 0:
                            handle.write(cap.stdout.strip() + "\n")
                        else:
                            handle.write(f"(capture-pane failed: {cap.stderr.strip()})\n")
                except OSError:
                    handle.write("(tmux capture-pane not available)\n")

            # Snapshot recent worktree artifacts
            for worktree in find_recent_worktrees():
                handle.write(f"[WORKTREE] {worktree}\n")
                session_dir = worktree / ".issue-orchestrator"
                if session_dir.exists():
                    for identity in session_dir.glob("session-identity-*.json"):
                        handle.write(f"[IDENTITY] {identity}\n")
                        for line in tail_lines(identity):
                            handle.write(line + "\n")
                    for completion in session_dir.glob("completion-*.json"):
                        handle.write(f"[COMPLETION] {completion}\n")
                        for line in tail_lines(completion):
                            handle.write(line + "\n")
                    session_log = session_dir / "session.log"
                    if session_log.exists():
                        handle.write(f"[SESSION_LOG] {session_log}\n")
                        for line in tail_lines(session_log):
                            handle.write(line + "\n")

                claude_dir = claude_project_dir_for(worktree)
                handle.write(f"[CLAUDE] {claude_dir}\n")
                if claude_dir.exists():
                    jsonl_files = sorted(claude_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if jsonl_files:
                        recent = jsonl_files[0]
                        handle.write(f"[CLAUDE_JSONL] {recent}\n")
                        for line in tail_lines(recent):
                            handle.write(line + "\n")
    except OSError:
        pass
