"""``ReviewExchangeRunner`` implementation backed by the persistent-session runner.

Wraps the existing :func:`run_persistent_session_exchange` plus the
sibling-reviewer-worktree helpers so callers in ``control/`` can depend
on the :class:`ReviewExchangeRunner` port instead of reaching into the
execution layer.

The reviewer-worktree lifecycle (create lazily on first exchange,
fast-forward at the start of every reviewer round, remove when the
pair is released at issue completion / reset / shutdown) lives
entirely inside this implementation plus the registry's ``on_release``
hook. The caller's only contract is "hand me a coder worktree and
config; get back an outcome."
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..domain.models import AgentConfig
from ..domain.review_exchange import ReviewExchangeOutcome
from ..events import EventContext
from ..ports.event_sink import EventSink
from .persistent_exchange_pair_registry_inmemory import (
    InMemoryPersistentExchangePairRegistry,
)
from ..ports.session_output import SessionOutput
from .persistent_session_exchange import (
    review_exchange_supervisor_timeout_seconds,
    run_persistent_session_exchange,
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
    ) -> None:
        self._session_output = session_output
        self._pair_registry = pair_registry

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
        coder_worktree: Path,
        issue_number: int,
        issue_title: str,
        coder_label: str,
        reviewer_label: str,
        coder_agent: AgentConfig,
        reviewer_agent: AgentConfig,
        max_rounds: int,
        max_no_progress: int,
        require_validation: bool,
        nit_policy: str = "surface",
        parent_session_name: str | None = None,
        initial_validation_record_path: Path | None = None,
        web_port: int | None = None,
        events: EventSink | None = None,
        event_context: EventContext | None = None,
        on_started: Callable[[Path], None] | None = None,
    ) -> ReviewExchangeOutcome:
        coder_branch = resolve_current_branch(coder_worktree)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")

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

        return run_persistent_session_exchange(
            session_output=self._session_output,
            pair_registry=self._pair_registry,
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
            max_rounds=max_rounds,
            max_no_progress=max_no_progress,
            require_validation=require_validation,
            nit_policy=nit_policy,
            parent_session_name=parent_session_name,
            initial_validation_record_path=initial_validation_record_path,
            web_port=web_port,
            events=events,
            event_context=event_context,
            on_started=on_started,
        )
