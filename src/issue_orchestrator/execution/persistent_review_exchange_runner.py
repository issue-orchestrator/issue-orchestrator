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
from .persistent_session_exchange import run_persistent_session_exchange
from .reviewer_worktree import (
    create_reviewer_worktree,
    resolve_current_branch,
)


class PersistentReviewExchangeRunner:
    """Persistent-session implementation of :class:`ReviewExchangeRunner`.

    Constructed once at the composition root with the orchestrator's
    :class:`SessionOutput`, the issue-scoped pair registry, and the
    pair-scoped state directory root. Reused for every exchange.
    """

    def __init__(
        self,
        session_output: SessionOutput,
        pair_registry: InMemoryPersistentExchangePairRegistry,
        persistent_pair_root: Path,
    ) -> None:
        self._session_output = session_output
        self._pair_registry = pair_registry
        self._persistent_pair_root = persistent_pair_root

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
            persistent_pair_root=self._persistent_pair_root,
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
            initial_validation_record_path=initial_validation_record_path,
            web_port=web_port,
            events=events,
            event_context=event_context,
            on_started=on_started,
        )
