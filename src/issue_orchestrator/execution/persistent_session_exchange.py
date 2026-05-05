"""Persistent-session review-exchange runner.

Drives a coder↔reviewer review exchange where each role is one persistent
agent process — opened at exchange start, prompted once per round via
``send_round``, and terminated explicitly at exchange end. The PTY for
each role captures one continuous ``terminal-recording.jsonl`` spanning
every round of the exchange, plus a ``chapters.json`` sidecar that marks
each prompt/feedback boundary so the session viewer can scrub straight
to "where the reviewer's round-2 comments start."

The reviewer runs in a separate worktree from the coder; the caller is
responsible for creating that worktree before invoking this runner and
removing it after. Between rounds the caller may inject a
``before_reviewer_round`` callback to e.g. fast-forward the reviewer
worktree to the coder's branch tip.

This module owns the round-loop semantics — validation gating,
no-progress termination, event emission. PR 2f (the dispatch flip) wires
it into ``CompletionReviewExchange`` and adds the worktree lifecycle
that surrounds it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.exchange_chapter import (
    CHAPTER_SECTION_FEEDBACK,
    CHAPTER_SECTION_PROMPT,
    CHAPTER_SECTION_TIMEOUT,
)
from ..domain.models import AgentConfig
from ..domain.review_exchange import (
    ReviewExchangeOutcome,
    ReviewExchangeResponse,
    build_coder_prompt,
    build_reviewer_prompt,
)
from ..events import EventContext, EventName
from ..infra.env import ENV_PREFIX
from ..infra.logging_config import get_repo_log_path
from ..infra.terminal_recording import TERMINAL_RECORDING_FILENAME
from ..ports import EventSink, make_trace_event
from ..ports.session_output import SessionOutput
from .persistent_exchange_pair_registry_inmemory import (
    InMemoryPersistentExchangePairRegistry,
    PersistentExchangePair,
)
from .persistent_round_runner import (
    PersistentRoundError,
    PersistentRoundTimeoutError,
    PersistentSession,
    close_persistent_session,
    open_persistent_session,
    recording_event_count,
    send_round,
)

logger = logging.getLogger(__name__)


_CODER_PROTOCOL_RETRY_LIMIT = 2

_BOOTSTRAP_PROMPT_TEMPLATE = (
    "You are the {role} in a coder↔reviewer review exchange for issue "
    "#{issue_number}: {issue_title}.\n\n"
    "Wait for the orchestrator to send your role-specific instructions via "
    "stdin. For each prompt, follow the instructions and write exactly one "
    "line of JSON to the file at $ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE. "
    "Then wait for the next prompt. Do not exit on your own; the orchestrator "
    "will terminate you when the exchange is done.\n"
)


def run_persistent_session_exchange(  # noqa: PLR0913
    *,
    session_output: SessionOutput,
    pair_registry: InMemoryPersistentExchangePairRegistry,
    coder_worktree_path: Path,
    reviewer_worktree_path: Path,
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
    before_reviewer_round: Callable[[int], None] | None = None,
) -> ReviewExchangeOutcome:
    """Run the coder↔reviewer exchange against a registry-owned persistent pair.

    Acquires a pair from ``pair_registry`` (spawns one on cache miss),
    drives the round loop, and releases the pair when the exchange
    ends. In B1 the release runs unconditionally per exchange so
    behavior is identical to the pre-registry world; in B2 the
    release moves to issue-completion sites so the same pair can
    serve multiple back-to-back exchanges.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    session_name = f"review-exchange-{issue_number}-{timestamp}"
    run = session_output.start_run(
        coder_worktree_path,
        session_name,
        issue_number=issue_number,
        agent_label=coder_label,
        backend="persistent-pty",
        orchestrator_log=str(get_repo_log_path(coder_worktree_path)),
    )
    run_dir = run.run_dir
    run_id = run.run_id
    exchange_run_id = run_id

    if initial_validation_record_path is not None and initial_validation_record_path.exists():
        seed_target = run_dir / "validation-record.json"
        if not seed_target.exists():
            seed_target.write_bytes(initial_validation_record_path.read_bytes())

    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    session_output.update_manifest(
        run_dir, {"review_exchange_dir": str(exchange_dir)},
    )
    if on_started is not None:
        on_started(run_dir)

    def _emit(event_name: EventName, payload: dict[str, Any]) -> None:
        if events is None or event_context is None:
            return
        enriched = dict(payload)
        enriched["run_dir"] = str(run_dir)
        enriched["session_run_id"] = run_id
        events.publish(make_trace_event(event_name, event_context.enrich(enriched)))

    _emit(EventName.REVIEW_EXCHANGE_STARTED, {
        "issue_number": issue_number,
        "issue_title": issue_title,
        "session_name": session_name,
        "coder_label": coder_label,
        "reviewer_label": reviewer_label,
        "max_rounds": max_rounds,
        "max_no_progress": max_no_progress,
        "require_validation": require_validation,
        "exchange_dir": str(exchange_dir),
    })

    coder_recording = run_dir / "coder" / TERMINAL_RECORDING_FILENAME
    reviewer_recording = run_dir / "reviewer" / TERMINAL_RECORDING_FILENAME
    coder_response = run_dir / "coder" / "review-response.json"
    reviewer_response = run_dir / "reviewer" / "review-response.json"

    def _spawn_pair() -> PersistentExchangePair:
        coder = _open_role_session(
            role="coder",
            agent=coder_agent,
            worktree=coder_worktree_path,
            run_dir=run_dir,
            recording_path=coder_recording,
            response_file=coder_response,
            agent_label=coder_label,
            web_port=web_port,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
        )
        # Reviewer-spawn-after-coder-success is the canonical
        # partial-construction case: if the reviewer's PTY/process
        # bring-up raises, the coder is already running and would
        # leak unless we close it explicitly. Pre-registry code
        # paired the two opens inside one ``try`` and closed any
        # already-opened session in ``finally``; the registry
        # version preserves that guarantee here so a partial spawn
        # never returns a half-built pair to the registry's cache.
        try:
            reviewer = _open_role_session(
                role="reviewer",
                agent=reviewer_agent,
                worktree=reviewer_worktree_path,
                run_dir=run_dir,
                recording_path=reviewer_recording,
                response_file=reviewer_response,
                agent_label=reviewer_label,
                web_port=web_port,
                issue_number=issue_number,
                issue_title=issue_title,
                session_name=session_name,
            )
        except BaseException:
            close_persistent_session(coder)
            raise
        from time import time as _wall_clock
        return PersistentExchangePair(
            coder_session=coder,
            reviewer_session=reviewer,
            reviewer_worktree_path=reviewer_worktree_path,
            issue_key=issue_number,
            created_at=_wall_clock(),
        )

    pair = pair_registry.acquire(issue_key=issue_number, spawn=_spawn_pair)
    try:
        outcome = _drive_rounds(
            session_output=session_output,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            exchange_run_id=exchange_run_id,
            coder_session=pair.coder_session,
            reviewer_session=pair.reviewer_session,
            coder_response=coder_response,
            reviewer_response=reviewer_response,
            coder_recording=coder_recording,
            reviewer_recording=reviewer_recording,
            coder_timeout_seconds=coder_agent.timeout_minutes * 60,
            reviewer_timeout_seconds=reviewer_agent.timeout_minutes * 60,
            max_rounds=max_rounds,
            max_no_progress=max_no_progress,
            require_validation=require_validation,
            before_reviewer_round=before_reviewer_round,
            emit=_emit,
        )
    except Exception as exc:
        _emit(EventName.REVIEW_EXCHANGE_FAILED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": 0,
            "error": str(exc),
            "exception_type": type(exc).__name__,
        })
        raise
    finally:
        # B1: release per exchange — same external behavior as the
        # pre-registry world. ADR 0026 / B2 moves this release to
        # issue-completion sites so the pair survives across rework
        # cycles for the same issue.
        pair_registry.release(issue_number, reason="exchange-complete")

    return outcome


