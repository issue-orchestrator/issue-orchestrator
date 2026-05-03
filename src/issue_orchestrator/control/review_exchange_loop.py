"""MCP review exchange loop runner."""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..agent_runner import AgentRunner, AgentResult, AgentSpec
from ..domain.models import AgentConfig
from ..domain.review_exchange import (
    ReviewExchangeOutcome,
    ReviewExchangeResponse,
    build_coder_prompt,
    build_reviewer_prompt,
    parse_exchange_response,
)
from ..infra.logging_config import get_repo_log_path
from ..infra.terminal_recording import TERMINAL_RECORDING_FILENAME
from ..infra.env import ENV_PREFIX
from ..ports.session_output import SessionOutput
from ..ports import EventSink,  make_trace_event
from ..events import EventName, EventContext
from .isolation import build_runtime_tool_env

logger = logging.getLogger(__name__)
_CODER_PROTOCOL_RETRY_LIMIT = 2
REVIEW_RESPONSE_FILENAME = "review-response.json"


def _resolve_provider(agent: AgentConfig) -> str | None:
    """Prefer explicit provider, otherwise reuse ai_system when it matches a provider."""
    if agent.provider:
        return agent.provider
    if not agent.ai_system:
        return None
    from ..agent_runner import get_provider

    try:
        get_provider(agent.ai_system)
    except Exception:
        return None
    return agent.ai_system


def _escape_claude_project_path(path: Path) -> str:
    cleaned = str(path).lstrip("/")
    return "-" + cleaned.replace("/", "-")


def run_review_exchange_loop(
    *,
    session_output: SessionOutput,
    worktree_path: Path,
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
    web_port: int | None,
    events: EventSink | None = None,
    event_context: EventContext | None = None,
    on_started: Callable[[Path], None] | None = None,
) -> ReviewExchangeOutcome:
    """Run the coder↔reviewer exchange loop and capture round-trip logs."""
    run_dir: Path | None = None
    run_id: str | None = None

    def _emit(event_name: EventName, payload: dict[str, Any]) -> None:
        if events is None or event_context is None:
            return
        enriched_payload = dict(payload)
        if run_dir is not None:
            enriched_payload["run_dir"] = str(run_dir)
        if run_id is not None:
            enriched_payload["session_run_id"] = run_id
        events.publish(make_trace_event(event_name, event_context.enrich(enriched_payload)))

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    session_name = f"review-exchange-{issue_number}-{timestamp}"
    claude_project_dir = Path.home() / ".claude" / "projects" / _escape_claude_project_path(worktree_path)
    run = session_output.start_run(
        worktree_path,
        session_name,
        issue_number=issue_number,
        agent_label=coder_label,
        backend="subprocess",
        claude_log_dir=str(claude_project_dir),
        orchestrator_log=str(get_repo_log_path(worktree_path)),
    )
    run_dir = run.run_dir
    run_id = run.run_id
    _seed_validation_record(
        run_dir=run_dir,
        source_record_path=initial_validation_record_path,
        session_output=session_output,
    )
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    session_output.update_manifest(run_dir, {"review_exchange_dir": str(exchange_dir)})
    session_output.ensure_review_exchange_session_log(run_dir)
    def _finalize_manifest(outcome: str) -> None:
        session_output.update_manifest(
            run_dir,
            {
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "outcome": outcome,
            },
        )

    if on_started is not None:
        on_started(run_dir)

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

    runner = AgentRunner()
    current_round = 0
    try:
        early_outcome, current_round = _execute_review_exchange_rounds(
            session_output=session_output,
            runner=runner,
            worktree_path=worktree_path,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            coder_label=coder_label,
            reviewer_label=reviewer_label,
            coder_agent=coder_agent,
            reviewer_agent=reviewer_agent,
            max_rounds=max_rounds,
            max_no_progress=max_no_progress,
            require_validation=require_validation,
            web_port=web_port,
            emit=_emit,
        )
        if early_outcome is not None:
            _finalize_manifest(early_outcome.status)
            return early_outcome
    except Exception as exc:
        _finalize_manifest("failed")
        _emit(EventName.REVIEW_EXCHANGE_FAILED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": current_round,
            "error": str(exc),
            "exception_type": type(exc).__name__,
        })
        raise

    summary = _write_summary(exchange_dir, max_rounds, reviewer_response=None)
    _emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": max_rounds,
        "status": "stopped",
        "reason": "max_rounds_exceeded",
    })
    _finalize_manifest("stopped")
    return ReviewExchangeOutcome(
        status="stopped",
        rounds=max_rounds,
        reason="max_rounds_exceeded",
        reviewer_response=None,
        exchange_dir=exchange_dir,
        summary=summary,
    )


