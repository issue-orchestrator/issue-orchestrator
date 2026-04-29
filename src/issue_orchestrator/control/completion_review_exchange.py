"""Review-exchange orchestration for completion processing."""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..domain.models import CompletionRecord, RequestedAction
from ..ports.background_job import NullBackgroundJobRunner
from ..ports.session_output import SessionOutput
from .background_job_supervisor import BackgroundJobSupervisor
from .review_publish_pipeline import resolve_review_publish_pipeline

if TYPE_CHECKING:
    from ..infra.config import Config
    from .review_exchange_loop import ReviewExchangeOutcome

logger = logging.getLogger(__name__)

ReviewStartedEmitter = Callable[..., None]
ReviewOutcomeEmitter = Callable[..., None]
RunReviewExchangeLoop = Callable[..., "ReviewExchangeOutcome"]


def _review_exchange_job_id(issue_number: int, session_name: str | None) -> str:
    """Stable job identity for a per-issue review exchange.

    Using (issue_number, session_name) keeps retries on the same coding run
    collapsed onto a single background thread while still letting a fresh
    coding run (new session_name) start its own exchange.
    """
    base = f"review-exchange:{issue_number}"
    return f"{base}:{session_name}" if session_name else base


def _cached_review_event_metadata(exchange_outcome: "ReviewExchangeOutcome") -> dict[str, str]:
    return dict(exchange_outcome.cache_metadata or {})


