from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..domain.exchange_chapter import (
    CHAPTER_SECTION_PROMPT,
    ExchangeChapter,
    ExchangeChapterSidecar,
)
from ..execution.manifest_accessor import ArtifactNotFoundError, ManifestAccessor, RunIdentity
from ..execution.review_exchange_transcript import (
    filter_review_exchange_transcript,
    parse_review_exchange_transcript,
    render_review_exchange_transcript,
)
from ..execution.session_output_adapter import EXCHANGE_CHAPTERS_NAME
from ..execution.validation_failure_summary import load_validation_failure_summary
from ..infra.claude_jsonl import claude_jsonl_entry_preview_lines
from ..infra.session_log_prettify import (
    extract_codex_transcript,
    prettify_session_log,
)
from ..infra.terminal_recording import first_terminal_geometry, iter_terminal_recording
from .timeline_presentation import _format_phase_name, _phase_status_icon, _positive_int
from .web_session_context import (
    WebOrchestratorDependency,
    resolve_issue_session_context,
    worktree_path_from_run_dir,
)

logger = logging.getLogger(__name__)

web_session_router = APIRouter()


def _load_phase_chapter_sidecar(
    recording_path: Path,
    role: str,
) -> ExchangeChapterSidecar | None:
    """Read the chapter sidecar that sits next to a persistent-layout recording.

    The persistent runner writes ``<run_dir>/<role>/terminal-recording.jsonl``
    plus ``<run_dir>/<role>/chapters.json``. The legacy spawn-per-phase
    layout has no sidecar — return ``None`` so callers fall back to
    serving the whole recording slice they were given.
    """
    sidecar_path = recording_path.parent / EXCHANGE_CHAPTERS_NAME
    if not sidecar_path.exists():
        return None
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        return ExchangeChapterSidecar.from_payload(payload)
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.warning(
            "Malformed chapters sidecar at %s: %s — serving whole recording",
            sidecar_path,
            exc,
        )
        return None


def _phase_event_window(
    chapters: list[ExchangeChapter],
    round_index: int,
) -> tuple[int, int | None]:
    """Return ``(start_index, end_index_or_None)`` in the recording for one round.

    The window starts at the prompt chapter for ``round_index`` and ends
    at the prompt chapter for the next round (exclusive). The final
    round has no successor, so ``end_index`` is ``None`` meaning
    "until end of recording" — the UI playhead naturally stops there.
    """
    sorted_prompts = sorted(
        (
            chapter
            for chapter in chapters
            if chapter.section == CHAPTER_SECTION_PROMPT
        ),
        key=lambda chapter: chapter.cycle_index,
    )
    start_index = 0
    end_index: int | None = None
    found_target = False
    for chapter in sorted_prompts:
        if chapter.cycle_index == round_index:
            start_index = chapter.recording_event_index
            found_target = True
        elif found_target and chapter.cycle_index > round_index:
            end_index = chapter.recording_event_index
            break
    return start_index, end_index


def _chapters_payload(
    chapters: list[ExchangeChapter],
) -> list[dict[str, Any]]:
    """Render chapters for the JSON response — one entry per chapter."""
    return [
        {
            "cycle_index": chapter.cycle_index,
            "section": chapter.section,
            "recording_event_index": chapter.recording_event_index,
            "recorded_at": chapter.recorded_at,
            "label": chapter.label,
        }
        for chapter in chapters
    ]


@dataclass(frozen=True)
class _PhaseScope:
    """Resolved phase-scoping inputs for serve_terminal_recording."""

    round_index: int | None
    session_role: str | None


def _resolve_phase_scope(
    round_index: int | None,
    session_role: str | None,
) -> _PhaseScope | JSONResponse:
    """Validate phase-scoping query params and return the resolved tuple.

    Returns a 400 response when the caller supplied one half of the pair
    (round_index without session_role or vice versa). Either both are
    set (phase scope) or both are None (whole-run scope).
    """
    resolved_round_index = _positive_int(round_index)
    resolved_session_role = str(session_role or "").strip().lower() or None
    if resolved_round_index is None and resolved_session_role is None:
        return _PhaseScope(round_index=None, session_role=None)
    if resolved_round_index is None or not resolved_session_role:
        return JSONResponse(
            {
                "error": (
                    "round_index and session_role are required together "
                    "for phase-scoped recordings"
                ),
            },
            status_code=400,
        )
    return _PhaseScope(
        round_index=resolved_round_index,
        session_role=resolved_session_role,
    )


