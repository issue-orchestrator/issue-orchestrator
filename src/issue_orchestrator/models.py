"""Data models for issue-orchestrator."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class SessionStatus(Enum):
    """Status of a Claude session."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class IssueStatus(Enum):
    """Status of an issue in the queue."""
    AVAILABLE = "available"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"
    COMPLETED = "completed"


@dataclass
class Issue:
    """A GitHub issue."""
    number: int
    title: str
    labels: list[str]
    state: str = "open"  # "open" or "closed"
    milestone: Optional[str] = None
    body: Optional[str] = None

    @property
    def agent_type(self) -> Optional[str]:
        """Extract agent type from labels (e.g., 'agent:web')."""
        for label in self.labels:
            if label.startswith("agent:"):
                return label
        return None

    @property
    def priority(self) -> int:
        """Extract priority (lower = higher priority)."""
        if "priority:high" in self.labels:
            return 1
        elif "priority:medium" in self.labels:
            return 2
        elif "priority:low" in self.labels:
            return 3
        return 4

    @property
    def is_blocked(self) -> bool:
        return "blocked" in self.labels

    @property
    def is_in_progress(self) -> bool:
        return "in-progress" in self.labels

    @property
    def needs_human(self) -> bool:
        return "needs-human" in self.labels

    @property
    def display_name(self) -> str:
        """Return a formatted display name for the issue."""
        return f"#{self.number}: {self.title}"


@dataclass
class AgentConfig:
    """Configuration for an agent type."""
    prompt_path: Path
    worktree_base: Path
    model: str = "sonnet"
    timeout_minutes: int = 45
    repo_root: Optional[Path] = None  # Per-agent repo root override
    # Command template - {initial_prompt} is passed as positional arg to claude
    command: str = "claude --dangerously-skip-permissions --model {model} --append-system-prompt 'Read {prompt} for your instructions.' '{initial_prompt}'"
    initial_prompt: str = "Work on issue #{issue_number}: {issue_title}. Follow the instructions in {prompt}. When done, exit with /exit."

    def get_command(self, issue_number: int, issue_title: str, worktree: Path) -> str:
        """Render the command template with actual values, including initial prompt."""
        # First render the initial prompt
        rendered_prompt = self.initial_prompt.format(
            issue_number=issue_number,
            issue_title=issue_title,
            prompt=self.prompt_path,
            worktree=worktree,
            model=self.model,
        )
        # Escape single quotes in the prompt for shell safety
        escaped_prompt = rendered_prompt.replace("'", "'\\''")

        # Then render the full command with the prompt included
        return self.command.format(
            issue_number=issue_number,
            issue_title=issue_title,
            prompt=self.prompt_path,
            worktree=worktree,
            model=self.model,
            initial_prompt=escaped_prompt,
        )


@dataclass
class Session:
    """A running Claude session working on an issue."""
    issue: Issue
    agent_config: AgentConfig
    tmux_session_name: str
    worktree_path: Path
    branch_name: str
    started_at: datetime = field(default_factory=datetime.now)
    status: SessionStatus = SessionStatus.RUNNING

    @property
    def runtime_minutes(self) -> int:
        """How long this session has been running."""
        delta = datetime.now() - self.started_at
        return int(delta.total_seconds() / 60)

    @property
    def is_timed_out(self) -> bool:
        """Check if session exceeded timeout."""
        return self.runtime_minutes > self.agent_config.timeout_minutes


@dataclass
class OrchestratorState:
    """Persisted state of the orchestrator."""
    active_sessions: list[Session] = field(default_factory=list)
    completed_today: list[int] = field(default_factory=list)  # issue numbers
    paused: bool = False
    priority_queue: list[int] = field(default_factory=list)  # manual priority overrides


@dataclass
class CommentHeadings:
    """Configurable headings for worker comments on issues.

    These headings structure the comments workers post, allowing
    correlation with CTO investigation comments and other tooling.
    """
    implementation: str = "## Implementation"
    problems: str = "## Problems Encountered"
    pr_link: str = "## Pull Request"
    blocked: str = "## Blocked"
    needs_human: str = "## Needs Human Input"

    def format_completion_comment(
        self,
        implementation: str,
        problems: str | None,
        pr_url: str,
    ) -> str:
        """Format a completion comment with all sections."""
        lines = [
            self.implementation,
            implementation,
            "",
            self.problems,
            problems or "None",
            "",
            self.pr_link,
            pr_url,
        ]
        return "\n".join(lines)

    def format_blocked_comment(self, reason: str) -> str:
        """Format a blocked comment."""
        return f"{self.blocked}\n{reason}"

    def format_needs_human_comment(self, question: str) -> str:
        """Format a needs-human comment."""
        return f"{self.needs_human}\n{question}"