# ---------------------------------------------------------------------------
# Session bring-up
# ---------------------------------------------------------------------------


def _open_role_session(
    *,
    role: str,
    agent: AgentConfig,
    worktree: Path,
    run_dir: Path,
    recording_path: Path,
    response_file: Path,
    agent_label: str,
    web_port: int | None,
    issue_number: int,
    issue_title: str,
    session_name: str,
) -> PersistentSession:
    """Build the launch command + env for one role and open the persistent session."""
    bootstrap = _BOOTSTRAP_PROMPT_TEMPLATE.format(
        role=role, issue_number=issue_number, issue_title=issue_title,
    )
    bootstrap_agent = AgentConfig(
        prompt_path=agent.prompt_path,
        prompt_relative=agent.prompt_relative,
        provider=agent.provider,
        model=agent.model,
        timeout_minutes=agent.timeout_minutes,
        provider_args=dict(agent.provider_args),
        permission_mode=agent.permission_mode,
        skip_review=agent.skip_review,
        reviewer=agent.reviewer,
        command=agent.command,
        meta_agent=agent.meta_agent,
        initial_prompt=bootstrap,
        ai_system=agent.ai_system,
        retry_prompt_template=agent.retry_prompt_template,
    )
    command_str = bootstrap_agent.get_command(
        issue_number=issue_number,
        issue_title=issue_title,
        worktree=worktree,
        task_kind=f"review_exchange_{role}",
    )
    import shlex
    command = shlex.split(command_str)

    response_file.parent.mkdir(parents=True, exist_ok=True)
    env = _build_role_env(
        run_dir=run_dir,
        response_file=response_file,
        worktree=worktree,
        role=role,
        agent_label=agent_label,
        web_port=web_port,
        issue_number=issue_number,
        session_name=session_name,
    )
    return open_persistent_session(
        command=command,
        working_dir=worktree,
        env=env,
        recording_path=recording_path,
        additional_recording_paths=[run_dir / TERMINAL_RECORDING_FILENAME],
    )


