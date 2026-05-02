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
    """Run the coder↔reviewer exchange with persistent agent sessions.

    Both sessions are guaranteed to be terminated and reaped before this
    function returns, even if an exception is raised mid-loop. The
    reviewer/coder worktrees themselves are the caller's responsibility
    to create and remove.
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

    coder_session: PersistentSession | None = None
    reviewer_session: PersistentSession | None = None
    try:
        coder_session = _open_role_session(
            role="coder",
            agent=coder_agent,
            worktree=coder_worktree_path,
            run_dir=run_dir,
            recording_path=coder_recording,
            response_file=reviewer_response if False else coder_response,  # explicit per-role
            agent_label=coder_label,
            web_port=web_port,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
        )
        reviewer_session = _open_role_session(
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
        outcome = _drive_rounds(
            session_output=session_output,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            exchange_run_id=exchange_run_id,
            coder_session=coder_session,
            reviewer_session=reviewer_session,
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
        if reviewer_session is not None:
            close_persistent_session(reviewer_session)
        if coder_session is not None:
            close_persistent_session(coder_session)

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
    import os as _os
    from ..control.isolation import build_runtime_tool_env
    completion_path = (
        f".issue-orchestrator/sessions/{run_dir.name}/{role}/completion-{role}.json"
    )
    env = dict(_os.environ)
    env.update({
        f"{ENV_PREFIX}COMPLETION_PATH": completion_path,
        f"{ENV_PREFIX}VALIDATION_OUTPUT_DIR": str(run_dir),
        f"{ENV_PREFIX}AGENT_LABEL": agent_label,
        f"{ENV_PREFIX}ISSUE_NUMBER": str(issue_number),
        f"{ENV_PREFIX}REVIEW_RESPONSE_FILE": str(response_file),
        "ORCHESTRATOR_ISSUE_NUMBER": str(issue_number),
        "ORCHESTRATOR_SESSION_ID": session_name,
    })
    env.update(build_runtime_tool_env(worktree, base_env={}))
    if web_port is not None:
        env["ORCHESTRATOR_API_PORT"] = str(web_port)
    return env


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
        )
        emit(EventName.REVIEW_EXCHANGE_ROLE_PROMPTED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
            "role": "coder",
            "prompt_chars": len(coder_prompt_text),
        })
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
            )

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

    summary = _write_summary(exchange_dir, max_rounds, reviewer_response=None)
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
    summary = _write_summary(exchange_dir, round_index, reviewer)
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
    summary = _write_summary(exchange_dir, round_index, reviewer)
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
) -> ReviewExchangeOutcome:
    summary = _write_summary(exchange_dir, round_index, last_reviewer)
    return ReviewExchangeOutcome(
        status="error",
        rounds=round_index,
        reason=f"{role}_no_completion",
        reviewer_response=last_reviewer,
        exchange_dir=exchange_dir,
        summary=summary,
    )


def _write_summary(
    exchange_dir: Path,
    round_index: int,
    reviewer_response: ReviewExchangeResponse | None,
) -> dict[str, Any]:
    summary = {
        "completed_rounds": round_index,
        "status": "ok" if reviewer_response and reviewer_response.response_type == "ok" else "stopped",
        "response_text": reviewer_response.response_text if reviewer_response else "",
        "reason": (
            "reviewer_ok"
            if reviewer_response and reviewer_response.response_type == "ok"
            else "incomplete"
        ),
    }
    summary_path = exchange_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
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
) -> None:
    """Capture the recording's current event index and append a chapter.

    Errors are logged but not raised — chapter writes are advisory; if
    the recording is briefly missing we want the exchange to keep
    running, not bail mid-round.
    """
    try:
        event_index = recording_event_count(recording_path)
    except FileNotFoundError:
        logger.warning(
            "Recording missing for %s at chapter time; skipping chapter %s",
            role, label,
        )
        return
    try:
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
    except Exception:
        logger.warning("Failed to record chapter %s/%s", role, label, exc_info=True)


def _validation_passed(run_dir: Path) -> bool:
    record_path = run_dir / "validation-record.json"
    if not record_path.exists():
        return False
    try:
        data = json.loads(record_path.read_text())
    except json.JSONDecodeError:
        return False
    return bool(data.get("passed"))
