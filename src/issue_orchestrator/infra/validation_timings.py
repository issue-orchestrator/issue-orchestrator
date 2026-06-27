"""Shared validation timing artifact helpers."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_CONFIG_KEY_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"
_CONFIG_FIELD_PATTERN = rf"{_CONFIG_KEY_PATTERN}=\S+"
_CONFIG_RE = re.compile(
    rf"\[validate-timing\] CONFIG (?P<fields>{_CONFIG_FIELD_PATTERN}(?:\s+{_CONFIG_FIELD_PATTERN})*)\s*$"
)
_CONFIG_FIELD_RE = re.compile(rf"(?P<key>{_CONFIG_KEY_PATTERN})=(?P<value>\S+)")
_START_RE = re.compile(
    r"\[validate-timing\] START target=(?P<target>\S+) at=(?P<at>\S+)"
)
_END_RE = re.compile(
    r"\[validate-timing\] END target=(?P<target>\S+) "
    r"status=(?P<status>-?\d+) elapsed=(?P<elapsed>\d+)s at=(?P<at>\S+)"
)


def resolve_git_dir(worktree: Path) -> Path | None:
    """Resolve the git dir for the current worktree without shelling out."""
    dot_git = worktree / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    content = dot_git.read_text(encoding="utf-8").strip()
    prefix = "gitdir: "
    if not content.startswith(prefix):
        return None
    git_dir = Path(content[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    return git_dir


def resolve_git_common_dir(worktree: Path) -> Path | None:
    """Resolve the repository's shared git dir for cross-worktree artifacts."""
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    commondir_file = git_dir / "commondir"
    if not commondir_file.exists():
        return git_dir
    common_dir = Path(commondir_file.read_text(encoding="utf-8").strip())
    if not common_dir.is_absolute():
        common_dir = (git_dir / common_dir).resolve()
    return common_dir


def read_head_ref_name(git_dir: Path) -> str | None:
    """Read the current branch name from HEAD when it points at a ref."""
    head_file = git_dir / "HEAD"
    if not head_file.exists():
        return None
    head = head_file.read_text(encoding="utf-8").strip()
    prefix = "ref: refs/heads/"
    if not head.startswith(prefix):
        return None
    return head[len(prefix) :]


def read_branch_name(worktree: Path) -> str | None:
    """Best-effort branch name for diagnostics records without git subprocesses."""
    git_dir = resolve_git_dir(worktree)
    if git_dir is None:
        return None
    branch = read_head_ref_name(git_dir)
    if branch:
        return branch
    common_dir = resolve_git_common_dir(worktree)
    if common_dir is not None:
        return read_head_ref_name(common_dir)
    return None


def current_branch_name(worktree: Path) -> str | None:
    """Best-effort branch name for diagnostics records."""
    return read_branch_name(worktree)


def get_shared_timings_file(worktree: Path) -> Path | None:
    """Return the shared JSONL timing file path for this repository."""
    common_dir = resolve_git_common_dir(worktree)
    if common_dir is None:
        return None
    return common_dir / "issue-orchestrator" / "validate-timings.jsonl"


def append_jsonl(path: Path | None, record: dict[str, object]) -> None:
    """Append one JSON object to a JSONL file, creating parents as needed."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        # Use one O_APPEND write so concurrent validation gates cannot interleave
        # JSONL fragments on local POSIX filesystems such as macOS APFS.
        written = os.write(fd, line)
        if written != len(line):
            raise OSError(f"short JSONL write to {path}: {written} of {len(line)}")
    finally:
        os.close(fd)


def build_timing_envelope(
    *,
    wall_started_at: datetime,
    monotonic_started_at: float,
) -> dict[str, object]:
    """Return common elapsed-time fields for validation timing records."""
    wall_ended_at = datetime.now(timezone.utc)
    return {
        "monotonic_elapsed_seconds": round(time.monotonic() - monotonic_started_at, 3),
        "wall_started_at": wall_started_at.isoformat(),
        "wall_ended_at": wall_ended_at.isoformat(),
        "wall_elapsed_seconds": round(
            (wall_ended_at - wall_started_at).total_seconds(), 3
        ),
    }


def append_validation_timing(worktree: Path, record: dict[str, object]) -> None:
    """Append one validation timing record with shared worktree context."""
    payload: dict[str, object] = {
        "worktree": str(worktree),
        "branch": current_branch_name(worktree),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(record)
    append_jsonl(get_shared_timings_file(worktree), payload)


def record_gate_timings(
    suite: str,
    worktree: Path,
    command: str,
    stdout: str,
    stderr: str,
) -> None:
    """Record target timings from captured publish-gate output."""
    if suite != "publish_gate":
        return
    recorder = ValidateTimingRecorder(worktree=worktree, command=command)
    recorder.process_output(stdout)
    recorder.process_output(stderr)


@dataclass
class ValidateTimingRecorder:
    """Collect and persist per-target validate timings from marker lines."""

    worktree: Path
    command: str
    run_id: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    branch: str | None = field(init=False)
    output_path: Path | None = field(init=False)
    config: dict[str, str] = field(default_factory=dict)
    starts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.branch = current_branch_name(self.worktree)
        self.output_path = get_shared_timings_file(self.worktree)

    def process_output(self, output: str) -> None:
        """Process captured command output containing validate timing markers."""
        for line in output.splitlines():
            self.process_line(line)

    def process_line(self, line: str) -> None:
        config_match = _CONFIG_RE.search(line)
        if config_match:
            self.config = {
                field.group("key"): field.group("value")
                for field in _CONFIG_FIELD_RE.finditer(config_match.group("fields"))
            }
            return

        start_match = _START_RE.search(line)
        if start_match:
            self.starts[start_match.group("target")] = start_match.group("at")
            return

        end_match = _END_RE.search(line)
        if not end_match:
            return

        target = end_match.group("target")
        record: dict[str, object] = {
            "kind": "target_timing",
            "run_id": self.run_id,
            "command": self.command,
            "worktree": str(self.worktree),
            "branch": self.branch,
            "target": target,
            "status": int(end_match.group("status")),
            "elapsed_seconds": int(end_match.group("elapsed")),
            "started_at": self.starts.pop(target, None),
            "ended_at": end_match.group("at"),
        }
        for key, value in self.config.items():
            record[key] = value
        append_jsonl(self.output_path, record)

    def finalize(self, *, exit_code: int, total_elapsed_seconds: float) -> None:
        record: dict[str, object] = {
            "kind": "run_summary",
            "run_id": self.run_id,
            "command": self.command,
            "worktree": str(self.worktree),
            "branch": self.branch,
            "exit_code": exit_code,
            "total_elapsed_seconds": round(total_elapsed_seconds, 3),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        for key, value in self.config.items():
            record[key] = value
        append_jsonl(self.output_path, record)

    def append_resource_sample(self, sample: dict[str, object]) -> None:
        """Persist one periodic host resource sample."""
        record = {
            "kind": "resource_sample",
            "run_id": self.run_id,
            "command": self.command,
            "worktree": str(self.worktree),
            "branch": self.branch,
            **sample,
        }
        for key, value in self.config.items():
            record[key] = value
        append_jsonl(self.output_path, record)
