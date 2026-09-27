"""Allocation-owned policy identity carried with a registered completion."""

from dataclasses import dataclass

from .completion_intake import CompletionIntakeError
from .models import CompletionRecord, sanitize_agent_label
from pathlib import Path
from .session_kind import HISTORICAL_AGENT_LABEL, SessionKind
from .session_run import RunContainedFile, SessionRunIdentity


@dataclass(frozen=True, slots=True)
class CompletionRunRole:
    issue_number: int
    kind: SessionKind
    agent_label: str

    def __post_init__(self) -> None:
        if type(self.kind) is not SessionKind:
            raise CompletionIntakeError("recorded completion kind is invalid")
        historical = self.kind is SessionKind.HISTORICAL
        if type(self.agent_label) is not str or historical != (self.agent_label == HISTORICAL_AGENT_LABEL):
            raise CompletionIntakeError(
                "recorded completion agent role is missing or invalid"
            )
        if not historical and not (
            self.agent_label.startswith("agent:") and self.agent_label.removeprefix("agent:").strip()
        ):
            raise CompletionIntakeError(
                "recorded completion agent role is missing or invalid"
            )
        if type(self.issue_number) is not int or self.issue_number <= 0:
            raise CompletionIntakeError("recorded completion issue is invalid")

    @classmethod
    def from_recorded(
        cls, issue_number: int, kind: SessionKind, agent_label: str | None
    ) -> "CompletionRunRole":
        if agent_label is None:
            raise CompletionIntakeError("recorded completion role is missing")
        return cls(issue_number, kind, agent_label)

    def require_processing_role(
        self, issue_number: int, supplied_label: str | None, tech_lead_label: str | None
    ) -> str:
        if issue_number != self.issue_number:
            raise CompletionIntakeError("receipt does not bind processing issue")
        if supplied_label is not None and supplied_label != self.agent_label:
            raise CompletionIntakeError("caller role differs from recorded allocation")
        # The allocator's rule, from the same owner (#7347 review r5): only the
        # issue-lane kinds are decided by the agent label, so a review by an
        # agent that is also the tech lead is still a review.
        if self.kind.contradicts_agent_role(self.agent_label, tech_lead_label):
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
    kind: SessionKind | None

    @classmethod
    def for_unprocessed_session(
        cls, agent_label: str | None, tech_lead_label: str | None,
    ) -> "CompletionProcessingPolicy":
        """Capture legacy session classification when no processor was invoked.

        ``agent_label`` is the SESSION's own role, settled by allocation at
        launch -- never ``Issue.agent_type``, which is whichever ``agent:``
        label the tracker happens to list first. A tech-lead run reading an
        issue that still carries its coder label classified as ordinary coding
        work, and the carried launch authority was bypassed (#7273 round 2
        finding 1).
        """
        kind = SessionKind.TECH_LEAD if agent_label is not None and agent_label == tech_lead_label else None
        return cls(agent_label, kind)

    @property
    def is_tech_lead(self) -> bool:
        return self.kind is SessionKind.TECH_LEAD

    def inheritable_launch_authority(
        self, run: "SessionRunIdentity | None"
    ) -> "SessionRunIdentity | None":
        """The run a validation retry of this one inherits authority from (#7273).

        Only a Tech Lead run is admitted by a create-once authority row, so only
        a Tech Lead retry names the run it came from. A retry that named a run
        with no row would be indistinguishable from one whose row has been lost,
        and the launcher refuses to relaunch the second -- so naming one for
        every retry would strand every ordinary retry in the queue.

        Takes the identity rather than the whole run assets because startup
        recovery has only the identity, read back off the manifest, and it has
        to reach the SAME answer as the live completion path. Recovery inferring
        its own rule from "the manifest had an identity" is how the two drifted
        and every recovered coder retry became unlaunchable (round 1 finding 2).
        """
        return run if self.is_tech_lead else None


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
            return CompletionProcessingPolicy(label, context.role.kind)
        label = supplied_label
        if label is None:
            label, error = self.legacy_label(completion_path)
            if error:
                raise CompletionIntakeError(error)
        return CompletionProcessingPolicy.for_unprocessed_session(label, self.tech_lead_label)