def _build_role_env(
    *,
    run_dir: Path,
    response_file: Path,
    worktree: Path,
    role: str,
    agent_label: str,
    web_port: int | None,
    issue_number: int,
    session_name: str,
) -> dict[str, str]:
    """Compose the agent environment via the shared filtered-env owner.

    Routing through ``build_filtered_env`` is load-bearing: the
    orchestrator process holds GH_TOKEN / GITHUB_TOKEN /
    ISSUE_ORCHESTRATOR_API_TOKEN / CLAUDECODE / SSH_AUTH_SOCK and
    similar credentials that long-lived agent processes must NOT
    inherit. The active runner (``control/review_exchange_loop._run_agent_round``)
    goes through this same helper for the same reason; bypassing it here
    would let coder/reviewer agents run with admin GitHub tokens, the
    Control API admin bearer, etc.
    """
    from ..control.isolation import build_runtime_tool_env
    from .agent_runner_env import build_filtered_env

    completion_path = (
        f".issue-orchestrator/sessions/{run_dir.name}/{role}/completion-{role}.json"
    )
    overrides: dict[str, str] = {
        f"{ENV_PREFIX}COMPLETION_PATH": completion_path,
        f"{ENV_PREFIX}VALIDATION_OUTPUT_DIR": str(run_dir),
        f"{ENV_PREFIX}AGENT_LABEL": agent_label,
        f"{ENV_PREFIX}ISSUE_NUMBER": str(issue_number),
        f"{ENV_PREFIX}REVIEW_RESPONSE_FILE": str(response_file),
        "ORCHESTRATOR_ISSUE_NUMBER": str(issue_number),
        "ORCHESTRATOR_SESSION_ID": session_name,
    }
    overrides.update(build_runtime_tool_env(worktree, base_env={}))
    if web_port is not None:
        overrides["ORCHESTRATOR_API_PORT"] = str(web_port)
    return build_filtered_env(overrides=overrides)


# ---------------------------------------------------------------------------
# Round loop
# ---------------------------------------------------------------------------