def _execute_review_exchange_rounds(  # noqa: PLR0913
    *,
    session_output: SessionOutput,
    runner: AgentRunner,
    worktree_path: Path,
    run_dir: Path,
    exchange_dir: Path,
    issue_number: int,
    issue_title: str,
    session_name: str,
    coder_label: str,
    reviewer_label: str,
    coder_agent: AgentConfig,
    reviewer_agent: AgentConfig,
    max_rounds: int,
    max_no_progress: int,
    require_validation: bool,
    web_port: int | None,
    emit: Callable[[EventName, dict[str, Any]], None],
) -> tuple[ReviewExchangeOutcome | None, int]:
    no_progress_count = 0
    last_reviewer_text: str | None = None
    last_coder_text: str | None = None
    current_round = 0

    for round_index in range(1, max_rounds + 1):
        current_round = round_index

        def _emit_reviewer_prompt_ready(round_index: int = round_index) -> None:
            emit(
                EventName.REVIEW_EXCHANGE_ROUND_STARTED,
                {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "round_index": round_index,
                },
            )
            emit(
                EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
                {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "round_index": round_index,
                    "role": "reviewer",
                },
            )

        reviewer_response = _run_reviewer_round(
            session_output=session_output,
            runner=runner,
            worktree_path=worktree_path,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            round_index=round_index,
            issue_number=issue_number,
            issue_title=issue_title,
            reviewer_agent=reviewer_agent,
            last_coder_text=last_coder_text,
            last_reviewer_text=last_reviewer_text,
            require_validation=require_validation,
            web_port=web_port,
            session_name=session_name,
            agent_label=reviewer_label,
            on_prompt_ready=_emit_reviewer_prompt_ready,
        )
        emit(
            EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "round_index": round_index,
                "role": "reviewer",
                "response_type": reviewer_response.response_type,
                "getting_closer": reviewer_response.getting_closer,
            },
        )
        outcome, no_progress_count, last_reviewer_text, last_coder_text = _process_exchange_round(
            session_output=session_output,
            runner=runner,
            reviewer_response=reviewer_response,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            round_index=round_index,
            issue_number=issue_number,
            issue_title=issue_title,
            session_name=session_name,
            coder_label=coder_label,
            coder_agent=coder_agent,
            require_validation=require_validation,
            web_port=web_port,
            no_progress_count=no_progress_count,
            max_no_progress=max_no_progress,
            last_reviewer_text=last_reviewer_text,
            last_coder_text=last_coder_text,
            emit=emit,
            worktree_path=worktree_path,
        )
        if outcome is not None:
            return outcome, current_round
    return None, current_round


