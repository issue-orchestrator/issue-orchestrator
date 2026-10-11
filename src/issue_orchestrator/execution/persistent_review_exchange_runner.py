"""``ReviewExchangeRunner`` implementation backed by the persistent-session runner.

Wraps the existing :func:`run_persistent_session_exchange` plus the
sibling-reviewer-worktree helpers so callers in ``control/`` can depend
on the :class:`ReviewExchangeRunner` port instead of reaching into the
execution layer.

The reviewer-worktree lifecycle (create lazily on first exchange,
fast-forward at the start of every reviewer round, remove when the
    pair is released at issue completion / reset / shutdown) lives
    entirely inside this implementation plus the registry's ``on_release``
    hook. The caller allocates the typed exchange run through
    ``SessionOutput`` and passes it in; this runner never resolves run
    directories by name.
"""

from __future__ import annotations

from ..ports.completion_intake import CompletionIntakeRuntime
from datetime import datetime, timezone
from pathlib import Path

from ..domain.models import AgentConfig
from ..domain.review_exchange import ReviewExchangeOutcome
from ..domain.review_exchange_run import ReviewExchangeRun
from ..domain.runtime_config import RuntimeConfigReference
from ..domain.review_validation import ReviewValidationEvidence
from ..events import EventContext
from ..ports.event_sink import EventSink
from ..ports.review_exchange_approval_gate import ReviewExchangeApprovalGate
from ..control.launch_prompt import NO_LAUNCH_PROMPT
from ..ports.launch_prompt import LaunchPromptProvider
from ..domain.launch_prompt import LaunchPromptUnavailable, PreparedLaunchPrompt
from ..domain.review_exchange_turn import ExchangeStandingRulings, Role
from ..domain.session_kind import SessionKind
from .persistent_exchange_pair_registry_inmemory import (
    InMemoryPersistentExchangePairRegistry,
)
from ..ports.session_output import SessionOutput
from .persistent_session_exchange import (
    review_exchange_supervisor_timeout_seconds,
    run_persistent_session_exchange,
)
from ..ports.turn_mailbox import TurnMailbox
from .review_exchange_response_channel import (
    ResponseChannel,
    ReviewExchangeResponseChannels,
)
from .reviewer_worktree import (
    create_reviewer_worktree,
    resolve_current_branch,
)


def persistent_pair_root_for_worktree(coder_worktree: Path) -> Path:
    """Return the attempt-scoped persistent-pair storage root.

    The repository engine owns the live pair registry, but durable pair
    artifacts are attempt-scoped: deleting the issue worktree must delete
    validation and recording state for that attempt. Keeping the root under
    the coder worktree preserves stable paths for live PTYs while making
    reset-from-scratch a real storage boundary.
    """
    return coder_worktree / ".issue-orchestrator" / "persistent-pairs"


_CODEX_LOOPBACK_BLOCKING_SANDBOXES = frozenset({"read-only", "workspace-write"})


def _codex_loopback_callbacks_blocked(agent: AgentConfig) -> bool:
    provider = (agent.provider or agent.ai_system or "").lower()
    if provider != "codex":
        return False
    approval_mode = str(agent.provider_args.get("approval_mode", "full-auto"))
    if approval_mode == "yolo":
        return False
    sandbox_value = agent.provider_args.get("sandbox")
    sandbox = (
        "workspace-write"
        if sandbox_value is None and approval_mode == "full-auto"
        else str(sandbox_value or "")
    )
    return sandbox in _CODEX_LOOPBACK_BLOCKING_SANDBOXES


def response_channel_for_agent(agent: AgentConfig) -> ResponseChannel:
    """Select the verdict transport supported by an exchange role's sandbox."""
    if _codex_loopback_callbacks_blocked(agent):
        return "file"
    return "mailbox"


