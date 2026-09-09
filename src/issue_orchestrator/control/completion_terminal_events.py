"""Publish terminal session events from one completion-owned boundary."""

from typing import Any

from ..domain.models import Session, SessionStatus
from ..domain.session_key import TaskKind
from ..events import EventName
from ..ports import (
    EventSink,
    make_session_completed_event,
    make_session_failed_event,
    make_trace_event,
)
from ..ports.session_output import SessionOutput
from .invalid_record_actions import failure_event_reason, invalid_record_event_fields
from .session_run_resolution import resolve_session_run_dir


class CompletionTerminalEventPublisher:
    """Own every trace-event projection of a terminal session outcome."""

    def __init__(self, events: EventSink, session_output: SessionOutput) -> None:
        self._events = events
        self._session_output = session_output

    def publish(
        self,
        session: Session,
        status: SessionStatus,
        pr_url: str | None,
        pr_number: int | None,
        *,
        blocked_reason: str | None = None,
        completion_detail: dict[str, Any] | None = None,
    ) -> None:
        """Publish the terminal event selected by the effective outcome."""
        detail = completion_detail or {}
        if status == SessionStatus.COMPLETED:
            self._publish_completed(session, pr_url, pr_number, detail)
        elif status in (SessionStatus.FAILED, SessionStatus.TIMED_OUT):
            self._publish_failure(session, status, detail)
        elif status == SessionStatus.BLOCKED:
            self._publish_blocked(session, blocked_reason, detail)
        elif status == SessionStatus.NEEDS_HUMAN:
            self._publish_needs_human(session, blocked_reason, detail)

    def _publish_completed(
        self,
        session: Session,
        pr_url: str | None,
        pr_number: int | None,
        detail: dict[str, Any],
    ) -> None:
        if session.key.task in {TaskKind.REVIEW, TaskKind.RETROSPECTIVE_REVIEW}:
            return
        task = session.key.task.value
        payload: dict[str, Any] = {
            "issue_number": session.issue.number,
            "session_id": session.terminal_id,
            "agent": session.agent_label,
            "task": task,
            "rework_cycle": session.rework_cycle,
            "pr_url": pr_url,
            "runtime_minutes": session.runtime_minutes,
            "completion_path_absolute": self._completion_path(session, detail),
            "run_dir": str(resolve_session_run_dir(self._session_output, session)),
        }
        for key in (
            "implementation",
            "problems",
            "review_summary",
            "review_issues",
            "risk_level",
        ):
            if detail.get(key):
                payload[key] = detail[key]
        self._events.publish(make_session_completed_event(payload))
        if pr_url and pr_number is not None:
            self._events.publish(
                make_trace_event(
                    EventName.ISSUE_PR_CREATED,
                    {
                        "issue_number": session.issue.number,
                        "pr_url": pr_url,
                        "pr_number": pr_number,
                        "agent": session.agent_label,
                        "task": task,
                        "rework_cycle": session.rework_cycle,
                    },
                )
            )

    @staticmethod
    def _completion_path(session: Session, detail: dict[str, Any]) -> str:
        supplied = detail.get("completion_path_absolute")
        if isinstance(supplied, str) and supplied.strip():
            return supplied
        return str((session.worktree_path / session.completion_path).resolve())

    def _publish_failure(
        self, session: Session, status: SessionStatus, detail: dict[str, Any]
    ) -> None:
        payload: dict[str, Any] = {
            "issue_number": session.issue.number,
            "session_id": session.terminal_id,
            "agent": session.agent_label,
            "task": session.key.task.value,
            "rework_cycle": session.rework_cycle,
            "error": failure_event_reason(
                expired=status == SessionStatus.TIMED_OUT,
                timeout_minutes=session.agent_config.timeout_minutes,
                detail=detail,
            ),
            "runtime_minutes": session.runtime_minutes,
            "timeout_minutes": session.agent_config.timeout_minutes,
            "run_dir": str(resolve_session_run_dir(self._session_output, session)),
        }
        payload.update(invalid_record_event_fields(detail))
        self._events.publish(make_session_failed_event(payload))

    def _publish_blocked(
        self, session: Session, blocked_reason: str | None, detail: dict[str, Any]
    ) -> None:
        payload: dict[str, Any] = {
            "issue_number": session.issue.number,
            "agent": session.agent_label,
            "task": session.key.task.value,
            "rework_cycle": session.rework_cycle,
            "reason": blocked_reason or "Agent marked issue as blocked",
        }
        for key in ("attempted", "blocked_by"):
            if detail.get(key):
                payload[key] = detail[key]
        self._events.publish(make_trace_event(EventName.ISSUE_BLOCKED, payload))

    def _publish_needs_human(
        self, session: Session, blocked_reason: str | None, detail: dict[str, Any]
    ) -> None:
        payload: dict[str, Any] = {
            "issue_number": session.issue.number,
            "agent": session.agent_label,
            "task": session.key.task.value,
            "rework_cycle": session.rework_cycle,
            "reason": blocked_reason or "Agent requested human input",
        }
        if detail.get("question"):
            payload["question"] = detail["question"]
        self._events.publish(make_trace_event(EventName.ISSUE_NEEDS_HUMAN, payload))
