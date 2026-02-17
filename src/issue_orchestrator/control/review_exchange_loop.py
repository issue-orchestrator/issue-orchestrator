"""MCP review exchange loop runner."""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agent_runner import AgentRunner, RunSpec
from ..domain.models import AgentConfig
from ..infra.logging_config import get_repo_log_path
from ..infra.env import ENV_PREFIX
from ..ports.session_output import SessionOutput
from ..ports import EventSink, TraceEvent
from ..events import EventName, EventContext

logger = logging.getLogger(__name__)


def _escape_claude_project_path(path: Path) -> str:
    cleaned = str(path).lstrip("/")
    return "-" + cleaned.replace("/", "-")


@dataclass(frozen=True)
class ReviewExchangeResponse:
    response_type: str
    response_text: str
    getting_closer: bool | None = None
    raw_json: dict[str, Any] | None = None
    raw_output: str | None = None


@dataclass(frozen=True)
class ReviewExchangeOutcome:
    status: str  # "ok" | "stopped" | "error"
    rounds: int
    reason: str
    reviewer_response: ReviewExchangeResponse | None = None
    exchange_dir: Path | None = None
    summary: dict[str, Any] | None = None


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
    web_port: int | None,
    events: EventSink | None = None,
    event_context: EventContext | None = None,
) -> ReviewExchangeOutcome:
    """Run the coder↔reviewer exchange loop and capture round-trip logs."""
    def _emit(event_name: EventName, payload: dict[str, Any]) -> None:
        if events is None or event_context is None:
            return
        events.publish(TraceEvent(event_name, event_context.enrich(payload)))

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
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    session_output.update_manifest(run_dir, {"review_exchange_dir": str(exchange_dir)})

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
    no_progress_count = 0
    last_reviewer_text: str | None = None
    last_coder_text: str | None = None

    current_round = 0
    try:
        for round_index in range(1, max_rounds + 1):
            current_round = round_index
            _emit(EventName.REVIEW_EXCHANGE_ROUND_STARTED, {
                "issue_number": issue_number,
                "session_name": session_name,
                "round_index": round_index,
            })
            reviewer_response = _run_reviewer_round(
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
            )

            if reviewer_response.response_type == "ok":
                if require_validation and not _validation_passed(run_dir):
                    reviewer_response = ReviewExchangeResponse(
                        response_type="changes_requested",
                        response_text=(
                            "Validation record missing or failed. "
                            "Run make validate and record it via agent-done."
                        ),
                        getting_closer=False,
                        raw_json=reviewer_response.raw_json,
                        raw_output=reviewer_response.raw_output,
                    )
                else:
                    _write_round_log(
                        exchange_dir=exchange_dir,
                        round_index=round_index,
                        role="reviewer",
                        response=reviewer_response,
                    )
                    summary = _write_summary(exchange_dir, round_index, reviewer_response)
                    _emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
                        "issue_number": issue_number,
                        "session_name": session_name,
                        "round_index": round_index,
                        "reviewer_response_type": reviewer_response.response_type,
                        "coder_response_type": None,
                    })
                    _emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
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
            _write_round_log(
                exchange_dir=exchange_dir,
                round_index=round_index,
                role="reviewer",
                response=reviewer_response,
            )

            if reviewer_response.getting_closer is False:
                no_progress_count += 1
            else:
                no_progress_count = 0

            if max_no_progress > 0 and no_progress_count >= max_no_progress:
                summary = _write_summary(exchange_dir, round_index, reviewer_response)
                _emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "round_index": round_index,
                    "reviewer_response_type": reviewer_response.response_type,
                    "coder_response_type": None,
                })
                _emit(EventName.REVIEW_EXCHANGE_COMPLETED, {
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

            last_reviewer_text = reviewer_response.response_text

            coder_response = _run_coder_round(
                runner=runner,
                worktree_path=worktree_path,
                run_dir=run_dir,
                exchange_dir=exchange_dir,
                round_index=round_index,
                issue_number=issue_number,
                issue_title=issue_title,
                coder_agent=coder_agent,
                reviewer_feedback=reviewer_response.response_text,
                web_port=web_port,
                session_name=session_name,
                agent_label=coder_label,
            )
            _write_round_log(
                exchange_dir=exchange_dir,
                round_index=round_index,
                role="coder",
                response=coder_response,
            )
            _emit(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED, {
                "issue_number": issue_number,
                "session_name": session_name,
                "round_index": round_index,
                "reviewer_response_type": reviewer_response.response_type,
                "coder_response_type": coder_response.response_type,
            })
            last_coder_text = coder_response.response_text
    except Exception as exc:
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
    return ReviewExchangeOutcome(
        status="stopped",
        rounds=max_rounds,
        reason="max_rounds_exceeded",
        reviewer_response=None,
        exchange_dir=exchange_dir,
        summary=summary,
    )


def _run_reviewer_round(
    *,
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
) -> ReviewExchangeResponse:
    prompt = _build_reviewer_prompt(
        issue_number=issue_number,
        issue_title=issue_title,
        round_index=round_index,
        last_coder_text=last_coder_text,
        last_reviewer_text=last_reviewer_text,
        require_validation=require_validation,
        run_dir=run_dir,
    )
    return _run_agent_round(
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
    )


def _run_coder_round(
    *,
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
) -> ReviewExchangeResponse:
    prompt = _build_coder_prompt(
        issue_number=issue_number,
        issue_title=issue_title,
        round_index=round_index,
        reviewer_feedback=reviewer_feedback,
        run_dir=run_dir,
    )
    return _run_agent_round(
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
    )


def _run_agent_round(
    *,
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
) -> ReviewExchangeResponse:
    prompt_path = _write_prompt(exchange_dir, round_index, role, prompt_text)
    prompt_rel = prompt_path.relative_to(worktree_path)
    agent_config = AgentConfig(
        prompt_path=prompt_path,
        prompt_relative=str(prompt_rel),
        provider=agent.provider,
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
            "Respond with exactly one line of JSON and then exit."
        ),
        ai_system=agent.ai_system,
        retry_prompt_template=agent.retry_prompt_template,
    )

    command_str = agent_config.get_command(
        issue_number=issue_number,
        issue_title=issue_title,
        worktree=worktree_path,
    )
    command = shlex.split(command_str)

    round_dir = exchange_dir / f"round-{round_index:03d}" / role
    round_dir.mkdir(parents=True, exist_ok=True)
    env_overrides = _build_env_overrides(
        run_dir,
        role=role,
        agent_label=agent_label,
        web_port=web_port,
        issue_number=issue_number,
        session_name=session_name,
    )
    spec = RunSpec(
        command=command,
        working_dir=worktree_path,
        timeout_seconds=agent.timeout_minutes * 60,
        output_dir=round_dir,
        env_overrides=env_overrides,
    )
    result = runner.run(spec)
    if not result.succeeded:
        stderr_snippet = result.stderr.strip().splitlines()
        stderr_preview = "\n".join(stderr_snippet[:6]) if stderr_snippet else "No stderr captured."
        return ReviewExchangeResponse(
            response_type="error",
            response_text=(
                "Agent run failed. "
                f"exit_code={result.exit_code} timed_out={result.timed_out}. "
                f"stderr:\n{stderr_preview}"
            ),
            raw_output=f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}",
        )
    response = _parse_exchange_response(result.stdout)
    if response is None:
        return ReviewExchangeResponse(
            response_type="error",
            response_text="Unable to parse JSON response from agent output.",
            raw_output=result.stdout,
        )
    return response