class PersistentReviewExchangeRunner:
    """Persistent-session implementation of :class:`ReviewExchangeRunner`.

    Constructed once at the composition root with the orchestrator's
    :class:`SessionOutput` and issue-scoped pair registry. Reused for
    every exchange. Pair filesystem state is resolved per coder worktree
    at run time so worktree teardown clears attempt-scoped artifacts.
    """

    def __init__(
        self,
        session_output: SessionOutput,
        pair_registry: InMemoryPersistentExchangePairRegistry,
        *,
        completion_intake: CompletionIntakeRuntime,
        turn_mailbox: "TurnMailbox | None" = None,
        launch_prompt: LaunchPromptProvider = NO_LAUNCH_PROMPT,
    ) -> None:
        self._completion_intake = completion_intake
        self._session_output = session_output
        self._pair_registry = pair_registry
        # Read off the registry rather than injected separately: the registry
        # owns the recorder because the registry is what destroys the evidence,
        # and taking it from there makes it impossible to wire the round loop
        # and the teardown to two different recorders (#7141 finding 2).
        self._kill_evidence = pair_registry.kill_evidence
        self._turn_mailbox = turn_mailbox
        self._launch_prompt = launch_prompt

    def job_timeout_seconds(
        self,
        *,
        coder_agent: AgentConfig,
        reviewer_agent: AgentConfig,
        max_rounds: int,
    ) -> float | None:
        coder_timeout = coder_agent.timeout_minutes * 60
        reviewer_timeout = reviewer_agent.timeout_minutes * 60
        if coder_timeout <= 0 or reviewer_timeout <= 0:
            return None
        return review_exchange_supervisor_timeout_seconds(
            coder_timeout_seconds=coder_timeout,
            reviewer_timeout_seconds=reviewer_timeout,
            max_rounds=max_rounds,
        )

    def run(  # noqa: PLR0913
        self,
        *,
        exchange_run: ReviewExchangeRun,
        completion_capability: str,
        coder_worktree: Path,
        issue_number: int,
        issue_title: str,
        coder_label: str,
        reviewer_label: str,
        coder_agent: AgentConfig,
        reviewer_agent: AgentConfig,
        runtime_config: RuntimeConfigReference,
        max_rounds: int,
        max_no_progress: int,
        require_validation: bool,
        nit_policy: str = "surface",
        initial_validation_evidence: ReviewValidationEvidence | None = None,
        approval_gate: ReviewExchangeApprovalGate | None = None,
        web_port: int | None = None,
        events: EventSink | None = None,
        event_context: EventContext | None = None,
    ) -> ReviewExchangeOutcome:
        coder_branch = resolve_current_branch(coder_worktree)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        response_channels = ReviewExchangeResponseChannels.file_only()
        if self._turn_mailbox is not None:
            response_channels = ReviewExchangeResponseChannels(
                coder=response_channel_for_agent(coder_agent),
                reviewer=response_channel_for_agent(reviewer_agent),
            )

        def _make_reviewer_worktree() -> Path:
            # Invoked at most once per pair — only on cache miss
            # inside ``run_persistent_session_exchange``'s spawn
            # closure. Subsequent exchanges reuse the cached pair's
            # ``reviewer_worktree_path`` and the inner round-loop
            # fast-forwards it before each reviewer round.
            wt = create_reviewer_worktree(
                coder_worktree=coder_worktree,
                coder_branch=coder_branch,
                timestamp=timestamp,
            )
            return wt.path

        prepared_coder_prompt = self._prepared(SessionKind.REWORK, issue_number)

        def _rulings_for(role: Role) -> str | None:
            # Read fresh for every turn: a ruling recorded mid-exchange binds the next turn.
            kind = SessionKind.REWORK if role is Role.CODER else SessionKind.REVIEW
            return self._prepared(kind, issue_number).rulings

        return run_persistent_session_exchange(
            exchange_run=exchange_run,
            completion_capability=completion_capability,
            completion_intake=self._completion_intake,
            session_output=self._session_output,
            pair_registry=self._pair_registry,
            kill_evidence=self._kill_evidence,
            persistent_pair_root=persistent_pair_root_for_worktree(coder_worktree),
            coder_worktree_path=coder_worktree,
            reviewer_worktree_factory=_make_reviewer_worktree,
            coder_branch=coder_branch,
            issue_number=issue_number,
            issue_title=issue_title,
            coder_label=coder_label,
            reviewer_label=reviewer_label,
            coder_agent=coder_agent,
            reviewer_agent=reviewer_agent,
            runtime_config=runtime_config,
            max_rounds=max_rounds,
            max_no_progress=max_no_progress,
            require_validation=require_validation,
            nit_policy=nit_policy,
            initial_validation_evidence=initial_validation_evidence,
            approval_gate=approval_gate,
            web_port=web_port,
            events=events,
            event_context=event_context,
            turn_mailbox=self._turn_mailbox,
            response_channels=response_channels,
            coder_prompt_addendum=prepared_coder_prompt.addendum,
            standing_rulings=ExchangeStandingRulings(for_role=_rulings_for),
        )

    def _prepared(self, kind: SessionKind, issue_number: int) -> PreparedLaunchPrompt:
        """The launch-prompt additions for one exchange role, or a loud failure."""
        prepared = self._launch_prompt.prepare(kind=kind, issue_number=issue_number)
        if isinstance(prepared, LaunchPromptUnavailable):
            raise RuntimeError(f"Required launch prompt input unavailable: {prepared.reason}")
        return prepared