def _drive_rounds(  # noqa: PLR0913
    *,
    session_output: SessionOutput,
    run_dir: Path,
    exchange_dir: Path,
    issue_number: int,
    issue_title: str,
    session_name: str,
    exchange_run_id: str,
    coder_session: PersistentSession,
    reviewer_session: PersistentSession,
    coder_response: Path,
    reviewer_response: Path,
    coder_recording: Path,
    reviewer_recording: Path,
    coder_timeout_seconds: float,
    reviewer_timeout_seconds: float,
    max_rounds: int,
    max_no_progress: int,
    require_validation: bool,
    before_reviewer_round: Callable[[int], None] | None,
    emit: Callable[[EventName, dict[str, Any]], None],
) -> ReviewExchangeOutcome:
    no_progress_count = 0
    last_reviewer_text: str | None = None
    last_coder_text: str | None = None

    for round_index in range(1, max_rounds + 1):
        if before_reviewer_round is not None:
            before_reviewer_round(round_index)

        # ----- Reviewer turn -----
        reviewer_prompt_text = build_reviewer_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            round_index=round_index,
            last_coder_text=last_coder_text,
            last_reviewer_text=last_reviewer_text,
            require_validation=require_validation,
            run_dir=run_dir,
        )
        emit(EventName.REVIEW_EXCHANGE_ROUND_STARTED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
        })
        _record_chapter(
            session_output=session_output,
            run_dir=run_dir,
            role="reviewer",
            recording_path=reviewer_recording,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            cycle_index=round_index,
            section=CHAPTER_SECTION_PROMPT,
            label=f"Round {round_index} reviewer prompt",
            session_name=session_name,
            emit=emit,
        )
        emit(EventName.REVIEW_EXCHANGE_ROLE_PROMPTED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
            "role": "reviewer",
            "prompt_chars": len(reviewer_prompt_text),
        })
        reviewer = _send_role_round(
            session=reviewer_session,
            role="reviewer",
            response_file=reviewer_response,
            recording_path=reviewer_recording,
            prompt=reviewer_prompt_text,
            timeout_seconds=reviewer_timeout_seconds,
            session_output=session_output,
            run_dir=run_dir,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            cycle_index=round_index,
            session_name=session_name,
            emit=emit,
        )
        if reviewer is None:
            return _build_outcome_for_role_timeout(
                exchange_dir=exchange_dir,
                round_index=round_index,
                role="reviewer",
                last_reviewer=None,
                emit=emit,
                issue_number=issue_number,
                session_name=session_name,
            )

        if require_validation and reviewer.response_type == "ok" and not _validation_passed(run_dir):
            reviewer = ReviewExchangeResponse(
                response_type="changes_requested",
                response_text=(
                    "Validation record missing or failed. Address the failing "
                    "checks and continue."
                ),
                getting_closer=False,
                raw_json=reviewer.raw_json,
                raw_output=reviewer.raw_output,
            )

        if reviewer.response_type == "ok":
            return _complete_with_reviewer_ok(
                exchange_dir=exchange_dir,
                round_index=round_index,
                reviewer=reviewer,
                emit=emit,
                issue_number=issue_number,
                session_name=session_name,
            )
        if reviewer.getting_closer is False:
            no_progress_count += 1
        else:
            no_progress_count = 0
        if max_no_progress > 0 and no_progress_count >= max_no_progress:
            return _stop_for_no_progress(
                exchange_dir=exchange_dir,
                round_index=round_index,
                reviewer=reviewer,
                emit=emit,
                issue_number=issue_number,
                session_name=session_name,
            )

        last_reviewer_text = reviewer.response_text

        # ----- Coder turn -----
        coder_prompt_text = build_coder_prompt(
            issue_number=issue_number,
            issue_title=issue_title,
            round_index=round_index,
            reviewer_feedback=reviewer.response_text,
            run_dir=run_dir,
        )
        _record_chapter(
            session_output=session_output,
            run_dir=run_dir,
            role="coder",
            recording_path=coder_recording,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            cycle_index=round_index,
            section=CHAPTER_SECTION_PROMPT,
            label=f"Round {round_index} coder prompt",
            session_name=session_name,
            emit=emit,
        )
        emit(EventName.REVIEW_EXCHANGE_ROLE_PROMPTED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
            "role": "coder",
            "prompt_chars": len(coder_prompt_text),
        })
        # Clear the previous turn's completion artifact so a stale file
        # from round N-1 cannot satisfy round N's protocol guardrail —
        # the guardrail must observe an artifact freshly written during
        # *this* round's coding-done invocation.
        _clear_coder_completion(run_dir)
        coder = _send_role_round(
            session=coder_session,
            role="coder",
            response_file=coder_response,
            recording_path=coder_recording,
            prompt=coder_prompt_text,
            timeout_seconds=coder_timeout_seconds,
            session_output=session_output,
            run_dir=run_dir,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            cycle_index=round_index,
            session_name=session_name,
            emit=emit,
        )
        if coder is None:
            return _build_outcome_for_role_timeout(
                exchange_dir=exchange_dir,
                round_index=round_index,
                role="coder",
                last_reviewer=reviewer,
                emit=emit,
                issue_number=issue_number,
                session_name=session_name,
            )

        coder, protocol_outcome = _enforce_coder_protocol(
            session_output=session_output,
            coder_session=coder_session,
            coder=coder,
            reviewer=reviewer,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            coder_response=coder_response,
            coder_recording=coder_recording,
            coder_timeout_seconds=coder_timeout_seconds,
            require_validation=require_validation,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            session_name=session_name,
            cycle_index=round_index,
            emit=emit,
        )
        if protocol_outcome is not None:
            return protocol_outcome

        emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
            "reviewer_response_type": reviewer.response_type,
            "reviewer_response_text": reviewer.response_text,
            "coder_response_type": coder.response_type,
            "coder_response_text": coder.response_text,
        })
        last_coder_text = coder.response_text

    summary = _write_summary(
        exchange_dir, max_rounds,
        status="stopped", reason="max_rounds_exceeded",
        reviewer_response=None,
    )
    emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": max_rounds,
        "status": "stopped",
        "reason": "max_rounds_exceeded",
    })
    return ReviewExchangeOutcome(
        status="stopped",
        rounds=max_rounds,
        reason="max_rounds_exceeded",
        reviewer_response=None,
        exchange_dir=exchange_dir,
        summary=summary,
    )