def _process_exchange_round(  # noqa: PLR0913
    *,
    session_output: SessionOutput,
    runner: AgentRunner,
    reviewer_response: ReviewExchangeResponse,
    run_dir: Path,
    exchange_dir: Path,
    round_index: int,
    issue_number: int,
    issue_title: str,
    session_name: str,
    coder_label: str,
    coder_agent: AgentConfig,
    require_validation: bool,
    web_port: int | None,
    no_progress_count: int,
    max_no_progress: int,
    last_reviewer_text: str | None,
    last_coder_text: str | None,
    emit: Callable[[EventName, dict[str, Any]], None],
    worktree_path: Path,
) -> tuple[ReviewExchangeOutcome | None, int, str | None, str | None]:
    reviewer_response, done_outcome = _handle_reviewer_response(
        reviewer_response=reviewer_response,
        require_validation=require_validation,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        round_index=round_index,
        emit=emit,
        issue_number=issue_number,
        session_name=session_name,
    )
    if done_outcome is not None:
        return done_outcome, no_progress_count, last_reviewer_text, last_coder_text

    _write_round_log(
        exchange_dir=exchange_dir,
        round_index=round_index,
        role="reviewer",
        response=reviewer_response,
    )
    no_progress_count = _next_no_progress_count(
        current=no_progress_count,
        reviewer_response=reviewer_response,
    )
    if max_no_progress > 0 and no_progress_count >= max_no_progress:
        return _stop_for_no_progress(
            exchange_dir=exchange_dir,
            round_index=round_index,
            reviewer_response=reviewer_response,
            emit=emit,
            issue_number=issue_number,
            session_name=session_name,
        ), no_progress_count, last_reviewer_text, last_coder_text

    last_reviewer_text = reviewer_response.response_text

    def _emit_coder_prompt_ready(round_index: int = round_index) -> None:
        emit(
            EventName.REVIEW_REWORK_STARTED,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "round_index": round_index,
                "reviewer_response_type": reviewer_response.response_type,
                "reviewer_response_text": reviewer_response.response_text,
                "task": "rework",
            },
        )
        emit(
            EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "round_index": round_index,
                "role": "coder",
            },
        )

    coder_response, protocol_error = _run_coder_round_with_protocol_retries(
        session_output=session_output,
        runner=runner,
        worktree_path=worktree_path,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        round_index=round_index,
        issue_number=issue_number,
        issue_title=issue_title,
        coder_agent=coder_agent,
        reviewer_response=reviewer_response,
        web_port=web_port,
        session_name=session_name,
        agent_label=coder_label,
        require_validation=require_validation,
        on_prompt_ready=_emit_coder_prompt_ready,
    )
    if protocol_error is not None:
        emit(
            EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT,
            {
                "issue_number": issue_number,
                "session_name": session_name,
                "round_index": round_index,
                "role": "coder",
                "reason": "protocol_error",
                "detail": protocol_error,
            },
        )
        return _stop_for_protocol_error(
            exchange_dir=exchange_dir,
            round_index=round_index,
            reviewer_response=reviewer_response,
            protocol_error=protocol_error,
            emit=emit,
            issue_number=issue_number,
            session_name=session_name,
        ), no_progress_count, last_reviewer_text, last_coder_text
    emit(
        EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK,
        {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
            "role": "coder",
            "response_type": coder_response.response_type,
            "getting_closer": coder_response.getting_closer,
        },
    )

    _write_round_log(
        exchange_dir=exchange_dir,
        round_index=round_index,
        role="coder",
        response=coder_response,
    )
    emit(EventName.REVIEW_REWORK_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": reviewer_response.response_type,
        "reviewer_response_text": reviewer_response.response_text,
        "coder_response_type": coder_response.response_type,
        "coder_response_text": coder_response.response_text,
        "task": "rework",
    })
    emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": reviewer_response.response_type,
        "reviewer_response_text": reviewer_response.response_text,
        "coder_response_type": coder_response.response_type,
        "coder_response_text": coder_response.response_text,
    })
    last_coder_text = coder_response.response_text
    return None, no_progress_count, last_reviewer_text, last_coder_text


def _enforce_reviewer_validation(
    reviewer_response: ReviewExchangeResponse,
    *,
    require_validation: bool,
    run_dir: Path,
) -> ReviewExchangeResponse:
    if not require_validation or _validation_passed(run_dir):
        return reviewer_response
    return ReviewExchangeResponse(
        response_type="changes_requested",
        response_text=(
            "Validation record missing or failed. "
            "Validation is run per round by orchestrator; address "
            "the failing checks and continue."
        ),
        getting_closer=False,
        raw_json=reviewer_response.raw_json,
        raw_output=reviewer_response.raw_output,
    )


def _handle_reviewer_response(
    *,
    reviewer_response: ReviewExchangeResponse,
    require_validation: bool,
    run_dir: Path,
    exchange_dir: Path,
    round_index: int,
    emit: Any,
    issue_number: int,
    session_name: str,
) -> tuple[ReviewExchangeResponse, ReviewExchangeOutcome | None]:
    if reviewer_response.response_type != "ok":
        return reviewer_response, None
    reviewer_response = _enforce_reviewer_validation(
        reviewer_response,
        require_validation=require_validation,
        run_dir=run_dir,
    )
    if reviewer_response.response_type != "ok":
        return reviewer_response, None
    return reviewer_response, _complete_with_reviewer_ok(
        exchange_dir=exchange_dir,
        round_index=round_index,
        reviewer_response=reviewer_response,
        emit=emit,
        issue_number=issue_number,
        session_name=session_name,
    )