def _build_env_overrides(
    run_dir: Path,
    *,
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
        "ORCHESTRATOR_ISSUE_NUMBER": str(issue_number),
        "ORCHESTRATOR_SESSION_ID": session_name,
    }
    if web_port is not None:
        env["ORCHESTRATOR_API_PORT"] = str(web_port)
    return env


def _build_reviewer_prompt(
    *,
    issue_number: int,
    issue_title: str,
    round_index: int,
    last_coder_text: str | None,
    last_reviewer_text: str | None,
    require_validation: bool,
    run_dir: Path,
) -> str:
    validation_note = ""
    if require_validation:
        validation_note = (
            "Validation is required. Only respond ok if validation-record.json exists "
            f"and passed in {run_dir}. If missing or failed, respond changes_requested "
            "asking the coder to run make validate via agent-done."
        )
    prior = ""
    if last_coder_text:
        prior += f"\nCoder response:\n{last_coder_text}\n"
    if last_reviewer_text:
        prior += f"\nPrevious review feedback:\n{last_reviewer_text}\n"
    return (
        f"You are the reviewer in a coder↔reviewer exchange for issue #{issue_number}: {issue_title}.\n"
        f"Round {round_index}.\n"
        f"{validation_note}\n"
        "Review the current worktree changes.\n"
        "Consider:\n"
        "A) the changes for this issue\n"
        "B) relevant context in the broader codebase\n"
        "C) any applicable .claude/skills guidance\n"
        "D) docs/ if needed for intended behavior\n"
        f"{prior}\n"
        "Respond with exactly one line of JSON:\n"
        "{\"response_type\":\"ok|changes_requested|disagree\","
        "\"getting_closer\":true|false,"
        "\"response_text\":\"...\"}\n"
    )


def _build_coder_prompt(
    *,
    issue_number: int,
    issue_title: str,
    round_index: int,
    reviewer_feedback: str,
    run_dir: Path,
) -> str:
    return (
        f"You are the coder in a review exchange for issue #{issue_number}: {issue_title}.\n"
        f"Round {round_index}.\n"
        "Review the feedback below and update the worktree accordingly.\n"
        "If you disagree, set response_type=disagree and explain why.\n"
        "Otherwise apply fixes, run make validate, and record it with agent-done.\n"
        f"Session output dir: {run_dir}\n"
        f"Reviewer feedback:\n{reviewer_feedback}\n"
        "Respond with exactly one line of JSON:\n"
        "{\"response_type\":\"ok|disagree\",\"response_text\":\"...\"}\n"
    )


def _parse_exchange_response(stdout: str) -> ReviewExchangeResponse | None:
    if not stdout:
        return None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        response_type = str(data.get("response_type", "")).strip()
        response_text = str(data.get("response_text", "")).strip()
        getting_closer = data.get("getting_closer")
        if response_type and response_text:
            return ReviewExchangeResponse(
                response_type=response_type,
                response_text=response_text,
                getting_closer=bool(getting_closer) if getting_closer is not None else None,
                raw_json=data,
                raw_output=stdout,
            )
    return None


def _validation_passed(run_dir: Path) -> bool:
    record_path = run_dir / "validation-record.json"
    if not record_path.exists():
        return False
    try:
        data = json.loads(record_path.read_text())
    except json.JSONDecodeError:
        return False
    return bool(data.get("passed"))


def _write_prompt(exchange_dir: Path, round_index: int, role: str, prompt_text: str) -> Path:
    prompt_dir = exchange_dir / f"round-{round_index:03d}"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / f"{role}-prompt.txt"
    prompt_path.write_text(prompt_text)
    return prompt_path


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
    round_path.write_text(json.dumps(existing, indent=2))


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
    summary_path = exchange_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    return summary