def _enforce_coder_protocol(  # noqa: PLR0913
    *,
    session_output: SessionOutput,
    coder_session: PersistentSession,
    coder: ReviewExchangeResponse,
    reviewer: ReviewExchangeResponse,
    run_dir: Path,
    exchange_dir: Path,
    coder_response: Path,
    coder_recording: Path,
    coder_timeout_seconds: float,
    require_validation: bool,
    exchange_run_id: str,
    issue_number: int,
    session_name: str,
    cycle_index: int,
    emit: Callable[[EventName, dict[str, Any]], None],
) -> tuple[ReviewExchangeResponse, ReviewExchangeOutcome | None]:
    """Validate the coder produced its completion-coder.json artifact, retry
    with a remediation prompt up to ``_CODER_PROTOCOL_RETRY_LIMIT`` times,
    and return either the validated response or a terminal outcome.

    Mirrors the active runner's _run_coder_round_with_protocol_retries.
    Without this guardrail a coder could advance the exchange by writing
    only the review-response file while skipping coding-done.
    """
    protocol_error = _validate_coder_completion(
        run_dir, require_validation=require_validation,
    )
    retries_remaining = _CODER_PROTOCOL_RETRY_LIMIT
    while protocol_error is not None and retries_remaining > 0:
        retries_remaining -= 1
        retry_prompt = (
            f"{protocol_error}\n"
            "Run `coding-done completed --implementation '...' --problems '...'` "
            "(or `coding-done blocked --reason '...' --attempted '...'` if you "
            "cannot continue), then write your one-line JSON response again to "
            "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE."
        )
        _record_chapter(
            session_output=session_output,
            run_dir=run_dir,
            role="coder",
            recording_path=coder_recording,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            cycle_index=cycle_index,
            section=CHAPTER_SECTION_PROMPT,
            label=f"Round {cycle_index} coder protocol-retry",
            session_name=session_name,
            emit=emit,
        )
        emit(EventName.REVIEW_EXCHANGE_ROLE_PROMPTED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": cycle_index,
            "role": "coder",
            "prompt_chars": len(retry_prompt),
            "protocol_retry": True,
        })
        # Same freshness invariant as the initial turn: drop any file
        # left over from the previous attempt before the retry runs.
        _clear_coder_completion(run_dir)
        retry_response = _send_role_round(
            session=coder_session,
            role="coder",
            response_file=coder_response,
            recording_path=coder_recording,
            prompt=retry_prompt,
            timeout_seconds=coder_timeout_seconds,
            session_output=session_output,
            run_dir=run_dir,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            cycle_index=cycle_index,
            session_name=session_name,
            emit=emit,
        )
        if retry_response is None:
            return coder, _build_outcome_for_role_timeout(
                exchange_dir=exchange_dir,
                round_index=cycle_index,
                role="coder",
                last_reviewer=reviewer,
                emit=emit,
                issue_number=issue_number,
                session_name=session_name,
            )
        coder = retry_response
        protocol_error = _validate_coder_completion(
            run_dir, require_validation=require_validation,
        )
    if protocol_error is not None:
        return coder, _build_outcome_for_protocol_error(
            exchange_dir=exchange_dir,
            round_index=cycle_index,
            last_reviewer=reviewer,
            last_coder=coder,
            protocol_error=protocol_error,
            emit=emit,
            issue_number=issue_number,
            session_name=session_name,
        )
    return coder, None