def _next_no_progress_count(current: int, reviewer_response: ReviewExchangeResponse) -> int:
    if reviewer_response.getting_closer is False:
        return current + 1
    return 0


def _complete_with_reviewer_ok(
    *,
    exchange_dir: Path,
    round_index: int,
    reviewer_response: ReviewExchangeResponse,
    emit: Any,
    issue_number: int,
    session_name: str,
) -> ReviewExchangeOutcome:
    _write_round_log(
        exchange_dir=exchange_dir,
        round_index=round_index,
        role="reviewer",
        response=reviewer_response,
    )
    summary = _write_summary(exchange_dir, round_index, reviewer_response)
    emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": reviewer_response.response_type,
        "reviewer_response_text": reviewer_response.response_text,
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
        reviewer_response=reviewer_response,
        exchange_dir=exchange_dir,
        summary=summary,
    )


def _stop_for_no_progress(
    *,
    exchange_dir: Path,
    round_index: int,
    reviewer_response: ReviewExchangeResponse,
    emit: Any,
    issue_number: int,
    session_name: str,
) -> ReviewExchangeOutcome:
    summary = _write_summary(exchange_dir, round_index, reviewer_response)
    emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": reviewer_response.response_type,
        "reviewer_response_text": reviewer_response.response_text,
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
        reviewer_response=reviewer_response,
        exchange_dir=exchange_dir,
        summary=summary,
    )


def _stop_for_protocol_error(
    *,
    exchange_dir: Path,
    round_index: int,
    reviewer_response: ReviewExchangeResponse,
    protocol_error: str,
    emit: Any,
    issue_number: int,
    session_name: str,
) -> ReviewExchangeOutcome:
    summary = _write_summary(exchange_dir, round_index, reviewer_response)
    emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": round_index,
        "reviewer_response_type": reviewer_response.response_type,
        "coder_response_type": "protocol_error",
    })
    emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "rounds": round_index,
        "status": "error",
        "reason": "coder_protocol_violation",
        "detail": protocol_error,
    })
    return ReviewExchangeOutcome(
        status="error",
        rounds=round_index,
        reason="coder_protocol_violation",
        reviewer_response=reviewer_response,
        exchange_dir=exchange_dir,
        summary=summary,
    )


def _run_reviewer_round(
    *,
    session_output: SessionOutput,
    runner: AgentRunner,
    worktree_path: Path,
    run_dir: Path,
    exchange_dir: Path,
    round_index: int,
    issue_number: int,
    issue_title: str,
    reviewer_agent: AgentConfig,
    last_coder_text: str | None,
    last_reviewer_text: str | None,
    require_validation: bool,
    web_port: int | None,
    session_name: str,
    agent_label: str,
    on_prompt_ready: Callable[[], None] | None = None,
) -> ReviewExchangeResponse:
    prompt = build_reviewer_prompt(
        issue_number=issue_number,
        issue_title=issue_title,
        round_index=round_index,
        last_coder_text=last_coder_text,
        last_reviewer_text=last_reviewer_text,
        require_validation=require_validation,
        run_dir=run_dir,
    )
    return _run_agent_round(
        session_output=session_output,
        runner=runner,
        worktree_path=worktree_path,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        round_index=round_index,
        issue_number=issue_number,
        issue_title=issue_title,
        session_name=session_name,
        agent=reviewer_agent,
        role="reviewer",
        agent_label=agent_label,
        prompt_text=prompt,
        web_port=web_port,
        on_prompt_ready=on_prompt_ready,
    )