def _slice_events_to_phase_window(
    all_events: list[dict[str, Any]],
    recording_path: Path,
    scope: _PhaseScope,
) -> tuple[
    list[dict[str, Any]], int, ExchangeChapterSidecar | None, int,
]:
    """Slice events to the requested round's window using chapters.json.

    Returns ``(events, total_events, sidecar_or_none, window_start)``.
    The persistent layout writes chapters next to each role's recording;
    the legacy spawn-per-phase layout has its own per-round file and no
    sidecar, in which case we return the unsliced events untouched.
    """
    if scope.round_index is None or scope.session_role is None:
        return all_events, len(all_events), None, 0
    sidecar = _load_phase_chapter_sidecar(recording_path, scope.session_role)
    if sidecar is None or not sidecar.chapters:
        return all_events, len(all_events), sidecar, 0
    window_start, window_end = _phase_event_window(
        sidecar.chapters, scope.round_index,
    )
    slice_end = window_end if window_end is not None else len(all_events)
    sliced = all_events[window_start:slice_end]
    return sliced, len(sliced), sidecar, window_start


def serve_terminal_recording(
    issue_number: int,
    run_dir: str | None,
    offset: int = 0,
    limit: int = 200,
    round_index: int | None = None,
    session_role: str | None = None,
    since_hash: str | None = None,
) -> JSONResponse:
    """Shared implementation for terminal recording endpoints."""
    if not run_dir:
        return JSONResponse(
            {
                "error": "run_dir is required",
                "hint": "Open terminal recordings from a run-scoped timeline action.",
            },
            status_code=400,
        )

    run_identity = RunIdentity(issue_number=issue_number, run_dir=Path(run_dir))
    accessor = ManifestAccessor(run_identity)
    scope = _resolve_phase_scope(round_index, session_role)
    if isinstance(scope, JSONResponse):
        return scope
    try:
        if scope.round_index is not None and scope.session_role is not None:
            artifact = accessor.get_review_exchange_phase_terminal_recording(
                round_index=scope.round_index,
                role=scope.session_role,
                allow_empty=True,
            )
        else:
            artifact = accessor.get_terminal_recording(allow_empty=True)
    except ArtifactNotFoundError as exc:
        return JSONResponse(
            {
                "error": "No terminal recording found",
                "hint": "Session may not have started or raw recording was not enabled",
                "diagnostic": {
                    "run_dir": str(run_identity.run_dir),
                    "detail": str(exc),
                },
            },
            status_code=404,
        )

    recording_path = artifact.path
    try:
        all_events = list(iter_terminal_recording(recording_path))
        all_events, total_events, chapter_sidecar, phase_window_start = (
            _slice_events_to_phase_window(all_events, recording_path, scope)
        )

        # Dispatch render mode by captured format. Codex ``exec --json``
        # writes a JSON event stream to the PTY; feeding that to an xterm
        # emulator renders gibberish (the "Reviewer Session Recording"
        # complaint). Detect the format and return a transcript instead.
        # Claude TUI sessions stay on the emulator path — their colors and
        # prompts only make sense in a real terminal.
        dispatch = _render_mode_for_recording(all_events)

        # Transcript mode receives ``since_hash`` for incremental refresh:
        # when the recording hasn't grown since the caller's previous fetch,
        # we skip retransmitting the whole transcript. This keeps a long
        # codex session with the modal open from costing O(N²) bytes over
        # the wire as the user polls.
        if dispatch.mode == "transcript":
            if since_hash and since_hash == dispatch.transcript_hash:
                return JSONResponse(
                    {
                        "issue_number": issue_number,
                        "recording_path": str(recording_path),
                        "render_mode": dispatch.mode,
                        "transcript_hash": dispatch.transcript_hash,
                        "unchanged": True,
                    }
                )
            return JSONResponse(
                {
                    "issue_number": issue_number,
                    "recording_path": str(recording_path),
                    "render_mode": dispatch.mode,
                    "transcript_lines": dispatch.transcript_lines,
                    "transcript_hash": dispatch.transcript_hash,
                    # Terminal-only fields deliberately omitted in transcript
                    # mode — the emulator view never consumes them, and for
                    # large codex recordings ``events`` would add megabytes
                    # of unused bytes to each response.
                }
            )

        events = all_events[offset:] if offset > 0 else all_events
        truncated = False
        if limit > 0 and len(events) > limit:
            if offset == 0:
                events = events[-limit:]
                truncated = True
            else:
                events = events[:limit]

        chapters_payload = (
            _chapters_payload(chapter_sidecar.chapters)
            if chapter_sidecar is not None
            else None
        )
        return JSONResponse(
            {
                "issue_number": issue_number,
                "recording_path": str(recording_path),
                "content_type": "application/x-ndjson",
                "total_events": total_events,
                "initial_geometry": _terminal_recording_initial_geometry(recording_path),
                "offset": offset,
                "truncated": truncated,
                # Phase-scoped persistent layout: ``chapters`` is the full
                # role outline so the UI can render a navigable sidebar,
                # and ``recording_event_index`` is the absolute position
                # in the role's recording where this slice begins (the
                # prompt boundary for ``round_index``). Both fields are
                # ``null`` for whole-run recordings and legacy spawn-
                # per-phase artifacts that have no sidecar.
                "chapters": chapters_payload,
                "recording_event_index": (
                    phase_window_start if chapter_sidecar is not None else None
                ),
                "events": events,
                "render_mode": dispatch.mode,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": f"Failed to read terminal recording: {exc}"}, status_code=500)


@web_session_router.get("/api/session/terminal-recording/{issue_number}")
async def get_terminal_recording(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    offset: int = 0,
    limit: int = 200,
    run_dir: str | None = None,
    round_index: int | None = None,
    session_role: str | None = None,
    since_hash: str | None = None,
) -> JSONResponse:
    """Return the canonical raw terminal recording for a run."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    return serve_terminal_recording(
        issue_number,
        run_dir,
        offset,
        limit,
        round_index,
        session_role,
        since_hash,
    )


def build_ui_log_stream_observation(run_dir: Path, *, resolved_log_path: Path | None) -> dict[str, Any]:
    """Build lightweight file-stat telemetry for run-scoped UI log streaming."""
    terminal_recording = run_dir / "terminal-recording.jsonl"
    provider_stdout = run_dir / "provider-runner" / "stdout.log"
    provider_stderr = run_dir / "provider-runner" / "stderr.log"
    claude_log = run_dir / "claude-session.jsonl"
    return {
        "run_dir": str(run_dir),
        "resolved_log_path": str(resolved_log_path) if resolved_log_path else None,
        "terminal_recording": _stream_file_observation(terminal_recording),
        "provider_stdout": _stream_file_observation(provider_stdout),
        "provider_stderr": _stream_file_observation(provider_stderr),
        "claude_log": _stream_file_observation(claude_log),
    }


RenderMode = Literal["terminal", "transcript"]


@dataclass(frozen=True)
class RecordingRenderDispatch:
    """Typed result of format-detecting a terminal recording.

    ``mode`` is a :obj:`Literal` so both Python call sites and the JS
    frontend can validate against a small fixed set. ``transcript_lines``
    and ``transcript_hash`` are populated only when ``mode == "transcript"``;
    in terminal mode they are ``None``. The hash is a stable fingerprint
    of the transcript content so the frontend can short-circuit redundant
    refreshes over the wire.
    """

    mode: RenderMode
    transcript_lines: list[str] | None
    transcript_hash: str | None


def _render_mode_for_recording(
    events: list[dict[str, Any]],
) -> RecordingRenderDispatch:
    """Choose how the UI should render this terminal recording.

    Terminal mode covers the existing xterm-emulator path (Claude TUI,
    raw PTY). Transcript mode handles Codex JSON streams where the
    emulator would render envelope JSON as-is.

    Detection decodes the first *complete* output line and runs it
    through the codex extractor — line-scoped rather than char-count-
    scoped so a single large ``agent_message`` (reviewer prose can
    easily exceed several KB) can't straddle the sniff boundary and
    cause a format mis-classification.
    """
    first_line = _first_complete_decoded_line(events)
    if first_line is None:
        return RecordingRenderDispatch(
            mode="terminal", transcript_lines=None, transcript_hash=None
        )
    if extract_codex_transcript([first_line]) is None:
        return RecordingRenderDispatch(
            mode="terminal", transcript_lines=None, transcript_hash=None
        )
    # Commit to transcript mode: run the FULL decoded content through the
    # prettifier so the UI gets an agent-message + command-execution
    # transcript instead of raw JSON envelopes.
    full_decoded = _decode_all_output(events)
    transcript = prettify_session_log(full_decoded.splitlines())
    digest = hashlib.sha256("\n".join(transcript).encode("utf-8")).hexdigest()
    return RecordingRenderDispatch(
        mode="transcript", transcript_lines=transcript, transcript_hash=digest
    )


def _first_complete_decoded_line(events: list[dict[str, Any]]) -> str | None:
    """Decode output chunks until one complete ``\\n``-terminated line emerges.

    Codex emits one JSON event per line, so the first complete line is
    sufficient to classify the format — regardless of how large that line
    is. Returns ``None`` when no output or no complete line is present.
    """
    buffer = ""
    saw_output = False
    for event in events:
        if event.get("event_type") != "output":
            continue
        data_b64 = event.get("data_b64")
        if not isinstance(data_b64, str) or not data_b64:
            continue
        try:
            chunk = base64.b64decode(data_b64).decode("utf-8", errors="ignore")
        except Exception:
            continue
        saw_output = True
        buffer += chunk
        newline = buffer.find("\n")
        if newline >= 0:
            return buffer[:newline]
    # No newline ever arrived — treat the whole buffer as one line only if
    # we actually saw output; otherwise the caller should fall back to
    # terminal mode (nothing to sniff).
    if saw_output and buffer:
        return buffer
    return None


def _decode_all_output(events: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for event in events:
        if event.get("event_type") != "output":
            continue
        data_b64 = event.get("data_b64")
        if not isinstance(data_b64, str) or not data_b64:
            continue
        try:
            chunks.append(base64.b64decode(data_b64).decode("utf-8", errors="ignore"))
        except Exception:
            continue
    return "".join(chunks)


def _terminal_recording_initial_geometry(path: Path) -> dict[str, int] | None:
    geometry = first_terminal_geometry(path)
    if geometry is None:
        return None
    rows, cols = geometry
    return {"rows": rows, "cols": cols}


def preview_lines_from_terminal_recording(path: Path) -> list[str]:
    """Decode raw output events into a best-effort text preview for legacy viewers."""
    decoded_chunks: list[str] = []
    try:
        for event in iter_terminal_recording(path):
            if event.get("event_type") != "output":
                continue
            data_b64 = event.get("data_b64")
            if not isinstance(data_b64, str) or not data_b64:
                continue
            try:
                decoded_chunks.append(base64.b64decode(data_b64).decode("utf-8", errors="ignore"))
            except Exception:
                continue
    except Exception:
        logger.warning("terminal recording preview decode failed: %s", path, exc_info=True)
        return path.read_text(errors="ignore").splitlines()
    if decoded_chunks:
        return "".join(decoded_chunks).splitlines()

    for raw_line in path.read_text(errors="ignore").splitlines():
        if raw_line.strip():
            decoded_chunks.append(raw_line)
    return "".join(decoded_chunks).splitlines()


def preview_lines_from_claude_jsonl(path: Path) -> list[str]:
    """Render a concise preview from a Claude JSONL transcript."""
    preview_lines: list[str] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                if not raw_line.strip():
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                preview_lines.extend(claude_jsonl_entry_preview_lines(entry))
    except OSError:
        return []
    return [line for line in preview_lines if line.strip()]


def _stream_file_observation(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "is_symlink": False,
        "symlink_target": None,
        "bytes": None,
        "mtime_epoch": None,
    }
    try:
        data["is_symlink"] = path.is_symlink()
        if data["is_symlink"]:
            try:
                data["symlink_target"] = str(path.resolve())
            except OSError:
                data["symlink_target"] = None
        if path.exists():
            stat = path.stat()
            data["exists"] = True
            data["bytes"] = int(stat.st_size)
            data["mtime_epoch"] = float(stat.st_mtime)
    except OSError:
        return data
    return data


def _manifest_response(run_dir: Path, session_name: str | None) -> JSONResponse:
    """Load RunManifest + analysis from run_dir and return as JSON."""
    from ..control.session_analyzer import load_analysis
    from ..domain.run_manifest import RunManifest

    try:
        manifest = RunManifest.load(run_dir)
    except FileNotFoundError:
        return JSONResponse(
            {
                "run_dir": str(run_dir),
                "session_name": session_name,
                "manifest": None,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": f"Failed to read manifest: {exc}"}, status_code=500)

    result: dict[str, Any] = {
        "run_dir": str(run_dir),
        "session_name": session_name,
        "manifest": manifest.to_dict(),
    }
    session_identity_path = run_dir / "session-identity.json"
    if session_identity_path.exists():
        try:
            result["session_identity"] = json.loads(session_identity_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.debug("Failed to read session identity: %s", session_identity_path, exc_info=True)

    analysis = load_analysis(run_dir)
    if analysis:
        result["analysis"] = {
            "headline": analysis.headline,
            "detail": analysis.detail,
            "suggestions": list(analysis.suggestions),
        }

    validation_failure = load_validation_failure_summary(run_dir)
    if validation_failure is not None:
        result["validation_failure"] = validation_failure.to_dict()

    return JSONResponse(result)


def session_manifest_response(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    run_dir: str | None = None,
) -> JSONResponse:  # noqa: C901, PLR0912
    """Build the session manifest response for an issue."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    requested_run_dir = run_dir
    context = resolve_issue_session_context(orchestrator, issue_number)
    worktree_path = context.worktree_path
    session_name = context.session_name
    resolved_run_dir = context.run_dir

    if requested_run_dir:
        candidate = Path(requested_run_dir)
        if candidate.exists():
            resolved_run_dir = candidate

    if resolved_run_dir:
        from ..execution.session_output_adapter import FileSystemSessionOutput

        session_output = FileSystemSessionOutput()
        if not session_name:
            session_name = session_output.session_name_from_path(str(resolved_run_dir))
        session_output.attach_claude_log(resolved_run_dir)
        return _manifest_response(resolved_run_dir, session_name)

    if not worktree_path:
        return JSONResponse(
            {
                "error": f"No worktree path found for issue #{issue_number}",
                "hint": "Session may have been cleaned up or never started",
            },
            status_code=404,
        )

    from ..execution.session_output_adapter import FileSystemSessionOutput

    session_output = FileSystemSessionOutput()
    resolved_run_dir = session_output.find_run_dir_for_issue(worktree_path, issue_number)
    if not resolved_run_dir:
        return JSONResponse(
            {
                "error": "No session run found",
                "hint": "Session may not have started or output was removed",
            },
            status_code=404,
        )
    session_output.attach_claude_log(resolved_run_dir)
    return _manifest_response(resolved_run_dir, session_name)


@web_session_router.get("/api/session/manifest/{issue_number}")
async def get_session_manifest(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    run_dir: str | None = None,
) -> JSONResponse:
    """Get the session manifest for an issue."""
    return session_manifest_response(issue_number, orchestrator, run_dir=run_dir)


@web_session_router.get("/api/session/worktree/{issue_number}")
async def get_session_worktree(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:  # noqa: C901
    """Get the worktree path for a session (active or history)."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    context = resolve_issue_session_context(orchestrator, issue_number)
    if not context.worktree_path:
        return JSONResponse(
            {"error": f"No worktree path found for issue #{issue_number}"},
            status_code=404,
        )

    return JSONResponse(
        {
            "issue_number": issue_number,
            "worktree_path": str(context.worktree_path),
            "session_name": context.session_name,
        }
    )


def session_phases_response(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:  # noqa: C901
    """Build the linear phase history response for an issue."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..execution.session_output_adapter import FileSystemSessionOutput

    context = resolve_issue_session_context(orchestrator, issue_number)
    if not context.worktree_path:
        return JSONResponse(
            {
                "phases": [],
                "current_phase": None,
                "error": "No worktree found for issue",
            }
        )

    session_output = FileSystemSessionOutput()
    runs = session_output.list_runs(context.worktree_path)

    phases = []
    current_phase = None
    for run in runs:
        phase_name = run.get("session_name", "unknown")
        status = run.get("status", "unknown")
        phase = {
            "name": phase_name,
            "display_name": _format_phase_name(phase_name),
            "status": status,
            "status_icon": _phase_status_icon(status),
            "started_at": run.get("started_at"),
            "ended_at": run.get("ended_at"),
            "agent_label": run.get("agent_label"),
            "run_dir": run.get("run_dir"),
            "outcome": run.get("outcome"),
            "validation_passed": run.get("validation_passed"),
        }
        phases.append(phase)
        if status == "in_progress":
            current_phase = phase_name

    return JSONResponse(
        {
            "phases": phases,
            "current_phase": current_phase,
            "issue_number": issue_number,
        }
    )


@web_session_router.get("/api/session/phases/{issue_number}")
async def get_session_phases(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Get the linear phase history for an issue."""
    return session_phases_response(issue_number, orchestrator)


@web_session_router.get("/api/session/orchestrator-log/{issue_number}")
async def get_filtered_orchestrator_log(  # noqa: C901, PLR0912
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    run_dir: str | None = None,
) -> JSONResponse:
    """Generate and return a filtered orchestrator log for an issue."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..execution.session_output_adapter import FileSystemSessionOutput
    from ..infra.logging_config import get_repo_log_path

    session_output = FileSystemSessionOutput()
    context = resolve_issue_session_context(orchestrator, issue_number)
    worktree_path = context.worktree_path
    session_name = context.session_name
    resolved_run_dir = context.run_dir
    if run_dir:
        candidate = Path(run_dir)
        if candidate.exists():
            resolved_run_dir = candidate
            inferred_worktree = worktree_path_from_run_dir(candidate)
            if inferred_worktree:
                worktree_path = inferred_worktree
            session_name = session_output.session_name_from_path(str(candidate))

    if not worktree_path:
        return JSONResponse({"error": f"No worktree found for issue #{issue_number}"}, status_code=404)

    if not session_name:
        session_name = session_output.session_name_from_path(str(resolved_run_dir)) if resolved_run_dir else None
    if not session_name:
        return JSONResponse(
            {
                "error": "Could not determine session name for issue log filtering",
                "worktree_path": str(worktree_path),
            },
            status_code=500,
        )

    log_path = get_repo_log_path(orchestrator.config.repo_root)
    if not log_path.exists():
        return JSONResponse(
            {
                "error": "Orchestrator log file not found",
                "full_log_path": str(log_path),
            },
            status_code=404,
        )

    if not resolved_run_dir:
        resolved_run_dir = session_output.find_run_dir_for_issue(worktree_path, issue_number)
    if not resolved_run_dir:
        return JSONResponse(
            {
                "error": "Could not find session run directory",
                "worktree_path": str(worktree_path),
            },
            status_code=500,
        )
    tail_path = session_output.write_orchestrator_tail(
        resolved_run_dir,
        log_path,
        issue_number,
        session_name,
        max_lines=500,
    )
    if not tail_path:
        return JSONResponse(
            {
                "error": (
                    f"No issue-scoped orchestrator log entries found for issue #{issue_number}"
                ),
            },
            status_code=500,
        )

    return JSONResponse(
        {
            "filtered_log_path": str(tail_path),
            "full_log_path": str(log_path),
            "issue_number": issue_number,
        }
    )


@web_session_router.get("/api/session/claude-log/{issue_number}")
async def get_claude_log_content(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    limit: int = 200,
    run_dir: str | None = None,
) -> JSONResponse:  # noqa: C901, PLR0912
    """Fetch and parse Claude session log for viewing in the dashboard."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    if not run_dir:
        return JSONResponse(
            {
                "error": "run_dir is required",
                "hint": "Open Claude log from a run-scoped timeline action.",
            },
            status_code=400,
        )

    run_identity = RunIdentity(issue_number=issue_number, run_dir=Path(run_dir))
    accessor = ManifestAccessor(run_identity)
    try:
        artifact = accessor.get_claude_log()
    except ArtifactNotFoundError as exc:
        return JSONResponse(
            {
                "error": "Claude log not found",
                "run_dir": str(run_identity.run_dir),
                "detail": str(exc),
            },
            status_code=404,
        )
    log_path = artifact.path

    entries = []
    try:
        with open(log_path, "r") as handle:
            for index, line in enumerate(handle):
                if index >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    entries.append({"_raw": line, "_parse_error": True})
    except Exception as exc:
        return JSONResponse({"error": f"Failed to read log: {exc}"}, status_code=500)

    return JSONResponse(
        {
            "log_path": str(log_path),
            "issue_number": issue_number,
            "run_dir": str(run_identity.run_dir),
            "entry_count": len(entries),
            "entries": entries,
        }
    )


@web_session_router.get("/api/session/review-transcript/{issue_number}")
async def get_review_transcript_content(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    run_dir: str | None = None,
    round_index: int | None = None,
    transcript_role: str | None = None,
) -> JSONResponse:
    """Return the dedicated review-exchange transcript for a run."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    if not run_dir:
        return JSONResponse(
            {
                "error": "run_dir is required",
                "hint": "Open review transcripts from a run-scoped timeline action.",
            },
            status_code=400,
        )

    run_identity = RunIdentity(issue_number=issue_number, run_dir=Path(run_dir))
    accessor = ManifestAccessor(run_identity)
    try:
        artifact = accessor.get_review_exchange_transcript(allow_empty=True)
    except ArtifactNotFoundError as exc:
        return JSONResponse(
            {
                "error": "Review transcript not found",
                "run_dir": str(run_identity.run_dir),
                "detail": str(exc),
            },
            status_code=404,
        )

    transcript_path = artifact.path
    try:
        content = transcript_path.read_text(encoding="utf-8")
    except Exception as exc:
        return JSONResponse({"error": f"Failed to read transcript: {exc}"}, status_code=500)

    parsed_entries = parse_review_exchange_transcript(content)
    filtered_entries = filter_review_exchange_transcript(
        parsed_entries,
        round_index=_positive_int(round_index),
        role=str(transcript_role or "").strip() or None,
    )
    if filtered_entries or round_index is not None or transcript_role:
        content = render_review_exchange_transcript(filtered_entries)

    scope_label = "Full review exchange"
    if _positive_int(round_index) and transcript_role:
        scope_label = f"Round {round_index} {str(transcript_role).strip()}"
    elif _positive_int(round_index):
        scope_label = f"Round {round_index}"
    elif transcript_role:
        scope_label = f"{str(transcript_role).strip()} entries"

    return JSONResponse(
        {
            "issue_number": issue_number,
            "run_dir": str(run_identity.run_dir),
            "transcript_path": str(transcript_path),
            "content": content,
            "scope_label": scope_label,
            "entry_count": (
                len(filtered_entries)
                if (round_index is not None or transcript_role)
                else len(parsed_entries)
            ),
        }
    )


def _latest_review_exchange_prompt(run_dir: Path) -> Path | None:
    """Return newest review-exchange prompt artifact under run_dir if present."""
    exchange_root = run_dir / "review-exchange"
    if not exchange_root.exists():
        return None
    candidates = sorted(
        list(exchange_root.glob("round-*/coder-prompt.txt"))
        + list(exchange_root.glob("round-*/reviewer-prompt.txt")),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    return candidates[0] if candidates else None


@web_session_router.get("/api/session/prompt/{issue_number}")
async def get_session_prompt_content(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    run_dir: str | None = None,
) -> JSONResponse:
    """Return run-scoped prompt content for a session."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    if not run_dir:
        return JSONResponse(
            {
                "error": "run_dir is required",
                "hint": "Open prompt from a run-scoped timeline action.",
            },
            status_code=400,
        )

    run_path = Path(run_dir)
    if not run_path.exists():
        return JSONResponse({"error": f"run_dir does not exist: {run_dir}"}, status_code=404)

    from ..domain.run_manifest import RunManifest
    from ..execution.session_output_adapter import SESSION_PROMPT_NAME

    manifest_prompt_path: Path | None = None
    try:
        manifest = RunManifest.load(run_path)
        session_prompt_path = manifest.to_dict().get("session_prompt_path")
        if isinstance(session_prompt_path, str) and session_prompt_path:
            manifest_prompt_path = Path(session_prompt_path)
    except Exception:
        manifest_prompt_path = None

    candidates = [
        manifest_prompt_path,
        run_path / SESSION_PROMPT_NAME,
        run_path / "retry-prompt.md",
        _latest_review_exchange_prompt(run_path),
    ]
    prompt_path = next(
        (path for path in candidates if path and path.exists() and path.stat().st_size > 0),
        None,
    )
    if not prompt_path:
        return JSONResponse(
            {"error": "No run-scoped prompt artifact found for this session"},
            status_code=404,
        )

    try:
        content = prompt_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return JSONResponse({"error": f"Failed to read prompt: {exc}"}, status_code=500)

    return JSONResponse(
        {
            "issue_number": issue_number,
            "run_dir": str(run_path),
            "prompt_path": str(prompt_path),
            "content": content,
        }
    )