def _send_role_round(  # noqa: PLR0913
    *,
    session: PersistentSession,
    role: str,
    response_file: Path,
    recording_path: Path,
    prompt: str,
    timeout_seconds: float,
    session_output: SessionOutput,
    run_dir: Path,
    exchange_run_id: str,
    issue_number: int,
    cycle_index: int,
    session_name: str,
    emit: Callable[[EventName, dict[str, Any]], None],
) -> ReviewExchangeResponse | None:
    """Send one role's round prompt and convert the response to a domain object.

    Returns ``None`` if the role timed out or died — the caller emits
    REVIEW_EXCHANGE_ROLE_TIMEOUT and bails out of the exchange.
    """
    try:
        parsed = send_round(
            session,
            prompt=prompt,
            response_file=response_file,
            timeout_seconds=timeout_seconds,
            # Tag heartbeat/diagnostic logs with role + cycle so an
            # interleaved coder + reviewer log is decodable without
            # cross-referencing PIDs (#6160 e2e regression: 17 minutes
            # of unattributed silence).
            role_label=f"{role}@round-{cycle_index}",
        )
    except (PersistentRoundTimeoutError, PersistentRoundError) as exc:
        logger.warning(
            "%s round %d failed: %s", role, cycle_index, exc,
        )
        _record_chapter(
            session_output=session_output,
            run_dir=run_dir,
            role=role,
            recording_path=recording_path,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            cycle_index=cycle_index,
            section=CHAPTER_SECTION_TIMEOUT,
            label=f"Round {cycle_index} {role} timeout/error",
            session_name=session_name,
            emit=emit,
        )
        emit(EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": cycle_index,
            "role": role,
            "reason": "no_completion",
            "detail": str(exc),
        })
        return None

    response = _normalize_role_response(parsed)
    _record_chapter(
        session_output=session_output,
        run_dir=run_dir,
        role=role,
        recording_path=recording_path,
        exchange_run_id=exchange_run_id,
        issue_number=issue_number,
        cycle_index=cycle_index,
        section=CHAPTER_SECTION_FEEDBACK,
        label=f"Round {cycle_index} {role} feedback",
        session_name=session_name,
        emit=emit,
    )
    emit(EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": cycle_index,
        "role": role,
        "response_type": response.response_type,
        "getting_closer": response.getting_closer,
    })
    return response


def _normalize_role_response(parsed: dict[str, Any]) -> ReviewExchangeResponse:
    """Convert the raw JSON dict into a domain ReviewExchangeResponse.

    Missing required fields are tolerated by surfacing a synthetic
    response_type that the caller can treat as a protocol error if it
    chooses to. Today's contract is: the agent writes
    {response_type, response_text, [getting_closer]}; anything else is
    surfaced as response_type='protocol_error'.
    """
    response_type = str(parsed.get("response_type") or "").strip()
    response_text = str(parsed.get("response_text") or "").strip()
    if not response_type or not response_text:
        return ReviewExchangeResponse(
            response_type="protocol_error",
            response_text=(
                "Agent response missing required response_type/response_text fields"
            ),
            getting_closer=False,
            raw_json=parsed,
            raw_output=None,
        )
    return ReviewExchangeResponse(
        response_type=response_type,
        response_text=response_text,
        getting_closer=parsed.get("getting_closer"),
        raw_json=parsed,
        raw_output=None,
    )


# ---------------------------------------------------------------------------
# Outcome helpers
# ---------------------------------------------------------------------------


def _complete_with_reviewer_ok(
    *,
    exchange_dir: Path,
    round_index: int,
    reviewer: ReviewExchangeResponse,
    emit: Callable[[EventName, dict[str, Any]], None],
    issue_number: int,
    session_name: str,
) -> ReviewExchangeOutcome:
    summary = _write_summary(
        exchange_dir, round_index,
        status="ok", reason="reviewer_ok", reviewer_response=reviewer,
    )
    emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": reviewer.response_type,
        "reviewer_response_text": reviewer.response_text,
        "coder_response_type": None,
    })
    emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": round_index,
        "status": "ok",
        "reason": "reviewer_ok",
    })
    return ReviewExchangeOutcome(
        status="ok",
        rounds=round_index,
        reason="reviewer_ok",
        reviewer_response=reviewer,
        exchange_dir=exchange_dir,
        summary=summary,
    )


