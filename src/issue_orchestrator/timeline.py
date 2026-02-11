"""Timeline domain model and helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .ports.timeline_store import TimelineRecord


@dataclass(frozen=True)
class TimelineArtifact:
    artifact_type: str
    label: str
    value: str

    def to_dict(self) -> dict[str, str]:
        return {
            "type": self.artifact_type,
            "label": self.label,
            "value": self.value,
        }


@dataclass(frozen=True)
class TimelineEvent:
    event_id: str
    timestamp: str
    event: str
    issue_number: int
    phase: str
    step: str
    status: str
    level: str
    summary: str | None
    parent_key: str
    artifacts: list[TimelineArtifact]
    detail: str | None = None
    run_id: str | None = None
    run_dir: str | None = None
    agent: str | None = None
    task: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event": self.event,
            "issue_number": self.issue_number,
            "phase": self.phase,
            "step": self.step,
            "status": self.status,
            "level": self.level,
            "summary": self.summary,
            "parent_key": self.parent_key,
            "detail": self.detail,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "artifacts": [a.to_dict() for a in self.artifacts],
        }
        if self.agent:
            d["agent"] = self.agent
        if self.task:
            d["task"] = self.task
        return d


@dataclass(frozen=True)
class TimelineStream:
    """Higher-level view over timeline records for an issue."""

    issue_number: int
    events: list[TimelineEvent]

    @classmethod
    def from_records(cls, issue_number: int, records: list[TimelineRecord]) -> "TimelineStream":
        events = [_record_to_event(issue_number, record) for record in records]
        return cls(issue_number=issue_number, events=events)

    def group_by_phase(self) -> dict[str, list[TimelineEvent]]:
        grouped: dict[str, list[TimelineEvent]] = {}
        for event in self.events:
            grouped.setdefault(event.phase, []).append(event)
        return grouped

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_number": self.issue_number,
            "events": [event.to_dict() for event in self.events],
        }


def build_issue_timeline(issue_number: int, records: list[TimelineRecord]) -> dict[str, Any]:
    return TimelineStream.from_records(issue_number, records).to_dict()


def _record_to_event(issue_number: int, record: TimelineRecord) -> TimelineEvent:
    data = record.data or {}
    event_name = record.event
    phase = _phase_for_event(event_name)
    step = _step_for_event(event_name)
    status = _status_for_event(event_name)
    level = _level_for_event(event_name)
    summary = _summary_from_data(data)
    detail = _detail_from_data(event_name, data, summary)
    parent_key = _parent_key(issue_number, data)
    run_id = _run_id_from_data(data)
    run_dir = _run_dir_from_data(data)
    artifacts = _artifacts_from_data(data)
    agent = data.get("agent") if isinstance(data.get("agent"), str) else None
    task = data.get("task") if isinstance(data.get("task"), str) else None
    return TimelineEvent(
        event_id=record.event_id,
        timestamp=record.timestamp,
        event=event_name,
        issue_number=issue_number,
        phase=phase,
        step=step,
        status=status,
        level=level,
        summary=summary,
        detail=detail,
        parent_key=parent_key,
        run_id=run_id,
        run_dir=run_dir,
        artifacts=artifacts,
        agent=agent,
        task=task,
    )


def _phase_for_event(event_name: str) -> str:
    if event_name in {"issue.completed"}:
        return "completed"
    if event_name in {"issue.pr_created"}:
        return "pr_pending"
    if event_name in {"issue.blocked"}:
        return "blocked"
    if event_name in {"issue.needs_human"}:
        return "needs_human"
    if event_name.startswith("review."):
        return "reviewing"
    if event_name.startswith("rework."):
        return "rework"
    if event_name.startswith("issue."):
        return "in_progress"
    if event_name.startswith("session."):
        return "in_progress"
    if event_name.startswith("completion.") or event_name.startswith("observation."):
        return "in_progress"
    return "system"


def _step_for_event(event_name: str) -> str:
    if event_name.startswith("session."):
        return event_name.replace("session.", "")
    if event_name.startswith("issue."):
        return event_name.replace("issue.", "")
    if event_name.startswith("review."):
        return event_name.replace("review.", "")
    if event_name.startswith("rework."):
        return event_name.replace("rework.", "")
    if event_name.startswith("completion."):
        return event_name.replace("completion.", "")
    if event_name.startswith("observation."):
        return event_name.replace("observation.", "")
    return event_name


def _status_for_event(event_name: str) -> str:
    failure_events = {
        "session.failed",
        "session.timeout",
        "session.blocked",
        "session.validation_failed",
        "issue.blocked",
        "issue.dependency_blocked",
        "issue.needs_human",
        "issue.pr_rejected",
        "review.changes_requested",
        "review.escalated",
        "review.closed",
        "rework.escalating",
        "dependency.blocked",
    }
    success_events = {
        "session.completed",
        "issue.pr_created",
        "issue.completed",
        "issue.unblocked",
        "issue.released",
        "dependency.unblocked",
        "review.approved",
        "review.skipped",
        "review.rework_completed",
        "review.triage_approved",
        "review.merged",
        "rework.skipped",
        "triage.skipped",
        "cleanup.completed",
        "validation.completed",
    }
    pending_events = {
        "issue.claimed",
        "issue.started",
        "review.queued",
        "review.started",
        "review.launching",
        "review.rework_started",
        "review.triage_started",
        "rework.started",
        "rework.launching",
        "triage.issue_created",
        "triage.launching",
        "triage.batch_triggered",
        "validation.started",
        "provider.transient_error",
        "provider.outage_entered",
        "provider.retry_scheduled",
        "provider.retry_attempted",
        "provider.outage_exited",
    }
    if event_name in failure_events:
        return "failed"
    if event_name in success_events:
        return "completed"
    if event_name in pending_events:
        return "started"
    if event_name.endswith(".started") or event_name.endswith(".launching"):
        return "started"
    return "completed"


def _level_for_event(event_name: str) -> str:
    if event_name.startswith("issue.") or event_name.startswith("review."):
        return "phase"
    return "detail"


def _parent_key(issue_number: int, data: dict[str, Any]) -> str:
    if isinstance(data.get("session_id"), str):
        return f"session:{data['session_id']}"
    if isinstance(data.get("pr_number"), int):
        return f"review:{data['pr_number']}"
    return f"issue:{issue_number}"


def _summary_from_data(data: dict[str, Any]) -> str | None:
    for key in ("reason", "summary", "error", "status", "outcome"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


_MAX_DETAIL = 200


def _detail_from_data(
    event_name: str,
    data: dict[str, Any],
    summary: str | None,
) -> str | None:
    """Extract contextual detail from enriched event data.

    The detail augments the narrative with "what happened and why"
    without duplicating the summary.  Returns None when no useful
    detail can be extracted.
    """
    parts: list[str] = []
    summary_str = summary or ""

    if event_name in ("session.blocked", "issue.blocked"):
        _add_if_new(parts, data.get("attempted"), summary_str)
        blocked_by = data.get("blocked_by")
        if isinstance(blocked_by, list) and blocked_by:
            issues = ", ".join(f"#{n}" for n in blocked_by)
            parts.append(f"Blocked by: {issues}")

    elif event_name in ("session.timeout", "session.failed"):
        runtime = data.get("runtime_minutes")
        timeout = data.get("timeout_minutes")
        if runtime is not None and timeout is not None:
            parts.append(f"Ran {runtime:.0f} min (limit: {timeout} min)")
        elif runtime is not None:
            parts.append(f"Ran {runtime:.0f} min")
        _add_if_new(parts, data.get("problems"), summary_str)

    elif event_name == "session.completed":
        _add_if_new(parts, data.get("implementation"), summary_str)
        _add_if_new(parts, data.get("problems"), summary_str)

    elif event_name == "session.validation_failed":
        _add_if_new(parts, data.get("validation_reason"), summary_str)

    elif event_name == "review.changes_requested":
        _add_if_new(parts, data.get("review_issues"), summary_str)
        risk = data.get("risk_level")
        if isinstance(risk, str) and risk:
            parts.append(f"Risk: {risk}")

    elif event_name == "review.approved":
        _add_if_new(parts, data.get("review_summary"), summary_str)

    elif event_name == "review.escalated":
        rework = data.get("rework_cycle")
        limit = data.get("max_rework_cycles")
        if rework is not None and limit is not None:
            parts.append(f"Rework cycle {rework}/{limit}")

    elif event_name == "issue.needs_human":
        _add_if_new(parts, data.get("question"), summary_str)

    if not parts:
        return None

    text = ". ".join(parts)
    if len(text) > _MAX_DETAIL:
        text = text[: _MAX_DETAIL - 1] + "\u2026"
    return text


def _add_if_new(parts: list[str], value: Any, summary: str) -> None:
    """Append a string value to parts if it's not already in the summary."""
    if isinstance(value, str) and value and value not in summary:
        parts.append(value)