def _run_coder_round(
    *,
    session_output: SessionOutput,
    runner: AgentRunner,
    worktree_path: Path,
    run_dir: Path,
    exchange_dir: Path,
    round_index: int,
    issue_number: int,
    issue_title: str,
    coder_agent: AgentConfig,
    reviewer_feedback: str,
    web_port: int | None,
    session_name: str,
    agent_label: str,
    on_prompt_ready: Callable[[], None] | None = None,
) -> ReviewExchangeResponse:
    prompt = build_coder_prompt(
        issue_number=issue_number,
        issue_title=issue_title,
        round_index=round_index,
        reviewer_feedback=reviewer_feedback,
        run_dir=run_dir,
    )
    return _run_agent_round(
        session_output=session_output,
        runner=runner,
        worktree_path=worktree_path,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        round_index=round_index,
        issue_number=issue_number,
        issue_title=issue_title,
        session_name=session_name,
        agent=coder_agent,
        role="coder",
        agent_label=agent_label,
        prompt_text=prompt,
        web_port=web_port,
        on_prompt_ready=on_prompt_ready,
    )


def _run_coder_round_with_protocol_retries(
    *,
    session_output: SessionOutput,
    runner: AgentRunner,
    worktree_path: Path,
    run_dir: Path,
    exchange_dir: Path,
    round_index: int,
    issue_number: int,
    issue_title: str,
    coder_agent: AgentConfig,
    reviewer_response: ReviewExchangeResponse,
    web_port: int | None,
    session_name: str,
    agent_label: str,
    require_validation: bool,
    on_prompt_ready: Callable[[], None] | None = None,
) -> tuple[ReviewExchangeResponse, str | None]:
    coder_feedback = reviewer_response.response_text
    coder_response = _run_coder_round(
        session_output=session_output,
        runner=runner,
        worktree_path=worktree_path,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        round_index=round_index,
        issue_number=issue_number,
        issue_title=issue_title,
        coder_agent=coder_agent,
        reviewer_feedback=coder_feedback,
        web_port=web_port,
        session_name=session_name,
        agent_label=agent_label,
        on_prompt_ready=on_prompt_ready,
    )
    protocol_error = _validate_coder_protocol(run_dir, require_validation=require_validation)
    retries_remaining = _CODER_PROTOCOL_RETRY_LIMIT
    while protocol_error is not None and retries_remaining > 0:
        retries_remaining -= 1
        coder_feedback = (
            f"{reviewer_response.response_text}\n\n"
            "Protocol error from orchestrator:\n"
            f"{protocol_error}\n"
            "You must: (1) run `coding-done completed --implementation '...' --problems '...'` "
            "to create completion/validation artifacts, then (2) write your JSON response to "
            "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE."
        )
        coder_response = _run_coder_round(
            session_output=session_output,
            runner=runner,
            worktree_path=worktree_path,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            round_index=round_index,
            issue_number=issue_number,
            issue_title=issue_title,
            coder_agent=coder_agent,
            reviewer_feedback=coder_feedback,
            web_port=web_port,
            session_name=session_name,
            agent_label=agent_label,
        )
        protocol_error = _validate_coder_protocol(run_dir, require_validation=require_validation)
    return coder_response, protocol_error


def _is_interactive_provider(agent_config: AgentConfig) -> bool:
    """Check if the agent uses an interactive provider (e.g. Claude Code TUI)."""
    provider_name = agent_config.provider or agent_config.ai_system
    if not provider_name:
        return False
    from ..agent_runner import get_provider, is_valid_provider
    if not is_valid_provider(provider_name):
        return False
    return get_provider(provider_name).interactive


def _run_interactive_round(
    runner: AgentRunner,
    spec: AgentSpec,
    response_file: Path,
) -> AgentResult:
    """Delegate to the runner's interactive method (subprocess-based).

    Uses ``subprocess.Popen`` instead of the pexpect-based ``runner.start()``
    to avoid forking from a multi-threaded process (uvicorn + SSE threads),
    which crashes on macOS with "multi-threaded process forked".
    """
    return runner.run_interactive(spec, response_file)


