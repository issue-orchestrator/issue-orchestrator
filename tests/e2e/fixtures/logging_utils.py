"""E2E logging utilities for progress tracking and log snapshots."""

from datetime import datetime
import subprocess
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


def find_recent_worktrees(limit: int = 3, worktree_base: Path | None = None) -> list[Path]:
    """Find recent e2e worktrees for snapshotting.

    Args:
        limit: Maximum number of worktrees to return.
        worktree_base: Base directory for worktrees. Defaults to /tmp/e2e-worktrees.
    """
    candidates: list[Path] = []
    tmp_root = worktree_base or Path("/tmp/e2e-worktrees")
    if tmp_root.exists():
        for root in tmp_root.iterdir():
            if not root.is_dir():
                continue
            candidates.append(root)
            # Support per-issue worktree roots (e.g., worktree_base/issue-123/...).
            try:
                for child in root.iterdir():
                    if child.is_dir():
                        candidates.append(child)
            except OSError:
                continue

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


def find_worktree_for_issue(issue_number: int, worktree_base: Path | None = None) -> Path | None:
    """Find a worktree directory whose branch name matches the issue number."""
    for worktree in find_recent_worktrees(limit=50, worktree_base=worktree_base):
        try:
            result = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            continue
        if result.returncode != 0:
            continue
        branch = result.stdout.strip()
        if branch.startswith(f"{issue_number}-") or branch == str(issue_number):
            return worktree
        if branch == "HEAD":
            session_marker = worktree / ".issue-orchestrator" / "sessions" / f"issue-{issue_number}" / "identity.json"
            if session_marker.exists():
                return worktree
    return None


def claude_project_dir_for(worktree: Path) -> Path:
    """Get the Claude project directory for a worktree."""
    escaped = "-" + str(worktree).lstrip("/").replace("/", "-")
    return Path.home() / ".claude" / "projects" / escaped


def snapshot_logs(reason: str, worktree_base: Path | None = None) -> None:
    """Persist tail snapshots of the latest logs for aborted/failed sessions.

    Args:
        reason: Reason for the snapshot.
        worktree_base: Base directory for worktrees. Defaults to /tmp/e2e-worktrees.
    """
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

            # Snapshot recent worktree artifacts
            for worktree in find_recent_worktrees(worktree_base=worktree_base):
                handle.write(f"[WORKTREE] {worktree}\n")
                session_root = worktree / ".issue-orchestrator" / "sessions"
                if session_root.exists():
                    for session_dir in sorted(session_root.iterdir()):
                        if not session_dir.is_dir():
                            continue
                        identity = session_dir / "identity.json"
                        if identity.exists():
                            handle.write(f"[IDENTITY] {identity}\n")
                            for line in tail_lines(identity):
                                handle.write(line + "\n")
                        for completion in session_dir.glob("completion*.json"):
                            handle.write(f"[COMPLETION] {completion}\n")
                            for line in tail_lines(completion):
                                handle.write(line + "\n")
                        for log_name in ("session.log", "pane.log"):
                            session_log = session_dir / log_name
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