def _artifacts_from_data(data: dict[str, Any]) -> list[TimelineArtifact]:
    artifacts: list[TimelineArtifact] = []
    pr_url = data.get("pr_url")
    if isinstance(pr_url, str) and pr_url:
        artifacts.append(TimelineArtifact("pull_request", "PR", pr_url))
    comment_url = data.get("comment_url")
    if isinstance(comment_url, str) and comment_url:
        artifacts.append(TimelineArtifact("review_comment", "Review Comment", comment_url))
    completion_path = data.get("completion_path_absolute")
    if isinstance(completion_path, str) and completion_path:
        artifacts.append(TimelineArtifact("completion_record", "Completion", completion_path))
    worktree_path = data.get("worktree_path")
    if isinstance(worktree_path, str) and worktree_path:
        artifacts.append(TimelineArtifact("worktree", "Worktree", worktree_path))
    validation_path = data.get("validation_record_path")
    if isinstance(validation_path, str) and validation_path:
        artifacts.append(TimelineArtifact("validation", "Validation", validation_path))
    run_dir = _run_dir_from_data(data)
    if isinstance(run_dir, str) and run_dir:
        artifacts.append(TimelineArtifact("run_dir", "Run Dir", run_dir))
    return artifacts


def _run_id_from_data(data: dict[str, Any]) -> str | None:
    run_id = data.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    return None


def _run_dir_from_data(data: dict[str, Any]) -> str | None:
    run_dir = data.get("run_dir")
    if isinstance(run_dir, str) and run_dir:
        return run_dir
    return None