def _run_agent_round(
    *,
    session_output: SessionOutput,
    runner: AgentRunner,
    worktree_path: Path,
    run_dir: Path,
    exchange_dir: Path,
    round_index: int,
    issue_number: int,
    issue_title: str,
    session_name: str,
    agent: AgentConfig,
    role: str,
    agent_label: str,
    prompt_text: str,
    web_port: int | None,
    on_prompt_ready: Callable[[], None] | None = None,
) -> ReviewExchangeResponse:
    prompt_path = _write_prompt(exchange_dir, round_index, role, prompt_text)
    _append_session_log(
        session_output,
        run_dir,
        round_index=round_index,
        role=role,
        section="prompt",
        content=prompt_text,
    )
    if on_prompt_ready is not None:
        on_prompt_ready()
    prompt_rel = prompt_path.relative_to(worktree_path)
    agent_config = AgentConfig(
        prompt_path=prompt_path,
        prompt_relative=str(prompt_rel),
        provider=_resolve_provider(agent),
        model=agent.model,
        timeout_minutes=agent.timeout_minutes,
        provider_args=dict(agent.provider_args),
        permission_mode=agent.permission_mode,
        skip_review=agent.skip_review,
        reviewer=agent.reviewer,
        command=agent.command,
        meta_agent=agent.meta_agent,
        initial_prompt=(
            "Follow the instructions in {prompt}. "
            "Write exactly one line of JSON to the file at $ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE "
            "and then exit."
        ),
        ai_system=agent.ai_system,
        retry_prompt_template=agent.retry_prompt_template,
    )

    command_str = agent_config.get_command(
        issue_number=issue_number,
        issue_title=issue_title,
        worktree=worktree_path,
        task_kind=f"review_exchange_{role}",
    )
    command = shlex.split(command_str)

    round_dir = exchange_dir / f"round-{round_index:03d}" / role
    round_dir.mkdir(parents=True, exist_ok=True)
    response_file = run_dir / REVIEW_RESPONSE_FILENAME
    # Remove stale response from previous round so we don't read it back
    response_file.unlink(missing_ok=True)
    env_overrides = _build_env_overrides(
        run_dir,
        worktree_path=worktree_path,
        role=role,
        agent_label=agent_label,
        web_port=web_port,
        issue_number=issue_number,
        session_name=session_name,
    )
    spec = AgentSpec(
        command=command,
        working_dir=worktree_path,
        timeout_seconds=agent.timeout_minutes * 60,
        log_path=round_dir / TERMINAL_RECORDING_FILENAME,
        additional_recording_paths=[run_dir / TERMINAL_RECORDING_FILENAME],
        mirror_log_path=round_dir / "agent-output.log",
        output_dir=round_dir,
        env_overrides=env_overrides,
    )

    # For interactive providers (e.g. Claude Code TUI), the prompt is already
    # in the command as a positional arg.  We start the TUI, poll for the
    # response file, then kill the process.  Non-interactive providers use
    # the classic run-and-wait path (they include -p and exit on their own).
    interactive = _is_interactive_provider(agent_config)
    if interactive:
        result = _run_interactive_round(runner, spec, response_file)
    else:
        result = runner.run(spec)

    # Read structured response from file (agent writes here instead of stdout)
    response_text = ""
    if response_file.exists():
        response_text = response_file.read_text(encoding="utf-8", errors="replace")

    _append_provider_runner_logs(
        run_dir,
        round_index=round_index,
        role=role,
        response_text=response_text,
        stderr=result.stderr or "",
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        succeeded=result.succeeded,
    )
    _append_session_log(
        session_output,
        run_dir,
        round_index=round_index,
        role=role,
        section="runner_result",
        content=(
            f"exit_code={result.exit_code} timed_out={result.timed_out} "
            f"succeeded={result.succeeded}\n"
            f"response_file:\n{response_text or '(empty)'}\n\n"
            f"stderr:\n{result.stderr or '(empty)'}"
        ),
    )
    # For interactive providers, forced kill yields non-zero exit — that's expected.
    # Success is determined by whether the response file was written and is parseable.
    # For non-interactive providers, a non-zero exit with no response file is a real failure.
    if not result.succeeded and not (interactive and response_text):
        stderr_snippet = result.stderr.strip().splitlines()
        stderr_preview = "\n".join(stderr_snippet[:6]) if stderr_snippet else "No stderr captured."
        return ReviewExchangeResponse(
            response_type="error",
            response_text=(
                "Agent run failed. "
                f"exit_code={result.exit_code} timed_out={result.timed_out}. "
                f"stderr:\n{stderr_preview}"
            ),
            raw_output=f"response_file:\n{response_text}\n\nstderr:\n{result.stderr}",
        )
    response = parse_exchange_response(response_text)
    if response is None:
        return ReviewExchangeResponse(
            response_type="error",
            response_text="Unable to parse JSON response from agent output.",
            raw_output=response_text,
        )
    return response


def _append_provider_runner_logs(
    run_dir: Path,
    *,
    round_index: int,
    role: str,
    response_text: str,
    stderr: str,
    exit_code: int | None,
    timed_out: bool,
    succeeded: bool,
) -> None:
    """Mirror round output into run-scoped provider-runner logs for UI/E2E parity."""
    output_dir = run_dir / "provider-runner"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    header = (
        f"[{timestamp}] round={round_index} role={role} "
        f"exit_code={exit_code} timed_out={timed_out} succeeded={succeeded}\n"
    )
    _append_text(output_dir / "stdout.log", header + (response_text.rstrip() or "(empty)") + "\n\n")
    _append_text(output_dir / "stderr.log", header + (stderr.rstrip() or "(empty)") + "\n\n")


def _append_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)


def _append_session_log(
    session_output: SessionOutput,
    run_dir: Path,
    *,
    round_index: int,
    role: str,
    section: str,
    content: str,
) -> None:
    """Append review-exchange transcript content to the dedicated exchange transcript."""
    session_output.append_review_exchange_session_log_entry(
        run_dir,
        round_index=round_index,
        role=role,
        section=section,
        content=content,
    )


def _build_env_overrides(
    run_dir: Path,
    *,
    worktree_path: Path,
    role: str,
    agent_label: str,
    web_port: int | None,
    issue_number: int,
    session_name: str,
) -> dict[str, str]:
    completion_path = f".issue-orchestrator/sessions/{run_dir.name}/completion-{role}.json"
    env = {
        f"{ENV_PREFIX}COMPLETION_PATH": completion_path,
        f"{ENV_PREFIX}VALIDATION_OUTPUT_DIR": str(run_dir),
        f"{ENV_PREFIX}AGENT_LABEL": agent_label,
        f"{ENV_PREFIX}ISSUE_NUMBER": str(issue_number),
        f"{ENV_PREFIX}REVIEW_RESPONSE_FILE": str(run_dir / REVIEW_RESPONSE_FILENAME),
        "ORCHESTRATOR_ISSUE_NUMBER": str(issue_number),
        "ORCHESTRATOR_SESSION_ID": session_name,
    }
    env.update(build_runtime_tool_env(worktree_path, base_env={}))
    if web_port is not None:
        env["ORCHESTRATOR_API_PORT"] = str(web_port)
    return env


def _validation_passed(run_dir: Path) -> bool:
    record_path = run_dir / "validation-record.json"
    if not record_path.exists():
        return False
    try:
        data = json.loads(record_path.read_text())
    except json.JSONDecodeError:
        return False
    return bool(data.get("passed"))


def _seed_validation_record(
    *,
    run_dir: Path,
    source_record_path: Path | None,
    session_output: SessionOutput,
) -> None:
    """Seed review-exchange run with a prior validation record when available."""
    if source_record_path is None or not source_record_path.exists():
        return
    target = run_dir / "validation-record.json"
    if target.exists():
        return
    try:
        shutil.copy2(source_record_path, target)
    except OSError:
        logger.debug(
            "Failed to seed validation record into review-exchange run_dir: %s -> %s",
            source_record_path,
            target,
        )
        return
    session_output.update_manifest(run_dir, {"validation_record_path": str(target)})