def _stop_for_no_progress(
    *,
    exchange_dir: Path,
    round_index: int,
    reviewer: ReviewExchangeResponse,
    emit: Callable[[EventName, dict[str, Any]], None],
    issue_number: int,
    session_name: str,
) -> ReviewExchangeOutcome:
    summary = _write_summary(
        exchange_dir, round_index,
        status="stopped", reason="reviewer_reports_no_progress",
        reviewer_response=reviewer,
    )
    emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": reviewer.response_type,
        "reviewer_response_text": reviewer.response_text,
        "coder_response_type": None,
    })
    emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": round_index,
        "status": "stopped",
        "reason": "reviewer_reports_no_progress",
    })
    return ReviewExchangeOutcome(
        status="stopped",
        rounds=round_index,
        reason="reviewer_reports_no_progress",
        reviewer_response=reviewer,
        exchange_dir=exchange_dir,
        summary=summary,
    )


def _build_outcome_for_role_timeout(
    *,
    exchange_dir: Path,
    round_index: int,
    role: str,
    last_reviewer: ReviewExchangeResponse | None,
    emit: Callable[[EventName, dict[str, Any]], None],
    issue_number: int,
    session_name: str,
) -> ReviewExchangeOutcome:
    """Build the ``error`` outcome when a role times out / dies / fails protocol.

    Persists the summary with matching ``status`` and emits the terminal
    ``REVIEW_EXCHANGE_COMPLETED`` event so timeline / cache consumers see
    a definitive end-of-exchange marker. Without the event the active
    path's contract — every exchange ends with one COMPLETED or FAILED
    event — is broken on the persistent path.
    """
    reason = f"{role}_no_completion"
    summary = _write_summary(
        exchange_dir, round_index,
        status="error", reason=reason, reviewer_response=last_reviewer,
    )
    emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": round_index,
        "status": "error",
        "reason": reason,
    })
    return ReviewExchangeOutcome(
        status="error",
        rounds=round_index,
        reason=reason,
        reviewer_response=last_reviewer,
        exchange_dir=exchange_dir,
        summary=summary,
    )


def _build_outcome_for_protocol_error(
    *,
    exchange_dir: Path,
    round_index: int,
    last_reviewer: ReviewExchangeResponse | None,
    last_coder: ReviewExchangeResponse | None,
    protocol_error: str,
    emit: Callable[[EventName, dict[str, Any]], None],
    issue_number: int,
    session_name: str,
) -> ReviewExchangeOutcome:
    """Build the ``error`` outcome when the coder fails its protocol contract.

    Mirrors the active runner's ``_stop_for_protocol_error``: emits a
    REVIEW_EXCHANGE_ROUND_COMPLETED with the partial round's data plus a
    REVIEW_EXCHANGE_COMPLETED with status=error and protocol_error reason.
    """
    summary = _write_summary(
        exchange_dir, round_index,
        status="error", reason="coder_protocol_error",
        reviewer_response=last_reviewer,
    )
    emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": last_reviewer.response_type if last_reviewer else None,
        "reviewer_response_text": last_reviewer.response_text if last_reviewer else None,
        "coder_response_type": "protocol_error",
        "coder_response_text": last_coder.response_text if last_coder else None,
        "detail": protocol_error,
    })
    emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": round_index,
        "status": "error",
        "reason": "coder_protocol_error",
        "detail": protocol_error,
    })
    return ReviewExchangeOutcome(
        status="error",
        rounds=round_index,
        reason="coder_protocol_error",
        reviewer_response=last_reviewer,
        exchange_dir=exchange_dir,
        summary=summary,
    )