class CompletionReviewExchange:
    """Owns review-exchange mode selection, caching, execution, and artifacts."""

    def __init__(
        self,
        *,
        config: "Config | None",
        session_output: SessionOutput,
        emit_review_started: ReviewStartedEmitter,
        emit_review_outcome: ReviewOutcomeEmitter,
        job_supervisor: BackgroundJobSupervisor | None = None,
    ) -> None:
        self._config = config
        self._session_output = session_output
        self._emit_review_started = emit_review_started
        self._emit_review_outcome = emit_review_outcome
        # Supervisor injection is REQUIRED for the async failure path to work:
        # ``take_failure`` only returns values that ``tick()`` has populated,
        # and ``tick()`` must be called from the orchestrator's main loop.
        # Tests that exercise the async path inject a real supervisor and
        # drive ``tick()`` themselves. Tests that don't care about async pass
        # nothing — we default to a supervisor wrapping NullBackgroundJobRunner
        # so ``submit``/``is_running`` return no-op values and the failure
        # queue is always empty (no need to tick).
        self._job_supervisor = job_supervisor or BackgroundJobSupervisor(
            NullBackgroundJobRunner()
        )

    def prepare_review_exchange(
        self,
        *,
        requested_actions: tuple[RequestedAction, ...],
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
        record: CompletionRecord,
        errors: list[str],
        actions_taken: list[str],
        run_review_exchange_loop: RunReviewExchangeLoop,
        review_cache_boundary_started_at: str | None = None,
    ) -> tuple[Any, str | None, ReviewExchangeOutcome | None, bool, bool, bool]:
        """Resolve mode, run/poll review exchange, and report status.

        Returns (plan, exchange_mode, exchange_result, review_exchange_completed,
        exchange_halt, deferred). ``deferred=True`` means the exchange is
        running in the background and completion processing must retry on a
        later tick — the caller MUST NOT proceed to push/PR creation.
        """
        exchange_mode: str | None = None
        exchange_result: ReviewExchangeOutcome | None = None
        review_exchange_completed = False

        if RequestedAction.CREATE_PR in requested_actions:
            try:
                exchange_mode = self.resolve_review_exchange_mode(agent_label)
            except ValueError as exc:
                errors.append(f"review_exchange: {exc}")
                pipeline = resolve_review_publish_pipeline(None)
                return pipeline.plan(requested_actions), None, None, False, True, False

        pipeline = resolve_review_publish_pipeline(exchange_mode)
        plan = pipeline.plan(requested_actions)
        if not plan.run_review_exchange_before_publish:
            return (
                plan,
                exchange_mode,
                exchange_result,
                review_exchange_completed,
                False,
                False,
            )

        (
            exchange_mode,
            exchange_result,
            exchange_halt,
            deferred,
        ) = self.run_review_exchange_if_needed(
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=agent_label,
            initial_validation_record_path=(
                Path(record.validation_record_path) if record.validation_record_path else None
            ),
            review_cache_boundary_started_at=review_cache_boundary_started_at,
            errors=errors,
            actions_taken=actions_taken,
            run_review_exchange_loop=run_review_exchange_loop,
        )
        if exchange_mode in {"via-mcp", "via-local-loop"} and exchange_result:
            review_exchange_completed = True
        return (
            plan,
            exchange_mode,
            exchange_result,
            review_exchange_completed,
            exchange_halt,
            deferred,
        )

    def resolve_create_pr_exchange_mode(
        self,
        *,
        exchange_mode: str | None,
        agent_label: str | None,
        errors: list[str],
    ) -> tuple[str | None, bool]:
        if exchange_mode is not None:
            return exchange_mode, False
        try:
            return self.resolve_review_exchange_mode(agent_label), False
        except ValueError as exc:
            errors.append(f"review_exchange: {exc}")
            return None, True

    def missing_review_exchange_outcome(
        self,
        exchange_mode: str | None,
        exchange_result: ReviewExchangeOutcome | None,
    ) -> bool:
        return exchange_mode in {"via-mcp", "via-local-loop"} and exchange_result is None

    def is_review_exchange_running(
        self,
        *,
        issue_number: int,
        session_name: str | None,
    ) -> bool:
        """Report whether a background review-exchange job is in flight.

        The reroute budget on the consumer side uses this to distinguish
        polling ticks (no new work, just waiting for an existing job) from
        fresh attempts. Counting polling ticks would let a slow exchange
        exhaust the budget before it finishes.
        """
        job_id = _review_exchange_job_id(issue_number, session_name)
        return self._job_supervisor.is_running(job_id)

    def run_review_exchange_if_needed(
        self,
        *,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
        initial_validation_record_path: Path | None,
        errors: list[str],
        actions_taken: list[str],
        run_review_exchange_loop: RunReviewExchangeLoop,
        review_cache_boundary_started_at: str | None = None,
    ) -> tuple[str | None, ReviewExchangeOutcome | None, bool, bool]:
        """Return (exchange_mode, outcome, halt, deferred).

        ``deferred=True`` means the exchange is running asynchronously; the
        caller must stop processing this completion record and retry on a
        later tick. ``outcome`` is populated only when the exchange has
        finished — from the on-disk cache of a prior background run, or
        from an inline run when no background runner is wired (tests, dev
        environments).
        """
        try:
            exchange_mode = self.resolve_review_exchange_mode(agent_label)
        except ValueError as exc:
            errors.append(f"review_exchange: {exc}")
            return None, None, True, False
        if exchange_mode not in {"via-mcp", "via-local-loop"}:
            return exchange_mode, None, False, False
        reviewer_label = self.resolve_reviewer_label(agent_label) if agent_label else None
        require_validation = bool(
            self._config and self._config.review_exchange_require_validation
        )
        existing_outcome = self.load_existing_review_exchange_outcome(
            worktree,
            session_name,
            require_validation=require_validation,
            current_validation_record_path=initial_validation_record_path,
            not_before_started_at=review_cache_boundary_started_at,
        )
        if existing_outcome:
            mode, outcome, halt = self._handle_cached_review_exchange_outcome(
                exchange_mode=exchange_mode,
                existing_outcome=existing_outcome,
                issue_number=issue_number,
                session_name=session_name,
                worktree=worktree,
                reviewer_label=reviewer_label,
                errors=errors,
                actions_taken=actions_taken,
            )
            return mode, outcome, halt, False

        job_id = _review_exchange_job_id(issue_number, session_name)

        # If a previous background attempt raised, the supervisor recorded
        # it. Surface that as a terminal halt rather than spawning another
        # attempt — otherwise a crashing job would forkbomb the tick loop
        # at one resubmit per iteration forever.
        failure = self._job_supervisor.take_failure(job_id)
        if failure is not None:
            reason = (
                f"review_exchange: background job raised: {failure.error}"
            )
            errors.append(reason)
            logger.error(
                "[REVIEW_EXCHANGE] background job failed; halting issue=%d job_id=%s",
                issue_number,
                job_id,
                exc_info=(type(failure.error), failure.error, None),
            )
            return exchange_mode, None, True, False

        if self._job_supervisor.is_running(job_id):
            logger.info(
                "[REVIEW_EXCHANGE] job still running issue=%d job_id=%s — deferring",
                issue_number,
                job_id,
            )
            return exchange_mode, None, False, True

        logger.info("Review exchange mode selected: %s", exchange_mode)
        submitted = self._submit_background_review_exchange(
            job_id=job_id,
            exchange_mode=exchange_mode,
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=agent_label,
            reviewer_label=reviewer_label,
            initial_validation_record_path=initial_validation_record_path,
            run_review_exchange_loop=run_review_exchange_loop,
        )
        if submitted:
            # Deferred: the exchange is now running off the main tick. A
            # subsequent tick will re-enter this path, find summary.json via
            # load_existing_review_exchange_outcome, and resume publish.
            return exchange_mode, None, False, True

        # No background runner wired — fall back to the legacy synchronous
        # path so tests and dev environments without a job runner still work.
        logger.debug(
            "[REVIEW_EXCHANGE] no background runner; running exchange inline"
        )
        mode, outcome, halt = self._run_fresh_review_exchange(
            exchange_mode=exchange_mode,
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=agent_label,
            reviewer_label=reviewer_label,
            initial_validation_record_path=initial_validation_record_path,
            errors=errors,
            actions_taken=actions_taken,
            run_review_exchange_loop=run_review_exchange_loop,
        )
        return mode, outcome, halt, False

    def _submit_background_review_exchange(
        self,
        *,
        job_id: str,
        exchange_mode: str,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
        reviewer_label: str | None,
        initial_validation_record_path: Path | None,
        run_review_exchange_loop: RunReviewExchangeLoop,
    ) -> bool:
        """Start the review exchange in a background job; return True if accepted.

        The background closure mirrors ``_run_fresh_review_exchange`` up to the
        point of emitting the *started* event and handing off to
        ``run_review_exchange_loop``. The loop writes ``summary.json`` when it
        finishes, which is the signal the next tick uses to resume processing.
        """

        def _job() -> None:
            review_started_run_dir: Path | None = None

            def _on_review_exchange_started(started_run_dir: Path) -> None:
                nonlocal review_started_run_dir
                review_started_run_dir = started_run_dir
                self._emit_review_started(
                    issue_number=issue_number,
                    reviewer_label=reviewer_label,
                    exchange_mode=exchange_mode,
                    run_dir=started_run_dir,
                )

            try:
                outcome = run_review_exchange_loop(
                    worktree=worktree,
                    issue_number=issue_number,
                    issue_title=issue_title,
                    session_name=session_name,
                    agent_label=agent_label,
                    initial_validation_record_path=initial_validation_record_path,
                    on_started=_on_review_exchange_started,
                )
            except Exception:
                logger.exception(
                    "[REVIEW_EXCHANGE] background job failed: issue=%d job_id=%s",
                    issue_number,
                    job_id,
                )
                raise

            # Persist the summary so the next tick can observe completion.
            # resolve_required_review_run_dir + store_review_exchange_summary
            # together write the same marker as the synchronous path would.
            if review_started_run_dir is None:
                review_started_run_dir = self.resolve_review_exchange_run_dir(
                    exchange_outcome=outcome,
                    worktree=worktree,
                    session_name=session_name,
                )
            self.store_review_exchange_summary(
                worktree=worktree,
                session_name=session_name,
                exchange_result=outcome,
            )

        return self._job_supervisor.submit(job_id, _job)

    def _handle_cached_review_exchange_outcome(
        self,
        *,
        exchange_mode: str,
        existing_outcome: ReviewExchangeOutcome,
        issue_number: int,
        session_name: str | None,
        worktree: Path,
        reviewer_label: str | None,
        errors: list[str],
        actions_taken: list[str],
    ) -> tuple[str, ReviewExchangeOutcome, bool]:
        review_run_dir = self.resolve_required_review_run_dir(
            exchange_outcome=existing_outcome,
            worktree=worktree,
            session_name=session_name,
            issue_number=issue_number,
        )
        cache_metadata = _cached_review_event_metadata(existing_outcome)
        self._emit_review_started(
            issue_number=issue_number,
            reviewer_label=reviewer_label,
            exchange_mode=exchange_mode,
            run_dir=review_run_dir,
            cached=True,
            **cache_metadata,
        )
        if existing_outcome.status == "ok":
            actions_taken.append("Review exchange passed (cached)")
            reviewer_summary = (
                existing_outcome.reviewer_response.response_text
                if getattr(existing_outcome, "reviewer_response", None)
                else "Review approved via cached exchange summary"
            )
            self._emit_review_outcome(
                issue_number=issue_number,
                reviewer_label=reviewer_label,
                exchange_mode=exchange_mode,
                approved=True,
                rounds=getattr(existing_outcome, "rounds", None),
                summary=reviewer_summary,
                run_dir=review_run_dir,
                cached=True,
                **cache_metadata,
            )
            return exchange_mode, existing_outcome, False
        self._emit_review_outcome(
            issue_number=issue_number,
            reviewer_label=reviewer_label,
            exchange_mode=exchange_mode,
            approved=False,
            rounds=getattr(existing_outcome, "rounds", None),
            summary=f"Review exchange halted: {existing_outcome.reason}",
            run_dir=review_run_dir,
            cached=True,
            **cache_metadata,
        )
        errors.append(f"review_exchange: {existing_outcome.status} ({existing_outcome.reason})")
        return exchange_mode, existing_outcome, True

    def _run_fresh_review_exchange(
        self,
        *,
        exchange_mode: str,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
        reviewer_label: str | None,
        initial_validation_record_path: Path | None,
        errors: list[str],
        actions_taken: list[str],
        run_review_exchange_loop: RunReviewExchangeLoop,
    ) -> tuple[str, ReviewExchangeOutcome, bool]:
        review_started_run_dir: Path | None = None

        def _on_review_exchange_started(started_run_dir: Path) -> None:
            nonlocal review_started_run_dir
            review_started_run_dir = started_run_dir
            self._emit_review_started(
                issue_number=issue_number,
                reviewer_label=reviewer_label,
                exchange_mode=exchange_mode,
                run_dir=started_run_dir,
            )

        exchange_result = run_review_exchange_loop(
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            agent_label=agent_label,
            initial_validation_record_path=initial_validation_record_path,
            on_started=_on_review_exchange_started,
        )
        review_run_dir = self.resolve_required_review_run_dir(
            exchange_outcome=exchange_result,
            worktree=worktree,
            session_name=session_name,
            issue_number=issue_number,
        )
        if review_started_run_dir is None:
            self._emit_review_started(
                issue_number=issue_number,
                reviewer_label=reviewer_label,
                exchange_mode=exchange_mode,
                run_dir=review_run_dir,
            )
        if exchange_result.status != "ok":
            self._emit_review_outcome(
                issue_number=issue_number,
                reviewer_label=reviewer_label,
                exchange_mode=exchange_mode,
                approved=False,
                rounds=getattr(exchange_result, "rounds", None),
                summary=f"Review exchange halted: {exchange_result.reason}",
                run_dir=review_run_dir,
            )
            errors.append(f"review_exchange: {exchange_result.status} ({exchange_result.reason})")
            return exchange_mode, exchange_result, True

        actions_taken.append("Review exchange passed")
        reviewer_summary = (
            exchange_result.reviewer_response.response_text
            if exchange_result.reviewer_response
            else "Review approved"
        )
        self._emit_review_outcome(
            issue_number=issue_number,
            reviewer_label=reviewer_label,
            exchange_mode=exchange_mode,
            approved=True,
            rounds=exchange_result.rounds,
            summary=reviewer_summary,
            run_dir=review_run_dir,
        )
        self.store_review_exchange_summary(
            worktree=worktree,
            session_name=session_name,
            exchange_result=exchange_result,
        )
        return exchange_mode, exchange_result, False

    def resolve_required_review_run_dir(
        self,
        *,
        exchange_outcome: ReviewExchangeOutcome | None,
        worktree: Path,
        session_name: str | None,
        issue_number: int,
    ) -> Path:
        review_run_dir = self.resolve_review_exchange_run_dir(
            exchange_outcome=exchange_outcome,
            worktree=worktree,
            session_name=session_name,
        )
        if review_run_dir is None:
            raise RuntimeError(
                f"review.started requires run_dir: issue={issue_number} session={session_name}"
            )
        return review_run_dir

    def store_review_exchange_summary(
        self,
        *,
        worktree: Path,
        session_name: str | None,
        exchange_result: ReviewExchangeOutcome,
    ) -> None:
        if not exchange_result.summary:
            return
        review_run_dir = self.resolve_review_exchange_run_dir(
            exchange_outcome=exchange_result,
            worktree=worktree,
            session_name=session_name,
        )
        if review_run_dir is None:
            return
        review_session_name = review_run_dir.name.split("__", 1)[-1]
        validation_record_path: Path | None = None
        if exchange_result.exchange_dir:
            # The record may be written by reviewer loop or by validation gate later.
            validation_record_path = exchange_result.exchange_dir.parent / "validation-record.json"
        self._session_output.store_review_exchange_summary(
            worktree,
            review_session_name,
            exchange_result.summary,
            validation_record_path=validation_record_path,
        )

    def resolve_review_exchange_run_dir(
        self,
        *,
        exchange_outcome: ReviewExchangeOutcome | None,
        worktree: Path,
        session_name: str | None,
    ) -> Path | None:
        """Resolve run_dir for review-exchange lifecycle events.

        Prefer the dedicated review-exchange run dir; fall back to session run dir.
        """
        exchange_dir = getattr(exchange_outcome, "exchange_dir", None) if exchange_outcome else None
        if exchange_dir:
            try:
                return Path(exchange_dir).parent
            except TypeError:
                logger.debug("Invalid exchange_dir on review exchange outcome: %r", exchange_dir)
        if session_name:
            return self._session_output.find_run_dir(worktree, session_name)
        return None

    def load_existing_review_exchange_outcome(
        self,
        worktree: Path,
        session_name: str | None,
        *,
        require_validation: bool,
        current_validation_record_path: Path | None = None,
        not_before_started_at: str | None = None,
    ) -> ReviewExchangeOutcome | None:
        if not session_name:
            return None
        cached = self._session_output.load_review_exchange_summary(
            worktree,
            session_name,
            not_before_started_at=not_before_started_at,
        )
        if not cached:
            return None
        cached_head_sha = self._validation_head_sha(cached.validation_record_path)
        current_head_sha = self._validation_head_sha(current_validation_record_path)
        logger.info(
            "[REVIEW_EXCHANGE] Evaluating cached summary: session=%s summary=%s "
            "validation=%s cached_head_sha=%s current_head_sha=%s require_validation=%s "
            "boundary=%s",
            session_name,
            cached.summary_path,
            cached.validation_record_path,
            cached_head_sha or "(none)",
            current_head_sha or "(none)",
            require_validation,
            not_before_started_at or "(none)",
        )
        if require_validation and not self.review_exchange_validation_passed(
            cached.validation_record_path
        ):
            logger.info(
                "[REVIEW_EXCHANGE] Ignoring cached summary without passing validation: "
                "session=%s summary=%s validation=%s",
                session_name,
                cached.summary_path,
                cached.validation_record_path,
            )
            return None
        if require_validation and not current_head_sha:
            logger.info(
                "[REVIEW_EXCHANGE] Ignoring cached summary without current validation head_sha: "
                "session=%s summary=%s current_validation=%s",
                session_name,
                cached.summary_path,
                current_validation_record_path,
            )
            return None
        if not self._review_exchange_validation_matches_current(
            cached.validation_record_path,
            current_validation_record_path,
        ):
            logger.info(
                "[REVIEW_EXCHANGE] Ignoring cached summary due to head_sha mismatch: "
                "session=%s summary=%s cached_head_sha=%s current_head_sha=%s",
                session_name,
                cached.summary_path,
                cached_head_sha or "(none)",
                current_head_sha or "(none)",
            )
            return None
        # Same commit, cached approval, but the *current* validation explicitly
        # failed — the prior approval no longer holds. Replaying the cached
        # "ok" here is the bug that loops the validation-failed reroute: the
        # caller would treat the cached approval as authoritative and never
        # invoke the coder to fix the failure. Force a fresh exchange instead.
        if self._current_validation_explicitly_failed(current_validation_record_path):
            logger.info(
                "[REVIEW_EXCHANGE] Ignoring cached summary because current validation failed: "
                "session=%s summary=%s current_validation=%s",
                session_name,
                cached.summary_path,
                current_validation_record_path,
            )
            return None
        status = cached.summary.get("status")
        rounds = cached.summary.get("completed_rounds")
        if not status or rounds is None:
            return None
        cache_metadata = {
            "review_cache_summary_path": str(cached.summary_path),
        }
        if cached.validation_record_path:
            cache_metadata["review_cache_validation_record_path"] = str(
                cached.validation_record_path
            )
        if cached_head_sha:
            cache_metadata["review_cache_head_sha"] = cached_head_sha
        logger.info(
            "[REVIEW_EXCHANGE] Reusing cached summary: session=%s summary=%s status=%s "
            "rounds=%s head_sha=%s",
            session_name,
            cached.summary_path,
            status,
            rounds,
            cached_head_sha or "(none)",
        )
        from .review_exchange_loop import ReviewExchangeOutcome, ReviewExchangeResponse

        return ReviewExchangeOutcome(
            status=status,
            rounds=rounds,
            reason="cached_summary",
            reviewer_response=ReviewExchangeResponse(
                response_type=status,
                response_text=cached.summary.get("response_text") or "",
            ),
            exchange_dir=cached.exchange_dir,
            summary=dict(cached.summary),
            cache_metadata=cache_metadata,
        )

    @staticmethod
    def review_exchange_validation_passed(record_path: Path | None) -> bool:
        if not record_path or not record_path.exists():
            return False
        try:
            data = json.loads(record_path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return bool(data.get("passed"))

    @staticmethod
    def _current_validation_explicitly_failed(record_path: Path | None) -> bool:
        """Return True iff the record exists and explicitly says ``passed=False``.

        Distinct from ``not review_exchange_validation_passed``: a missing or
        unreadable record is treated as "no signal" (False), so this only
        rejects the cache when the caller produced a definitive failure.
        """
        if not record_path or not record_path.exists():
            return False
        try:
            data = json.loads(record_path.read_text())
        except (OSError, json.JSONDecodeError):
            return False
        return data.get("passed") is False

    @classmethod
    def _review_exchange_validation_matches_current(
        cls,
        cached_record_path: Path | None,
        current_record_path: Path | None,
    ) -> bool:
        current_sha = cls._validation_head_sha(current_record_path)
        if not current_sha:
            # Strict cache callers reject missing current validation before this
            # helper. Older/dev resume paths keep their prior cache behavior.
            return True
        cached_sha = cls._validation_head_sha(cached_record_path)
        return cached_sha == current_sha

    @staticmethod
    def _validation_head_sha(record_path: Path | None) -> str | None:
        if not record_path or not record_path.exists():
            return None
        try:
            data = json.loads(record_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        head_sha = data.get("head_sha")
        return head_sha if isinstance(head_sha, str) and head_sha else None

    def resolve_review_exchange_mode(self, agent_label: str | None) -> str | None:
        if not self._config:
            return None
        mode = self._config.review_exchange_mode
        if agent_label:
            configured_reviewer = self._config.get_reviewer_for_agent(agent_label)
            if not configured_reviewer:
                logger.info(
                    "Review exchange disabled for %s: no reviewer configured",
                    agent_label,
                )
                return None
        if mode in {"via-mcp", "via-local-loop"}:
            agent_label = self.require_review_exchange_agent_label(agent_label, mode)
            if mode == "via-mcp":
                from ..infra.review_exchange_registry import supports_mcp_pair

                coder_system, reviewer_system = self.resolve_exchange_systems(agent_label)
                if not supports_mcp_pair(coder_system, reviewer_system):
                    raise ValueError(
                        "Review exchange via-mcp requires a supported ai_system pair: "
                        f"{agent_label} ({coder_system}->{reviewer_system})"
                    )
            return mode
        if mode != "auto":
            return None
        if not agent_label:
            logger.warning(
                "Review exchange auto mode requires agent label; falling back to draft PR."
            )
            return None
        agent_label = self.require_review_exchange_agent_label(agent_label, "auto")
        from ..infra.review_exchange_registry import supports_mcp_pair

        coder_system, reviewer_system = self.resolve_exchange_systems(agent_label)
        if supports_mcp_pair(coder_system, reviewer_system):
            return "via-mcp"
        return "via-local-loop"

    def require_review_exchange_agent_label(
        self, agent_label: str | None, mode: str
    ) -> str:
        if not agent_label:
            raise ValueError(f"Review exchange requires agent_label for {mode} mode")
        return agent_label

    def resolve_reviewer_label(self, agent_label: str) -> str:
        if not self._config:
            raise ValueError("Review exchange requires config")
        if agent_label not in self._config.agents:
            raise ValueError(f"Review exchange agent '{agent_label}' not found in config.agents")
        reviewer_label = self._config.get_reviewer_for_agent(agent_label)
        if not reviewer_label:
            raise ValueError("Review exchange requires review.default or per-agent reviewer")
        if reviewer_label not in self._config.agents:
            raise ValueError(f"Review exchange reviewer '{reviewer_label}' not found in config.agents")
        return reviewer_label

    def resolve_exchange_systems(self, agent_label: str) -> tuple[str, str]:
        if not self._config:
            raise ValueError("Review exchange requires config")
        reviewer_label = self.resolve_reviewer_label(agent_label)
        coder_system = self._config.agents[agent_label].ai_system
        reviewer_system = self._config.agents[reviewer_label].ai_system
        if not coder_system or not reviewer_system:
            raise ValueError("Review exchange requires ai_system on coder and reviewer agents")
        return coder_system, reviewer_system

    def run_review_exchange_loop(
        self,
        *,
        worktree: Path,
        issue_number: int,
        issue_title: str,
        session_name: str | None,
        agent_label: str | None,
        initial_validation_record_path: Path | None = None,
        on_started: Callable[[Path], None] | None = None,
        events: Any | None = None,
        event_context: Any | None = None,
    ) -> Any:
        if not self._config:
            raise ValueError("Review exchange requires config")
        if not agent_label:
            raise ValueError("Review exchange requires agent_label")
        coder_label = agent_label
        reviewer_label = self.resolve_reviewer_label(agent_label)
        coder_agent = self._config.agents[coder_label]
        reviewer_agent = self._config.agents[reviewer_label]
        max_rounds = self._config.review_exchange_max_rounds
        max_no_progress = self._config.review_exchange_max_no_progress
        require_validation = self._config.review_exchange_require_validation
        web_port = self._config.control_api_port

        from .review_exchange_loop import run_review_exchange_loop

        return run_review_exchange_loop(
            session_output=self._session_output,
            worktree_path=worktree,
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