def _check_validation_record(run_dir: Path) -> str | None:
    """Check that validation-record.json exists, is valid, and passed."""
    validation_path = run_dir / "validation-record.json"
    if not validation_path.exists():
        return f"missing validation artifact: {validation_path}"
    if validation_path.stat().st_size <= 0:
        return f"validation artifact is empty: {validation_path}"
    try:
        vdata = json.loads(validation_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return f"validation artifact is not valid JSON: {validation_path}"
    if not vdata.get("passed"):
        msg = (
            f"validation failed (exit_code={vdata.get('exit_code', '?')}). "
            "Fix the failures and re-run coding-done."
        )
        stderr_path = vdata.get("stderr_path")
        if stderr_path:
            sp = Path(stderr_path) if Path(stderr_path).is_absolute() else run_dir / stderr_path
            try:
                if sp.is_file():
                    msg += f"\nValidation output:\n{sp.read_text(encoding='utf-8', errors='replace')[:2000]}"
            except OSError:
                pass
        return msg
    return None


def _validate_coder_protocol(run_dir: Path, *, require_validation: bool) -> str | None:
    completion_path = run_dir / "completion-coder.json"
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
    if require_validation:
        return _check_validation_record(run_dir)
    return None


def _write_prompt(exchange_dir: Path, round_index: int, role: str, prompt_text: str) -> Path:
    prompt_dir = exchange_dir / f"round-{round_index:03d}"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / f"{role}-prompt.txt"
    prompt_path.write_text(prompt_text)
    return prompt_path


_ATOMIC_WRITE_TMP_PREFIX = "."
_ATOMIC_WRITE_TMP_SUFFIX = ".tmp"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically so mid-write polls never see a torn file.

    The main orchestrator tick polls ``summary.json`` every iteration to
    detect async review-exchange completion. A non-atomic write (plain
    ``write_text``) creates a window where a reader can hit a partial file
    and raise :class:`json.JSONDecodeError`. Write to a sibling temp file on
    the same filesystem and rename — the rename is atomic on POSIX, so any
    reader sees either the pre-write content or the full new content.

    Orphaned tempfiles from a ``kill -9`` between ``mkstemp`` and
    ``os.replace`` are cleaned up by
    :func:`sweep_atomic_write_tempfiles` at orchestrator startup.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f"{_ATOMIC_WRITE_TMP_PREFIX}{path.name}.",
        suffix=_ATOMIC_WRITE_TMP_SUFFIX,
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(encoded)
        os.replace(tmp_path_str, path)
    except Exception:
        # Only clean up the tempfile on failure; the successful path has
        # already renamed it out of existence.
        try:
            os.unlink(tmp_path_str)
        except FileNotFoundError:
            pass
        raise


def sweep_atomic_write_tempfiles(exchange_dirs_root: Path) -> int:
    """Remove orphaned ``_atomic_write_json`` tempfiles under *exchange_dirs_root*.

    ``_atomic_write_json`` normally self-cleans in both success (rename) and
    failure (explicit unlink) paths. The one case where it can't: an external
    ``kill -9`` between ``mkstemp`` and ``os.replace``. Those tempfiles
    accumulate silently in per-run ``review-exchange/`` directories. Runs
    once at orchestrator startup; O(tempfiles found), not O(all files).
    Returns the number of tempfiles removed, for logging.
    """
    if not exchange_dirs_root.exists():
        return 0
    removed = 0
    for tmp_path in exchange_dirs_root.rglob(
        f"{_ATOMIC_WRITE_TMP_PREFIX}*{_ATOMIC_WRITE_TMP_SUFFIX}"
    ):
        # Belt and suspenders: only touch files whose surrounding dir looks
        # like a review-exchange run dir. Prevents an overly-broad root from
        # nuking unrelated dotfiles.
        if tmp_path.parent.name != "review-exchange":
            continue
        try:
            tmp_path.unlink()
            removed += 1
        except OSError:
            # Next startup will retry; don't block boot on sweep failures.
            continue
    return removed


def _write_round_log(
    *,
    exchange_dir: Path,
    round_index: int,
    role: str,
    response: ReviewExchangeResponse,
) -> None:
    payload = {
        "round_index": round_index,
        "role": role,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "response_type": response.response_type,
        "response_text": response.response_text,
        "getting_closer": response.getting_closer,
        "raw_json": response.raw_json,
    }
    round_path = exchange_dir / f"round-{round_index:03d}.json"
    existing: dict[str, Any] = {}
    if round_path.exists():
        try:
            existing = json.loads(round_path.read_text())
        except json.JSONDecodeError:
            existing = {}
    existing[role] = payload
    _atomic_write_json(round_path, existing)


def _write_summary(
    exchange_dir: Path,
    round_index: int,
    reviewer_response: ReviewExchangeResponse | None,
) -> dict[str, Any]:
    summary = {
        "completed_rounds": round_index,
        "status": reviewer_response.response_type if reviewer_response else "unknown",
        "response_text": reviewer_response.response_text if reviewer_response else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(exchange_dir / "summary.json", summary)
    return summary
