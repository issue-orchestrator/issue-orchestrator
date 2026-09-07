"""Allocation-owned policy identity carried with a registered completion."""

from dataclasses import dataclass

from .completion_intake import CompletionIntakeError
from .models import CompletionRecord, sanitize_agent_label
from pathlib import Path
from .session_key import TaskKind
from .session_run import RunContainedFile


@dataclass(frozen=True, slots=True)
class CompletionRunRole:
    issue_number: int
    task: TaskKind
    agent_label: str

    def __post_init__(self) -> None:
        if type(self.task) is not TaskKind:
            raise CompletionIntakeError("recorded completion task is invalid")
        if (
            type(self.agent_label) is not str
            or not self.agent_label.startswith("agent:")
            or not self.agent_label.removeprefix("agent:").strip()
        ):
            raise CompletionIntakeError(
                "recorded completion agent role is missing or invalid"
            )
        if type(self.issue_number) is not int or self.issue_number <= 0:
            raise CompletionIntakeError("recorded completion issue is invalid")

    @classmethod
    def from_recorded(
        cls, issue_number: int, task: TaskKind | None, agent_label: str | None
    ) -> "CompletionRunRole":
        if task is None or agent_label is None:
            raise CompletionIntakeError("recorded completion role is missing")
        return cls(issue_number, task, agent_label)

    def require_processing_role(
        self, issue_number: int, supplied_label: str | None, tech_lead_label: str | None
    ) -> str:
        if issue_number != self.issue_number:
            raise CompletionIntakeError("receipt does not bind processing issue")
        if supplied_label is not None and supplied_label != self.agent_label:
            raise CompletionIntakeError("caller role differs from recorded allocation")
        if (self.task is TaskKind.TECH_LEAD) != (self.agent_label == tech_lead_label):
            raise CompletionIntakeError(
                "recorded Tech Lead role does not match configured launch policy"
            )
        return self.agent_label


@dataclass(frozen=True, slots=True)
class RegisteredCompletion:
    record: CompletionRecord
    role: CompletionRunRole
    artifact: RunContainedFile


@dataclass(frozen=True, slots=True)
class CompletionProcessingPolicy:
    """Role selected once for an invocation, independent of later settings edits."""

    agent_label: str | None
    task: TaskKind | None

    @classmethod
    def for_unprocessed_session(
        cls, agent_label: str | None, tech_lead_label: str | None,
    ) -> "CompletionProcessingPolicy":
        """Capture legacy session classification when no processor was invoked."""
        task = TaskKind.TECH_LEAD if agent_label is not None and agent_label == tech_lead_label else None
        return cls(agent_label, task)

    @property
    def is_tech_lead(self) -> bool:
        return self.task is TaskKind.TECH_LEAD


@dataclass(frozen=True, slots=True)
class CompletionRolePolicy:
    agent_labels: tuple[str, ...]
    tech_lead_label: str | None

    def legacy_label(
        self, completion_path: str | None
    ) -> tuple[str | None, str | None]:
        if completion_path is None:
            return None, None
        filename = Path(completion_path).name
        if not (filename.startswith("completion-") and filename.endswith(".json")):
            return None, None
        safe_name = filename[len("completion-") : -len(".json")]
        matches = [
            label
            for label in self.agent_labels
            if sanitize_agent_label(label) == safe_name
        ]
        if not matches:
            return None, None
        if len(matches) > 1:
            return (
                None,
                f"Multiple agent labels map to completion file {filename}: {', '.join(matches)}",
            )
        return matches[0], None

    def processing_policy(
        self,
        context: RegisteredCompletion | None,
        issue_number: int,
        supplied_label: str | None,
        completion_path: str | None,
    ) -> CompletionProcessingPolicy:
        if context is not None:
            label = context.role.require_processing_role(
                issue_number, supplied_label, self.tech_lead_label
            )
            return CompletionProcessingPolicy(label, context.role.task)
        label = supplied_label
        if label is None:
            label, error = self.legacy_label(completion_path)
            if error:
                raise CompletionIntakeError(error)
        return CompletionProcessingPolicy.for_unprocessed_session(label, self.tech_lead_label)
