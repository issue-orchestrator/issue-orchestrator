"""Board snapshot - typed orchestrator-state facts for triage/tech-lead sessions.

A ``BoardSnapshot`` is a serializable bundle of orchestrator-state facts
(active sessions, pending queues, blocked issues, recent failures, timeline
extracts, log tail) written to a file that a triage or tech-lead agent
session reads to understand the state of the board.

Flow:
1. Orchestrator: ``BoardSnapshotBuilder.build()`` -> ``BoardSnapshot``
2. Orchestrator: ``snapshot.write(path)`` into the session worktree
3. Agent: reads the snapshot file, reasons about the board, reports via
   ``coding-done``

The snapshot is a point-in-time fact bundle, not live state: it carries a
``generated_at`` timestamp and a ``schema_version`` so readers can reject
payloads they do not understand. ``from_dict`` fails fast on an unexpected
schema version or missing required keys - a malformed snapshot must never
be silently reinterpreted.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

BOARD_SNAPSHOT_SCHEMA_VERSION = 2

# Canonical snapshot filename inside a session's triage-data directory,
# next to TRIAGE_ASSIGNMENT_FILENAME (domain/triage_session.py).
BOARD_SNAPSHOT_FILENAME = "board-snapshot.json"


class BoardSessionInfoDict(TypedDict):
    """Serialized form of BoardSessionInfo."""

    issue_number: int
    issue_title: str
    agent_type: str
    session_type: str
    status: str
    started_at: str
    age_minutes: int
    terminal_id: str


class BoardQueueEntryDict(TypedDict):
    """Serialized form of BoardQueueEntry."""

    queue: str
    issue_number: int
    detail: str


class BoardBlockedByDict(TypedDict):
    """Serialized form of one blocking dependency."""

    number: int
    title: str
    state: str


class BoardBlockedIssueDict(TypedDict):
    """Serialized form of BoardBlockedIssue."""

    issue_number: int
    issue_title: str
    summary: str
    blocked_by: list[BoardBlockedByDict]


class BoardFailureDict(TypedDict):
    """Serialized form of BoardFailure."""

    issue_number: int
    issue_title: str
    failure_reason: str
    artifact_hints: list[str]


class BoardTimelineExtractDict(TypedDict):
    """Serialized form of BoardTimelineExtract."""

    issue_number: int
    records: list[dict[str, Any]]


class BoardCaseFileDict(TypedDict):
    issue_number: int
    title: str
    comment_count: int
    updated_at: str
    area: str


class BoardAreaSignalDict(TypedDict):
    area: str
    distinct_patterns: int
    shipped_fixes: int


class BoardShippedFixDict(TypedDict):
    issue_number: int
    title: str
    pr_url: str
    area: str
    merged_at: str


class BoardSnapshotDict(TypedDict):
    """Serialized form of BoardSnapshot."""

    schema_version: int
    generated_at: str
    orchestrator_paused: bool
    sessions: list[BoardSessionInfoDict]
    queues: list[BoardQueueEntryDict]
    blocked_issues: list[BoardBlockedIssueDict]
    recent_failures: list[BoardFailureDict]
    case_files: list[BoardCaseFileDict]
    area_signals: list[BoardAreaSignalDict]
    recent_shipped_fixes: list[BoardShippedFixDict]
    timeline: list[BoardTimelineExtractDict]
    log_tail: list[str]


@dataclass
class BoardSessionInfo:
    """An active agent session, as seen on the board.

    ``agent_type`` is the issue's ``agent:*`` label; empty string when the
    issue carries no agent label (a legitimate state, not an error).
    ``age_minutes`` is computed by the builder from an injected clock so
    snapshots are deterministic and testable.
    """

    issue_number: int
    issue_title: str
    agent_type: str
    session_type: str
    status: str
    started_at: str  # ISO timestamp
    age_minutes: int
    terminal_id: str


@dataclass
class BoardQueueEntry:
    """One entry in a pending queue.

    ``queue`` names the source queue on OrchestratorState (e.g.
    "pending_reviews", "pending_reworks", "pending_triage",
    "pending_validation_retries", "priority_queue"). ``detail`` is a short
    human-readable elaboration; may be "" (e.g. priority_queue entries).
    """

    queue: str
    issue_number: int
    detail: str


@dataclass
class BoardBlockedIssue:
    """An issue blocked on unresolved dependencies.

    ``blocked_by`` lists (dependency number, title, state) tuples, mirroring
    ``DependencyProblem.blocked_by``.
    """

    issue_number: int
    issue_title: str
    summary: str
    blocked_by: list[tuple[int, str, str]]


@dataclass
class BoardFailure:
    """A recently failed session, pending triage attention.

    ``artifact_hints`` lists paths to session artifacts worth inspecting;
    may be empty when the failure source carries no artifact information.
    """

    issue_number: int
    issue_title: str
    failure_reason: str
    artifact_hints: list[str]


@dataclass
class BoardTimelineExtract:
    """Recent timeline records for one issue.

    Each record is a plain dict mirroring the fields of
    ``ports.timeline_store.TimelineRecord`` that matter to a board reader:
    ``event_id``, ``timestamp``, ``event``, ``data``.
    """

    issue_number: int
    records: list[dict[str, Any]]


@dataclass
class BoardCaseFile:
    """An open signature-keyed pattern evidence ledger (#6781)."""
    issue_number: int
    title: str
    comment_count: int
    updated_at: str
    area: str


@dataclass
class BoardAreaSignal:
    """Step-back evidence for one component/seam (#6781 amendment)."""
    area: str
    distinct_patterns: int
    shipped_fixes: int


@dataclass
class BoardShippedFix:
    """Restart-safe patch evidence for an area-tagged merged issue."""
    issue_number: int
    title: str
    pr_url: str
    area: str
    merged_at: str


@dataclass
class BoardSnapshot:
    """Point-in-time bundle of orchestrator-state facts for an agent session.

    Created by ``control.board_snapshot_builder.BoardSnapshotBuilder``,
    written to a file, read by a triage/tech-lead agent. All list fields are
    bounded by the builder (the file is consumed by an agent with a finite
    context window); see the builder's docstrings for the exact caps.
    """

    generated_at: str  # ISO timestamp
    orchestrator_paused: bool
    schema_version: int = BOARD_SNAPSHOT_SCHEMA_VERSION
    sessions: list[BoardSessionInfo] = field(default_factory=list)
    queues: list[BoardQueueEntry] = field(default_factory=list)
    blocked_issues: list[BoardBlockedIssue] = field(default_factory=list)
    recent_failures: list[BoardFailure] = field(default_factory=list)
    case_files: list[BoardCaseFile] = field(default_factory=list)
    area_signals: list[BoardAreaSignal] = field(default_factory=list)
    recent_shipped_fixes: list[BoardShippedFix] = field(default_factory=list)
    timeline: list[BoardTimelineExtract] = field(default_factory=list)
    log_tail: list[str] = field(default_factory=list)

    def to_dict(self) -> BoardSnapshotDict:
        """Convert to JSON-serializable dict."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "orchestrator_paused": self.orchestrator_paused,
            "sessions": [
                {
                    "issue_number": s.issue_number,
                    "issue_title": s.issue_title,
                    "agent_type": s.agent_type,
                    "session_type": s.session_type,
                    "status": s.status,
                    "started_at": s.started_at,
                    "age_minutes": s.age_minutes,
                    "terminal_id": s.terminal_id,
                }
                for s in self.sessions
            ],
            "queues": [
                {
                    "queue": q.queue,
                    "issue_number": q.issue_number,
                    "detail": q.detail,
                }
                for q in self.queues
            ],
            "blocked_issues": [
                {
                    "issue_number": b.issue_number,
                    "issue_title": b.issue_title,
                    "summary": b.summary,
                    "blocked_by": [
                        {"number": number, "title": title, "state": state}
                        for number, title, state in b.blocked_by
                    ],
                }
                for b in self.blocked_issues
            ],
            "recent_failures": [
                {
                    "issue_number": f.issue_number,
                    "issue_title": f.issue_title,
                    "failure_reason": f.failure_reason,
                    "artifact_hints": list(f.artifact_hints),
                }
                for f in self.recent_failures
            ],
            "case_files": [
                {
                    "issue_number": item.issue_number,
                    "title": item.title,
                    "comment_count": item.comment_count,
                    "updated_at": item.updated_at,
                    "area": item.area,
                }
                for item in self.case_files
            ],
            "area_signals": [
                {
                    "area": item.area,
                    "distinct_patterns": item.distinct_patterns,
                    "shipped_fixes": item.shipped_fixes,
                }
                for item in self.area_signals
            ],
            "recent_shipped_fixes": [
                {
                    "issue_number": item.issue_number,
                    "title": item.title,
                    "pr_url": item.pr_url,
                    "area": item.area,
                    "merged_at": item.merged_at,
                }
                for item in self.recent_shipped_fixes
            ],
            "timeline": [
                {
                    "issue_number": t.issue_number,
                    "records": [dict(record) for record in t.records],
                }
                for t in self.timeline
            ],
            "log_tail": list(self.log_tail),
        }

    @classmethod
    def from_dict(cls, data: BoardSnapshotDict) -> "BoardSnapshot":
        """Load from dict. Fails fast on schema drift or missing keys.

        Raises:
            ValueError: if ``schema_version`` is not the supported version.
            KeyError: if any required key is missing.
        """
        schema_version = data["schema_version"]
        if schema_version != BOARD_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported board snapshot schema_version {schema_version!r}; "
                f"this reader supports schema_version {BOARD_SNAPSHOT_SCHEMA_VERSION}"
            )
        return cls(
            schema_version=schema_version,
            generated_at=data["generated_at"],
            orchestrator_paused=data["orchestrator_paused"],
            sessions=[
                BoardSessionInfo(
                    issue_number=s["issue_number"],
                    issue_title=s["issue_title"],
                    agent_type=s["agent_type"],
                    session_type=s["session_type"],
                    status=s["status"],
                    started_at=s["started_at"],
                    age_minutes=s["age_minutes"],
                    terminal_id=s["terminal_id"],
                )
                for s in data["sessions"]
            ],
            queues=[
                BoardQueueEntry(
                    queue=q["queue"],
                    issue_number=q["issue_number"],
                    detail=q["detail"],
                )
                for q in data["queues"]
            ],
            blocked_issues=[
                BoardBlockedIssue(
                    issue_number=b["issue_number"],
                    issue_title=b["issue_title"],
                    summary=b["summary"],
                    blocked_by=[
                        (dep["number"], dep["title"], dep["state"])
                        for dep in b["blocked_by"]
                    ],
                )
                for b in data["blocked_issues"]
            ],
            recent_failures=[
                BoardFailure(
                    issue_number=f["issue_number"],
                    issue_title=f["issue_title"],
                    failure_reason=f["failure_reason"],
                    artifact_hints=list(f["artifact_hints"]),
                )
                for f in data["recent_failures"]
            ],
            case_files=[
                BoardCaseFile(
                    issue_number=item["issue_number"],
                    title=item["title"],
                    comment_count=item["comment_count"],
                    updated_at=item["updated_at"],
                    area=item["area"],
                )
                for item in data["case_files"]
            ],
            area_signals=[
                BoardAreaSignal(
                    area=item["area"],
                    distinct_patterns=item["distinct_patterns"],
                    shipped_fixes=item["shipped_fixes"],
                )
                for item in data["area_signals"]
            ],
            recent_shipped_fixes=[
                BoardShippedFix(
                    issue_number=item["issue_number"],
                    title=item["title"],
                    pr_url=item["pr_url"],
                    area=item["area"],
                    merged_at=item["merged_at"],
                )
                for item in data["recent_shipped_fixes"]
            ],
            timeline=[
                BoardTimelineExtract(
                    issue_number=t["issue_number"],
                    records=[dict(record) for record in t["records"]],
                )
                for t in data["timeline"]
            ],
            log_tail=list(data["log_tail"]),
        )

    def write(self, path: Path) -> None:
        """Write the snapshot to ``path`` as JSON, creating parent dirs."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        logger.info(
            "[board] Snapshot written: %s (%d sessions, %d queue entries)",
            path,
            len(self.sessions),
            len(self.queues),
        )

    @classmethod
    def read(cls, path: Path) -> "BoardSnapshot":
        """Read a snapshot from ``path``. Fails fast on unreadable payloads."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)