def _write_summary(
    exchange_dir: Path,
    round_index: int,
    *,
    status: str,
    reason: str,
    reviewer_response: ReviewExchangeResponse | None,
) -> dict[str, Any]:
    """Persist summary.json atomically using the same shape the active
    runner emits, so the publish-cache contract is uniform across both
    runners. ``status`` is the ReviewExchangeOutcome status value
    ("ok"/"stopped"/"error"); ``reason`` carries the matching reason
    token."""
    summary = {
        "completed_rounds": round_index,
        "status": status,
        "response_text": reviewer_response.response_text if reviewer_response else None,
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(exchange_dir / "summary.json", summary)
    return summary


# ---------------------------------------------------------------------------
# Chapter sidecar
# ---------------------------------------------------------------------------


def _record_chapter(  # noqa: PLR0913
    *,
    session_output: SessionOutput,
    run_dir: Path,
    role: str,
    recording_path: Path,
    exchange_run_id: str,
    issue_number: int,
    cycle_index: int,
    section: str,
    label: str,
    session_name: str,
    emit: Callable[[EventName, dict[str, Any]], None],
) -> None:
    """Capture the recording's current event index, append a chapter,
    and emit ``REVIEW_EXCHANGE_CHAPTER_RECORDED``.

    Errors propagate. Role recordings are created at session open and the
    chapter offset is the UI contract for scrubbing the persistent
    recording — a missing recording or failed sidecar write means the
    replay contract is broken, not a best-effort detail. The top-level
    ``run_persistent_session_exchange`` handler converts the propagated
    exception into a REVIEW_EXCHANGE_FAILED event and re-raises so the
    orchestrator surface treats it as a definitive exchange failure.

    The chapter event is emitted *after* the sidecar write succeeds so
    SSE/timeline consumers see the same offset that's now durable on disk;
    on failure the exception propagates and no event fires (consistent
    with the rest of the runner's emit-on-success contract).
    """
    event_index = recording_event_count(recording_path)
    session_output.record_exchange_chapter(
        run_dir,
        role=role,
        exchange_run_id=exchange_run_id,
        issue_number=issue_number,
        cycle_index=cycle_index,
        section=section,
        recording_event_index=event_index,
        recorded_at=datetime.now(timezone.utc).isoformat(),
        label=label,
    )
    emit(EventName.REVIEW_EXCHANGE_CHAPTER_RECORDED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": cycle_index,
        "role": role,
        "section": section,
        "recording_event_index": event_index,
        "label": label,
    })


def _validation_passed(run_dir: Path) -> bool:
    record_path = run_dir / "validation-record.json"
    if not record_path.exists():
        return False
    try:
        data = json.loads(record_path.read_text())
    except json.JSONDecodeError:
        return False
    return bool(data.get("passed"))


def _coder_completion_path(run_dir: Path) -> Path:
    """Single source of truth for where the coder writes its completion artifact."""
    return run_dir / "coder" / "completion-coder.json"


def _clear_coder_completion(run_dir: Path) -> None:
    """Unlink any prior coder completion artifact so the protocol guardrail
    observes only the file freshly written during the current turn.

    Without this, ``_validate_coder_completion`` sees a stale artifact
    from an earlier round and accepts a coder that skipped coding-done
    on this turn entirely. The active runner avoids this because each
    round spawns a fresh coder process whose env points at a per-round
    path; the persistent runner shares the path across rounds, so we
    have to invalidate explicitly.
    """
    _coder_completion_path(run_dir).unlink(missing_ok=True)


def _validate_coder_completion(
    run_dir: Path,
    *,
    require_validation: bool,
) -> str | None:
    """Mirror of control/review_exchange_loop._validate_coder_protocol.

    The coder must produce a completion-coder.json artifact (the
    ``coding-done`` CLI's output) and, when ``require_validation`` is on,
    a passing validation-record.json. A coder that only writes the
    review-response file but skips coding-done would otherwise advance
    the exchange by accident.
    """
    completion_path = _coder_completion_path(run_dir)
    if not completion_path.exists():
        return f"missing completion artifact: {completion_path}"
    if completion_path.stat().st_size <= 0:
        return f"completion artifact is empty: {completion_path}"
    try:
        payload = json.loads(completion_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return f"completion artifact is not valid JSON: {completion_path}"
    if not isinstance(payload, dict):
        return f"completion artifact must be a JSON object: {completion_path}"
    if require_validation and not _validation_passed(run_dir):
        return "validation-record.json missing or did not pass"
    return None


# ``_atomic_write_json`` is the shared helper from ``infra.atomic_io``;
# re-export under the private name so the existing test that monkeypatches
# ``pse.os.replace`` continues to find the same write path.
from ..infra.atomic_io import atomic_write_json as _atomic_write_json  # noqa: E402
