"""Response-file polling for persistent PTY rounds."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..domain.review_exchange_failures import RoundFailureReason
from .persistent_round_runner import (
    PersistentRoundError,
    PersistentRoundTimeoutError,
    PersistentSession,
    ResponseRejection,
    ResponseVerifier,
)

logger = logging.getLogger(__name__)

_SEND_ROUND_HEARTBEAT_SECONDS = 30.0


def wait_for_round_response(
    session: PersistentSession,
    *,
    response_file: Path,
    started_at: float,
    deadline: float,
    timeout_seconds: float,
    poll_interval_seconds: float,
    response_drain_seconds: float,
    prompt_acceptance_idle_seconds: float | None,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    label: str,
    response_verifier: ResponseVerifier | None,
    on_rejected_response: Callable[[], None] | None,
    drain_pty_output: Callable[[PersistentSession], int],
    drain_pty_output_until_quiet: Callable[..., None],
) -> dict[str, Any]:
    """Poll until the response file parses as JSON, the agent exits, or timeout."""
    last_heartbeat = now()
    last_activity_at = last_heartbeat
    last_recording_size = safe_recording_size(session)
    last_rejection: ResponseRejection | None = None
    poll_iter = 0
    bytes_drained_total = 0
    while now() < deadline:
        poll_iter += 1
        current = now()
        drained = drain_pty_output(session)
        bytes_drained_total += drained
        recording_size = safe_recording_size(session)
        recording_grew = (
            last_recording_size is not None
            and recording_size is not None
            and recording_size > last_recording_size
        )
        if drained or recording_grew:
            last_activity_at = current
        if recording_size is not None:
            last_recording_size = recording_size
        accepted, rejection = _handle_parsed_round_response(
            session,
            response_file=response_file,
            response_verifier=response_verifier,
            response_drain_seconds=response_drain_seconds,
            now=now,
            sleep=sleep,
            label=label,
            started_at=started_at,
            poll_iter=poll_iter,
            bytes_drained_total=bytes_drained_total,
            on_rejected_response=on_rejected_response,
            drain_pty_output_until_quiet=drain_pty_output_until_quiet,
        )
        if accepted is not None:
            return accepted
        if rejection is not None:
            last_rejection = rejection
            ret = session.proc.poll()
            if ret is not None:
                _raise_exited_after_rejected_response(
                    session,
                    response_file=response_file,
                    rejection=rejection,
                    label=label,
                    exit_code=ret,
                )
            sleep(poll_interval_seconds)
            continue
        ret = session.proc.poll()
        if ret is not None:
            return _response_at_exit_or_raise(
                session,
                response_file=response_file,
                response_verifier=response_verifier,
                label=label,
                started_at=started_at,
                now=now,
                poll_iter=poll_iter,
                bytes_drained_total=bytes_drained_total,
                on_rejected_response=on_rejected_response,
            )
        idle_for = now() - last_activity_at
        if (
            prompt_acceptance_idle_seconds is not None
            and idle_for >= prompt_acceptance_idle_seconds
        ):
            logger.warning(
                "[send_round] prompt not accepted role=%s pid=%d after %.1fs idle "
                "(elapsed=%.1fs poll_iters=%d bytes_drained=%d "
                "response_file_exists=%s recording_bytes=%s)",
                label,
                session.proc.pid,
                idle_for,
                now() - started_at,
                poll_iter,
                bytes_drained_total,
                response_file.exists(),
                recording_size if recording_size is not None else "n/a",
            )
            raise PersistentRoundTimeoutError(
                "Agent did not produce terminal output or a response after "
                f"prompt delivery for {idle_for:.1f}s",
                failure_reason=RoundFailureReason.PROMPT_NOT_ACCEPTED,
            )
        if now() - last_heartbeat >= _SEND_ROUND_HEARTBEAT_SECONDS:
            logger.info(
                "[send_round] heartbeat role=%s pid=%d alive=%s elapsed=%.0fs "
                "deadline_in=%.0fs poll_iters=%d bytes_drained=%d "
                "idle_for=%.0fs response_file_exists=%s recording_bytes=%s",
                label,
                session.proc.pid,
                session.proc.poll() is None,
                now() - started_at,
                deadline - now(),
                poll_iter,
                bytes_drained_total,
                idle_for,
                response_file.exists(),
                recording_size if recording_size is not None else "n/a",
            )
            last_heartbeat = now()
        sleep(poll_interval_seconds)
    logger.warning(
        "[send_round] timeout role=%s pid=%d after %.1fs "
        "(poll_iters=%d bytes_drained=%d response_file_exists=%s)",
        label,
        session.proc.pid,
        timeout_seconds,
        poll_iter,
        bytes_drained_total,
        response_file.exists(),
    )
    raise PersistentRoundTimeoutError(
        _round_timeout_message(
            response_file,
            timeout_seconds=timeout_seconds,
            last_rejection=last_rejection,
        )
    )


def response_rejection(
    parsed: Mapping[str, Any],
    response_verifier: ResponseVerifier | None,
) -> ResponseRejection | None:
    if response_verifier is None:
        return None
    return response_verifier(parsed)


def _handle_parsed_round_response(
    session: PersistentSession,
    *,
    response_file: Path,
    response_verifier: ResponseVerifier | None,
    response_drain_seconds: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    label: str,
    started_at: float,
    poll_iter: int,
    bytes_drained_total: int,
    on_rejected_response: Callable[[], None] | None,
    drain_pty_output_until_quiet: Callable[..., None],
) -> tuple[dict[str, Any] | None, ResponseRejection | None]:
    parsed = try_read_response(response_file)
    if parsed is None:
        return None, None
    rejection = response_rejection(parsed, response_verifier)
    if rejection is not None:
        discard_rejected_response(
            session,
            response_file=response_file,
            rejection=rejection,
            label=label,
            elapsed_seconds=now() - started_at,
            on_rejected_response=on_rejected_response,
        )
        return None, rejection
    drain_pty_output_until_quiet(
        session,
        quiet_seconds=response_drain_seconds,
        now=now,
        sleep=sleep,
    )
    logger.info(
        "[send_round] response received role=%s pid=%d in %.1fs "
        "(poll_iters=%d bytes_drained=%d)",
        label,
        session.proc.pid,
        now() - started_at,
        poll_iter,
        bytes_drained_total,
    )
    return parsed, None


def _response_at_exit_or_raise(
    session: PersistentSession,
    *,
    response_file: Path,
    response_verifier: ResponseVerifier | None,
    label: str,
    started_at: float,
    now: Callable[[], float],
    poll_iter: int,
    bytes_drained_total: int,
    on_rejected_response: Callable[[], None] | None,
) -> dict[str, Any]:
    ret = session.proc.poll()
    if ret is None:
        raise RuntimeError("response-at-exit helper called for a live process")
    final = try_read_response(response_file)
    if final is not None:
        rejection = response_rejection(final, response_verifier)
        if rejection is None:
            logger.info(
                "[send_round] response received at exit role=%s pid=%d "
                "exit_code=%d in %.1fs",
                label,
                session.proc.pid,
                ret,
                now() - started_at,
            )
            return final
        discard_rejected_response(
            session,
            response_file=response_file,
            rejection=rejection,
            label=label,
            elapsed_seconds=now() - started_at,
            on_rejected_response=on_rejected_response,
        )
        _raise_exited_after_rejected_response(
            session,
            response_file=response_file,
            rejection=rejection,
            label=label,
            exit_code=ret,
        )
    if response_file.exists():
        logger.warning(
            "[send_round] agent exited with invalid JSON role=%s pid=%d "
            "exit_code=%d response_file=%s",
            label,
            session.proc.pid,
            ret,
            response_file,
        )
        raise PersistentRoundError(
            f"Agent exited (code={ret}) leaving invalid JSON in {response_file}",
            failure_reason=RoundFailureReason.INVALID_RESPONSE,
        )
    logger.warning(
        "[send_round] agent exited before responding role=%s pid=%d "
        "exit_code=%d after %.1fs (poll_iters=%d bytes_drained=%d)",
        label,
        session.proc.pid,
        ret,
        now() - started_at,
        poll_iter,
        bytes_drained_total,
    )
    raise PersistentRoundError(
        f"Agent exited unexpectedly (code={ret}) before responding",
        failure_reason=RoundFailureReason.PROCESS_EXITED_BEFORE_RESPONSE,
    )


def _raise_exited_after_rejected_response(
    session: PersistentSession,
    *,
    response_file: Path,
    rejection: ResponseRejection,
    label: str,
    exit_code: int,
) -> None:
    logger.warning(
        "[send_round] agent exited after rejected response "
        "role=%s pid=%d exit_code=%d response_file=%s",
        label,
        session.proc.pid,
        exit_code,
        response_file,
    )
    raise PersistentRoundError(
        "Agent exited after writing a rejected response "
        f"({rejection.reason}): {rejection.detail}",
        failure_reason=RoundFailureReason.INVALID_RESPONSE,
    )


def discard_rejected_response(
    session: PersistentSession,
    *,
    response_file: Path,
    rejection: ResponseRejection,
    label: str,
    elapsed_seconds: float,
    on_rejected_response: Callable[[], None] | None,
) -> None:
    logger.warning(
        "[send_round] discarding rejected response role=%s pid=%d "
        "elapsed=%.1fs reason=%s response_file=%s detail=%s",
        label,
        session.proc.pid,
        elapsed_seconds,
        rejection.reason,
        response_file,
        rejection.detail,
    )
    if on_rejected_response is not None:
        on_rejected_response()
    response_file.unlink(missing_ok=True)


def _round_timeout_message(
    response_file: Path,
    *,
    timeout_seconds: float,
    last_rejection: ResponseRejection | None,
) -> str:
    message = (
        f"Agent did not produce valid JSON in {response_file} within "
        f"{timeout_seconds}s"
    )
    if last_rejection is None:
        return message
    return (
        f"{message}; last rejected response reason={last_rejection.reason}: "
        f"{last_rejection.detail}"
    )


def safe_recording_size(session: PersistentSession) -> int | None:
    """Best-effort read of the role recording's current size in bytes."""
    log_writer = session.log_writer
    if log_writer is None:
        return None
    recording_path = getattr(log_writer, "recording_path", None)
    if recording_path is None or not recording_path.exists():
        return None
    try:
        return recording_path.stat().st_size
    except OSError:
        return None


def try_read_response(response_file: Path) -> dict[str, Any] | None:
    """Return the parsed JSON if the file exists and parses, else None."""
    if not response_file.exists():
        return None
    try:
        text = response_file.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed
