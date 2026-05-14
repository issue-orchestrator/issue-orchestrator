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

import enum
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.artifact_contracts import (
    AgentProvider,
    AgentRole,
    AgentTurnArtifactScope,
    ArtifactContractViolation,
    ArtifactRef,
    ChapterSidecarArtifact,
    CoderTurnCompleted,
    CoderTurnStarted,
    ExchangeRunId,
    ExistingFile,
    ExistingNonEmptyFile,
    IssueNumber,
    PositiveAttemptIndex,
    PositiveRoundIndex,
    PromptArtifact,
    ReviewResponseArtifact,
    ReviewerTurnCompleted,
    ReviewerTurnStarted,
    TerminalRecordingArtifact,
)
from ..domain.review_exchange_manifest import (
    ReviewExchangeManifestHeader,
    ReviewExchangeRecordingPaths,
)
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
from ..domain.review_exchange_resume import is_no_completion_reason
from ..domain.review_exchange_turn import (
    ReviewExchangePromptFiles,
    ReviewExchangeTurnPacket,
    ReviewExchangeTurnResult,
    Role,
    TurnResultKind,
)
from ..events import EventContext, EventName
from ..infra.env import ENV_PREFIX
from ..infra.logging_config import get_repo_log_path
from ..infra.repo_identity import get_repo_head_sha
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


class _PairRecordingContractErrorKind(str, enum.Enum):
    NO_WRITER = "no_writer"
    PATH_MISMATCH = "path_mismatch"
    MISSING_FILE = "missing_file"
    NOT_A_FILE = "not_a_file"


@dataclass(frozen=True)
class _PairRecordingContractError:
    role: Role
    kind: _PairRecordingContractErrorKind
    detail: str

    def __str__(self) -> str:
        return f"{self.role.value} {self.kind.value}: {self.detail}"


class _PairRecordingContractViolation(RuntimeError):
    def __init__(
        self,
        *,
        issue_number: int,
        errors: tuple[_PairRecordingContractError, ...],
    ) -> None:
        self.issue_number = issue_number
        self.errors = errors
        joined = "; ".join(str(error) for error in errors)
        super().__init__(
            f"persistent pair recording contract invalid after respawn "
            f"issue={issue_number} errors={joined}"
        )


def review_exchange_supervisor_timeout_seconds(
    *,
    coder_timeout_seconds: float,
    reviewer_timeout_seconds: float,
    max_rounds: int,
    grace_seconds: float = 300.0,
) -> float:
    """Return the outer wall-clock deadline for one persistent exchange.

    The persistent runner owns coder protocol retries. The background
    supervisor deadline must include that retry budget so it only catches a
    wedged runner, not a legitimate exchange still inside its per-role
    deadlines.
    """
    if coder_timeout_seconds <= 0:
        raise ValueError("coder_timeout_seconds must be positive")
    if reviewer_timeout_seconds <= 0:
        raise ValueError("reviewer_timeout_seconds must be positive")
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be non-negative")
    rounds = max(1, int(max_rounds))
    coder_attempts_per_round = 1 + _CODER_PROTOCOL_RETRY_LIMIT
    per_round = reviewer_timeout_seconds + (
        coder_timeout_seconds * coder_attempts_per_round
    )
    return float(per_round * rounds + grace_seconds)


_BOOTSTRAP_PROMPT_TEMPLATE = (
    "You are the {role} in a coder↔reviewer review exchange for issue "
    "#{issue_number}: {issue_title}.\n\n"
    "Wait for the orchestrator to send your role-specific instructions via "
    "stdin. The orchestrator may send the full prompt directly, or it may "
    "send a short notice pointing at a prompt file in this worktree. For "
    "each turn, read the full instructions, follow them, and write exactly "
    "one line of JSON to the file at $ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE. "
    "Then wait for the next prompt. Do not exit on your own; the orchestrator "
    "will terminate you when the exchange is done.\n"
)


@dataclass
class _RoleSliceMirror:
    """Translate pair-recording event indices into per-session slice indices.

    The slice file at ``<run_dir>/<role>/terminal-recording.jsonl`` is
    written **continuously** by the role's
    ``MirroredTerminalRecordingWriter`` — registered at exchange start
    via ``add_mirror_recording`` and removed at exchange end. The
    timeline viewer therefore sees agent output update in near real time
    rather than waiting for a chapter boundary to flush.

    What this dataclass owns is the **offset translation** between the
    pair recording (long-lived, accumulates across every exchange the
    pair handles) and the slice (per-exchange, freshly attached). Its
    ``slice_base`` is the pair recording's event count *at exchange
    start* — the first event the slice will mirror. Chapter sidecars
    store ``pair_event_idx - slice_base`` so the viewer can scrub the
    manifest-pointed slice directly. Without that translation, a cached
    pair on exchange 2 would record chapter offsets in the hundreds
    while the slice file holds dozens of events and the web replay
    route's ``all_events[offset:]`` would return an empty window.
    """

    pair_recording: Path
    session_slice: Path
    slice_base: int

    def pair_to_slice_offset(self, pair_event_idx: int) -> int:
        """Translate a pair-recording event index into a slice-local index.

        The slice file is written as a strict subset of the pair
        recording starting at ``slice_base``; the slice's event N
        corresponds to pair event ``slice_base + N``. Chapter sidecars
        store these slice-local offsets so the viewer can scrub the
        manifest-pointed slice directly.

        Raises ``ValueError`` when ``pair_event_idx < slice_base``.
        Chapter recording happens during the exchange, after slice
        attach; an index from before exchange start is a wrong-source
        bug (caller fed an index from a different recording) and
        masking it with a clamp would silently return wrong content.
        """
        if pair_event_idx < self.slice_base:
            raise ValueError(
                f"pair_event_idx={pair_event_idx} is below "
                f"slice_base={self.slice_base}; chapter offsets must "
                "be sampled after the slice mirror is attached at "
                "exchange start. A negative slice index would index "
                "past the start of the slice and silently return "
                "content from prior exchanges.",
            )
        return pair_event_idx - self.slice_base


def _attach_slice_mirror(
    session: PersistentSession,
    slice_path: Path,
) -> None:
    """Register a per-session slice with the role's PTY writer.

    Fails loudly. The slice mirror is load-bearing for the per-session
    timeline contract — without it the viewer reads an empty slice
    file from the manifest while the agent's output continues to flow
    only into the pair recording, recreating the exact "I can't see
    what the reviewer is doing" symptom this PR is supposed to fix.
    Failures here propagate up to ``run_persistent_session_exchange``'s
    top-level handler, which emits ``REVIEW_EXCHANGE_FAILED`` and
    re-raises so the orchestrator's loop bound (PR #6267) can govern
    retries / escalation rather than the silent empty-timeline mode.

    ``log_writer is None`` is a production invariant violation: every
    role session opened by ``open_persistent_session`` carries a real
    ``MirroredTerminalRecordingWriter``. Test fixtures that construct
    sessions directly must wire a writer too — not doing so would mean
    the test was getting a free pass on the live-mirror invariant.
    """
    writer = session.log_writer
    if writer is None:
        raise RuntimeError(
            f"PersistentSession has no log_writer; cannot attach "
            f"per-session slice mirror at {slice_path}. Production "
            "sessions always carry a writer; this indicates either a "
            "regression in open_persistent_session or a test fixture "
            "that bypassed the writer wiring.",
        )
    # ``seed_resize=False`` keeps the slice indexing aligned with the
    # offset translator: the first slice event corresponds to the
    # first pair event written *after* exchange start, with no
    # synthetic leading event to throw off ``pair_to_slice_offset``.
    writer.add_mirror_recording(slice_path, seed_resize=False)


def _detach_slice_mirror(
    session: PersistentSession,
    slice_path: Path,
) -> None:
    """Stop mirroring writes to the per-session slice path.

    Called from a ``finally`` block, so any exception here would
    obscure the exception that put us in the finally — log and
    continue rather than mask the real failure. The flip side of
    ``_attach_slice_mirror``'s fail-fast: if attach succeeded, detach
    almost never fails (the writer's path map is in-process state),
    and if detach somehow fails the worst case is the next exchange
    seeing tail bytes from this exchange in its slice — caught by
    ``test_slice_detaches_at_exchange_end_no_leak_to_next_exchange``.
    """
    writer = session.log_writer
    if writer is None:
        # The attach helper would have raised before we got here, so
        # reaching this branch means someone called detach without
        # ever calling attach. Tolerate so a partial-construction
        # cleanup path stays simple.
        return
    try:
        writer.remove_mirror_recording(slice_path)
    except (OSError, ValueError):
        logger.exception(
            "Failed to detach per-session slice mirror at %s during "
            "exchange teardown; subsequent writes from this writer "
            "may continue to target the slice file. Logging and "
            "continuing — raising here would mask the original "
            "exception that triggered the finally block.",
            slice_path,
        )


def _prepare_session_slice(slice_path: Path) -> None:
    """Create the per-session slice directory and seed an empty file.

    Pre-creating an empty file keeps the timeline viewer's recording
    lookup (``ManifestAccessor.get_review_exchange_recording``) from
    404'ing while a hung exchange is mid-round and no slice events
    have been mirrored yet. ``allow_empty=False`` callers still see
    "empty" as a recoverable condition rather than "missing".
    """
    slice_path.parent.mkdir(parents=True, exist_ok=True)
    slice_path.touch(exist_ok=True)


def _pair_recording_contract_errors(
    pair: PersistentExchangePair,
) -> tuple[_PairRecordingContractError, ...]:
    """Return recording-path contract violations for a cached pair.

    A live persistent pair writes to file handles opened when the pair was
    spawned. If a reset or cleanup removes the pair-scoped recording path while
    the process survives, touching the path would create a different file that
    the existing writer will never use. The only correct recovery is to release
    that cached pair and spawn a fresh one.
    """
    errors: list[_PairRecordingContractError] = []
    for role, session, recording_path in (
        (Role.CODER, pair.coder_session, pair.coder_recording_path),
        (Role.REVIEWER, pair.reviewer_session, pair.reviewer_recording_path),
    ):
        writer = session.log_writer
        if writer is None:
            errors.append(
                _PairRecordingContractError(
                    role=role,
                    kind=_PairRecordingContractErrorKind.NO_WRITER,
                    detail="session has no terminal recording writer",
                )
            )
        elif Path(writer.recording_path) != recording_path:
            errors.append(
                _PairRecordingContractError(
                    role=role,
                    kind=_PairRecordingContractErrorKind.PATH_MISMATCH,
                    detail=(
                        f"writer path {writer.recording_path} does not match "
                        f"pair path {recording_path}"
                    ),
                )
            )
        if not recording_path.exists():
            errors.append(
                _PairRecordingContractError(
                    role=role,
                    kind=_PairRecordingContractErrorKind.MISSING_FILE,
                    detail=f"recording path missing: {recording_path}",
                )
            )
        elif not recording_path.is_file():
            errors.append(
                _PairRecordingContractError(
                    role=role,
                    kind=_PairRecordingContractErrorKind.NOT_A_FILE,
                    detail=f"recording path is not a file: {recording_path}",
                )
            )
    return tuple(errors)


def _acquire_pair_with_recording_contract(
    *,
    pair_registry: InMemoryPersistentExchangePairRegistry,
    issue_number: int,
    spawn: Callable[[], PersistentExchangePair],
) -> PersistentExchangePair:
    """Acquire a pair, respawning once when cached recording paths vanished."""
    pair = pair_registry.acquire(issue_key=issue_number, spawn=spawn)
    recording_contract_errors = _pair_recording_contract_errors(pair)
    if not recording_contract_errors:
        return pair
    logger.warning(
        "[REVIEW_EXCHANGE] persistent pair has unusable recording "
        "paths; releasing and respawning issue=%s errors=%s",
        issue_number,
        "; ".join(str(error) for error in recording_contract_errors),
    )
    pair_registry.release(
        issue_number,
        reason="recording-contract-missing-on-acquire",
    )
    pair = pair_registry.acquire(issue_key=issue_number, spawn=spawn)
    respawn_errors = _pair_recording_contract_errors(pair)
    if respawn_errors:
        pair_registry.release(
            issue_number,
            reason="recording-contract-invalid-after-respawn",
        )
        raise _PairRecordingContractViolation(
            issue_number=issue_number,
            errors=respawn_errors,
        )
    return pair


def _emit_review_exchange_failed(
    *,
    emit: Callable[[EventName, dict[str, Any]], None],
    issue_number: int,
    session_name: str,
    exc: Exception,
) -> None:
    emit(EventName.REVIEW_EXCHANGE_FAILED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": 0,
        "error": str(exc),
        "exception_type": type(exc).__name__,
    })


def _acquire_pair_or_emit_failure(
    *,
    pair_registry: InMemoryPersistentExchangePairRegistry,
    issue_number: int,
    session_name: str,
    spawn: Callable[[], PersistentExchangePair],
    emit: Callable[[EventName, dict[str, Any]], None],
) -> PersistentExchangePair:
    try:
        return _acquire_pair_with_recording_contract(
            pair_registry=pair_registry,
            issue_number=issue_number,
            spawn=spawn,
        )
    except Exception as exc:
        _emit_review_exchange_failed(
            emit=emit,
            issue_number=issue_number,
            session_name=session_name,
            exc=exc,
        )
        raise


def _release_pair_after_no_completion(
    *,
    pair_registry: InMemoryPersistentExchangePairRegistry,
    pair: PersistentExchangePair,
    issue_number: int,
    session_name: str,
    outcome: ReviewExchangeOutcome,
) -> bool:
    """Release a wedged persistent pair after a no-completion exchange.

    ``*_no_completion`` means one role timed out before writing the required
    response artifact. The resume policy retries those summaries by launching a
    fresh exchange; with persistent pairs, "fresh" must include the underlying
    PTYs. Otherwise an alive-but-not-reading agent keeps getting reused and the
    retry loop recreates the same timeout.
    """
    if outcome.status != "error" or not is_no_completion_reason(outcome.reason):
        return False
    logger.warning(
        "[REVIEW_EXCHANGE] no-completion result; releasing persistent pair "
        "before retry issue=%s session_name=%s reason=%s coder_pid=%d "
        "reviewer_pid=%d coder_response=%s reviewer_response=%s "
        "reviewer_worktree=%s",
        issue_number,
        session_name,
        outcome.reason,
        pair.coder_session.proc.pid,
        pair.reviewer_session.proc.pid,
        pair.coder_response_path,
        pair.reviewer_response_path,
        pair.reviewer_worktree_path,
    )
    pair_registry.release(
        issue_number,
        reason=f"review-exchange-{outcome.reason}",
    )
    return True


def _release_pair_after_exchange_exception(
    *,
    pair_registry: InMemoryPersistentExchangePairRegistry,
    pair: PersistentExchangePair,
    issue_number: int,
    session_name: str,
    exc: Exception,
) -> None:
    logger.warning(
        "[REVIEW_EXCHANGE] exchange raised; releasing persistent pair "
        "issue=%s session_name=%s exception_type=%s coder_pid=%d "
        "reviewer_pid=%d coder_response=%s reviewer_response=%s "
        "reviewer_worktree=%s",
        issue_number,
        session_name,
        type(exc).__name__,
        pair.coder_session.proc.pid,
        pair.reviewer_session.proc.pid,
        pair.coder_response_path,
        pair.reviewer_response_path,
        pair.reviewer_worktree_path,
    )
    pair_registry.release(issue_number, reason="review-exchange-exception")


def run_persistent_session_exchange(  # noqa: PLR0913
    *,
    session_output: SessionOutput,
    pair_registry: InMemoryPersistentExchangePairRegistry,
    persistent_pair_root: Path,
    coder_worktree_path: Path,
    reviewer_worktree_factory: Callable[[], Path],
    coder_branch: str | None = None,
    issue_number: int,
    issue_title: str,
    coder_label: str,
    reviewer_label: str,
    coder_agent: AgentConfig,
    reviewer_agent: AgentConfig,
    max_rounds: int,
    max_no_progress: int,
    require_validation: bool,
    parent_session_name: str | None = None,
    initial_validation_record_path: Path | None = None,
    web_port: int | None = None,
    events: EventSink | None = None,
    event_context: EventContext | None = None,
    on_started: Callable[[Path], None] | None = None,
    before_reviewer_round: Callable[[int], None] | None = None,
) -> ReviewExchangeOutcome:
    """Run the coder↔reviewer exchange against a registry-owned persistent pair.

    Acquires a pair from ``pair_registry``. On cache miss the spawn
    closure invokes ``reviewer_worktree_factory`` to create the
    reviewer worktree, opens both PTY-attached sessions with their
    env pointing at *pair-scoped* response/recording files (under the
    caller-supplied, worktree-scoped ``persistent_pair_root``), and
    caches the pair. On cache hit the existing pair is reused — same
    coder PID, same reviewer PID, same recording continuing where it
    left off.

    The release at issue-lifetime boundaries (PR merge, reset-retry,
    escalation, orchestrator shutdown) is the *caller's*
    responsibility — this function does not release per exchange.
    Holding the pair past one exchange is precisely the user-visible
    benefit of the registry; ADR 0026 explains the lifecycle map.

    ``reviewer_worktree_factory`` is invoked at most once per pair —
    on the first cache miss for the issue. Subsequent exchanges reuse
    the worktree stored on the cached pair and rely on the
    ``before_reviewer_round`` callback to fast-forward it to the
    coder's latest tip.
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

    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    _write_review_exchange_manifest_header(
        session_output,
        run_dir,
        ReviewExchangeManifestHeader(
            exchange_dir=exchange_dir,
            parent_session_name=parent_session_name,
        ),
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

    # Pair-scoped paths: the same physical files survive across every
    # exchange the pair handles. The agent's env points at the pair-
    # scoped completion / validation paths once at spawn; if the pair
    # is reused for exchange 2, exchange 2's rounds read/write the
    # same files.
    #
    # The per-round response file is the one exception: the agent
    # writes it itself, and codex's per-shell-tool seatbelt sandbox
    # constrains writes to the role's worktree (its cwd). A response
    # file under ``pair_dir`` is unreachable from inside that sandbox
    # — every write attempt fails with ``operation not permitted``,
    # the round runner's polling never sees the file, the round times
    # out, and the orchestrator relaunches the exchange forever.
    # Putting the response file inside the role's worktree keeps it
    # within the writable root. The orchestrator process itself is
    # unsandboxed and reads from any path, so polling continues to
    # work unchanged. The file is per-round transient (unlinked at
    # the start of each round in ``send_round``), so losing it on
    # worktree teardown is correct, not a regression.
    pair_dir = persistent_pair_root / f"issue-{issue_number}"
    coder_pair_dir = pair_dir / "coder"
    reviewer_pair_dir = pair_dir / "reviewer"
    coder_response = coder_worktree_path / ".issue-orchestrator" / "review-response.json"
    coder_recording = coder_pair_dir / TERMINAL_RECORDING_FILENAME
    reviewer_recording = reviewer_pair_dir / TERMINAL_RECORDING_FILENAME
    coder_completion = coder_pair_dir / "completion-coder.json"
    pair_validation_record = pair_dir / "validation-record.json"

    # Per-session slice paths: each role's run_dir gets its own
    # ``terminal-recording.jsonl`` populated incrementally at chapter
    # boundaries (see ``_RoleSliceMirror``). The manifest's primary
    # ``<role>_recording`` key points here so the timeline viewer's
    # ``ManifestAccessor.get_review_exchange_recording`` returns the
    # per-session projection by default. The canonical pair recording
    # is preserved under ``<role>_recording_pair`` for power users
    # and cross-exchange forensics.
    coder_session_slice = run_dir / "coder" / TERMINAL_RECORDING_FILENAME
    reviewer_session_slice = run_dir / "reviewer" / TERMINAL_RECORDING_FILENAME
    _prepare_session_slice(coder_session_slice)
    _prepare_session_slice(reviewer_session_slice)
    session_output.update_manifest(
        run_dir,
        ReviewExchangeRecordingPaths(
            persistent_pair_dir=pair_dir,
            coder_recording=coder_session_slice,
            reviewer_recording=reviewer_session_slice,
            coder_recording_pair=coder_recording,
            reviewer_recording_pair=reviewer_recording,
        ).to_manifest_fields(),
    )

    run_validation_record_path = run_dir / "validation-record.json"
    pair_validation = _PairValidationMirror(
        pair_dir=pair_dir,
        record_path=pair_validation_record,
        coder_worktree_path=coder_worktree_path,
        run_record_path=run_validation_record_path,
    )
    pair_validation.replace_from_initial(initial_validation_record_path)

    def _spawn_pair() -> PersistentExchangePair:
        # Cache miss: this is the first exchange for the issue (or the
        # previous pair died). Create the reviewer worktree and open
        # both sessions with pair-scoped env paths.
        reviewer_wt_path = reviewer_worktree_factory()
        # Reviewer response file lives inside the reviewer worktree
        # (writable root for the reviewer's seatbelt sandbox); see
        # the ``coder_response`` comment above for the full rationale.
        reviewer_response = reviewer_wt_path / ".issue-orchestrator" / "review-response.json"
        coder = _open_role_session(
            role="coder",
            agent=coder_agent,
            worktree=coder_worktree_path,
            run_dir=run_dir,
            recording_path=coder_recording,
            response_file=coder_response,
            completion_path=coder_completion,
            validation_output_dir=pair_dir,
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
        # (Reviewer doesn't write a coding-done completion, but we
        # still pass pair-scoped completion / validation paths so the
        # env layout is consistent across roles.)
        try:
            reviewer = _open_role_session(
                role="reviewer",
                agent=reviewer_agent,
                worktree=reviewer_wt_path,
                run_dir=run_dir,
                recording_path=reviewer_recording,
                response_file=reviewer_response,
                completion_path=reviewer_pair_dir / "completion-reviewer.json",
                validation_output_dir=pair_dir,
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
            reviewer_worktree_path=reviewer_wt_path,
            issue_key=issue_number,
            created_at=_wall_clock(),
            coder_response_path=coder_response,
            reviewer_response_path=reviewer_response,
            coder_recording_path=coder_recording,
            reviewer_recording_path=reviewer_recording,
            coder_completion_path=coder_completion,
            validation_record_path=pair_validation_record,
        )

    pair = _acquire_pair_or_emit_failure(
        pair_registry=pair_registry,
        issue_number=issue_number,
        session_name=session_name,
        spawn=_spawn_pair,
        emit=_emit,
    )

    # Always fast-forward the reviewer worktree at the start of every
    # reviewer round, including round 1. Round 1 of a *fresh* pair is a
    # no-op (the worktree was just created at the coder tip); round 1
    # of a *cached* pair from a previous exchange is the load-bearing
    # case — the coder may have advanced its branch between exchanges
    # and the reviewer needs the new tip. The caller-supplied
    # ``before_reviewer_round`` (if any) runs after the FF.
    def _ff_then_caller_hook(round_index: int) -> None:
        if coder_branch is not None:
            from .reviewer_worktree import (
                ReviewerWorktree,
                fast_forward_reviewer_worktree,
            )
            fast_forward_reviewer_worktree(
                ReviewerWorktree(
                    path=pair.reviewer_worktree_path,
                    coder_branch=coder_branch,
                ),
            )
        if before_reviewer_round is not None:
            before_reviewer_round(round_index)

    # Build per-role mirrors after the pair is acquired so the slice
    # bases are sampled from the *current* pair recording size — i.e.
    # everything the agent has emitted up to this exchange's first
    # round. Cached pairs have prior exchanges' content already in the
    # pair recording; the per-session slice must skip past it. The
    # mirror only owns offset translation now (chapter sidecars +
    # SSE payloads) — actual per-event mirroring is wired into the
    # role's ``MirroredTerminalRecordingWriter`` below so the slice
    # file fills in near real time, not just at chapter boundaries.
    coder_slice_base = recording_event_count(
        pair.coder_recording_path, require_recording=False,
    )
    reviewer_slice_base = recording_event_count(
        pair.reviewer_recording_path, require_recording=False,
    )
    coder_mirror = _RoleSliceMirror(
        pair_recording=pair.coder_recording_path,
        session_slice=coder_session_slice,
        slice_base=coder_slice_base,
    )
    reviewer_mirror = _RoleSliceMirror(
        pair_recording=pair.reviewer_recording_path,
        session_slice=reviewer_session_slice,
        slice_base=reviewer_slice_base,
    )

    # Live mirror registration. From this point on, every event the
    # agent's PTY drains into the canonical pair recording is *also*
    # appended to the per-session slice file. A user inspecting the
    # timeline mid-round (during a long reviewer think, or while a
    # round is hung waiting for response-file delivery) sees agent
    # output in near real time instead of waiting for a chapter
    # boundary to fire.
    #
    # Attach is INSIDE the try block so a failure here lands in the
    # REVIEW_EXCHANGE_FAILED handler — the orchestrator's loop bound
    # (PR #6267) governs retries / escalation rather than letting an
    # attach failure silently leave the timeline empty. The detach in
    # ``finally`` is load-bearing for the success path: leaving the
    # slice path attached past exchange end means the writer keeps
    # writing to a path under a possibly-torn-down run_dir, polluting
    # the next exchange's slice with the previous exchange's tail
    # bytes. ``_detach_slice_mirror`` is a no-op when the path was
    # never attached, so a partial attach (coder succeeded, reviewer
    # raised) cleans up safely.
    try:
        _attach_slice_mirror(pair.coder_session, coder_session_slice)
        _attach_slice_mirror(pair.reviewer_session, reviewer_session_slice)
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
            # Round-loop reads/writes pair-scoped files (stable across
            # exchanges) — never the run_dir-derived defaults that B1
            # used. On cache hit, ``pair.coder_response_path`` points
            # to the same file the agent's env was set to at spawn.
            coder_response=pair.coder_response_path,
            reviewer_response=pair.reviewer_response_path,
            coder_recording=pair.coder_recording_path,
            reviewer_recording=pair.reviewer_recording_path,
            coder_completion_path=pair.coder_completion_path,
            validation_record_path=pair.validation_record_path,
            prompt_files=ReviewExchangePromptFiles(
                validation_record=run_validation_record_path,
            ),
            pair_validation=pair_validation,
            coder_timeout_seconds=coder_agent.timeout_minutes * 60,
            reviewer_timeout_seconds=reviewer_agent.timeout_minutes * 60,
            max_rounds=max_rounds,
            max_no_progress=max_no_progress,
            require_validation=require_validation,
            coder_provider=_agent_provider(coder_agent),
            reviewer_provider=_agent_provider(reviewer_agent),
            before_reviewer_round=_ff_then_caller_hook,
            emit=_emit,
            coder_mirror=coder_mirror,
            reviewer_mirror=reviewer_mirror,
        )
    except Exception as exc:
        _release_pair_after_exchange_exception(
            pair_registry=pair_registry,
            pair=pair,
            issue_number=issue_number,
            session_name=session_name,
            exc=exc,
        )
        _emit_review_exchange_failed(
            emit=_emit,
            issue_number=issue_number,
            session_name=session_name,
            exc=exc,
        )
        raise
    finally:
        _detach_slice_mirror(pair.coder_session, coder_session_slice)
        _detach_slice_mirror(pair.reviewer_session, reviewer_session_slice)
        _clear_role_prompt_inbox(pair.coder_response_path)
        _clear_role_prompt_inbox(pair.reviewer_response_path)

    _release_pair_after_no_completion(
        pair_registry=pair_registry,
        pair=pair,
        issue_number=issue_number,
        session_name=session_name,
        outcome=outcome,
    )
    return outcome


@dataclass(frozen=True)
class _PairValidationMirror:
    """Own the pair-scoped validation record's freshness contract.

    The persistent pair lives across exchanges, but validation is only
    valid for the coder worktree's current HEAD. This mirror is the
    single owner for invalidating stale pair records, copying the
    current validation owner's record into pair scope, and asserting
    that a required validation record both passed and matches HEAD.
    """

    pair_dir: Path
    record_path: Path
    coder_worktree_path: Path
    run_record_path: Path | None = None

    def replace_from_initial(self, source: Path | None) -> None:
        """Mirror the caller's current validation source at exchange start.

        A missing source clears any prior pair record. That is
        intentional: an exchange without current validation evidence
        must not inherit the last exchange's passing record.
        """
        self._replace_from(source)

    def refresh_from_completion(
        self,
        payload: dict[str, Any],
        *,
        run_validation_record_path: Path,
    ) -> str | None:
        """Mirror validation evidence produced by this coder turn."""
        source, error = self._completion_validation_source(
            payload,
            run_validation_record_path=run_validation_record_path,
        )
        if error is not None:
            self._clear()
            return error
        self._replace_from(source)
        return None

    def current_validation_error(self) -> str | None:
        return _validation_record_error(
            self.record_path,
            current_head_sha=get_repo_head_sha(self.coder_worktree_path),
        )

    def _completion_validation_source(
        self,
        payload: dict[str, Any],
        *,
        run_validation_record_path: Path,
    ) -> tuple[Path | None, str | None]:
        raw_path = payload.get("validation_record_path")
        if raw_path is not None:
            if not isinstance(raw_path, str) or not raw_path.strip():
                return None, "completion validation_record_path must be a non-empty string"
            return self._validated_worktree_path(raw_path)
        if run_validation_record_path.exists():
            return run_validation_record_path, None
        return None, None

    def _validated_worktree_path(self, raw_path: str) -> tuple[Path | None, str | None]:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.coder_worktree_path / candidate
        try:
            resolved = candidate.resolve()
            worktree = self.coder_worktree_path.resolve()
            if resolved != self.record_path.resolve():
                resolved.relative_to(worktree)
        except (OSError, ValueError):
            return None, (
                "completion validation_record_path must stay under the coder worktree"
            )
        if not resolved.exists():
            return None, f"completion validation_record_path does not exist: {resolved}"
        if not resolved.is_file():
            return None, f"completion validation_record_path is not a file: {resolved}"
        return resolved, None

    def _replace_from(self, source: Path | None) -> None:
        if source is None or not source.exists():
            self._clear()
            return
        self.pair_dir.mkdir(parents=True, exist_ok=True)
        payload = source.read_bytes()
        _atomic_write_bytes(self.record_path, payload)
        if self.run_record_path is not None:
            _atomic_write_bytes(self.run_record_path, payload)

    def _clear(self) -> None:
        self.record_path.unlink(missing_ok=True)
        if self.run_record_path is not None:
            self.run_record_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Session bring-up
# ---------------------------------------------------------------------------


def _open_role_session(  # noqa: PLR0913
    *,
    role: str,
    agent: AgentConfig,
    worktree: Path,
    run_dir: Path,
    recording_path: Path,
    response_file: Path,
    completion_path: Path,
    validation_output_dir: Path,
    agent_label: str,
    web_port: int | None,
    issue_number: int,
    issue_title: str,
    session_name: str,
) -> PersistentSession:
    """Build the launch command + env for one role and open the persistent session.

    Per-role files (``response_file``, ``completion_path``,
    ``validation_output_dir``) are pair-scoped: the pair lives across
    every exchange the issue runs, so the agent's env points at
    stable paths. ``run_dir`` is per-exchange and is used only for
    the chapter-mirror recording path so the session viewer keeps
    seeing per-exchange snapshots.
    """
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
    completion_path.parent.mkdir(parents=True, exist_ok=True)
    validation_output_dir.mkdir(parents=True, exist_ok=True)
    env = _build_role_env(
        response_file=response_file,
        completion_path=completion_path,
        validation_output_dir=validation_output_dir,
        worktree=worktree,
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
        # No ``additional_recording_paths`` here — the original B2
        # design tried to mirror the pair-scoped recording into
        # ``run_dir/<role>/terminal-recording.jsonl`` for backward
        # compat, but the writer's mirror paths are fixed at spawn
        # time. On a registry cache hit (exchange 2+), the pair's
        # writer keeps writing to exchange 1's run_dir mirror — the
        # second exchange's run_dir would never see a recording.
        # ManifestAccessor now reads ``coder_recording`` /
        # ``reviewer_recording`` from the per-exchange manifest
        # instead (they point at the pair-scoped canonical file),
        # which is the right shape: one continuous recording per
        # pair, multiple exchanges referencing it via manifest.
    )


def _build_role_env(
    *,
    response_file: Path,
    completion_path: Path,
    validation_output_dir: Path,
    worktree: Path,
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

    All three file paths (``response_file``, ``completion_path``,
    ``validation_output_dir``) are pair-scoped — the agent's env is
    set once at spawn and points at locations that survive across
    every exchange the persistent pair handles.
    """
    from ..control.isolation import build_runtime_tool_env
    from .agent_runner_env import build_filtered_env

    overrides: dict[str, str] = {
        f"{ENV_PREFIX}COMPLETION_PATH": str(completion_path),
        f"{ENV_PREFIX}VALIDATION_OUTPUT_DIR": str(validation_output_dir),
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


def _agent_provider(agent: AgentConfig) -> AgentProvider:
    raw = agent.provider if agent.provider else agent.ai_system
    if not raw:
        raise ArtifactContractViolation(
            "AgentProvider",
            "value",
            "agent provider or ai_system must be configured",
        )
    return AgentProvider(raw)


# ---------------------------------------------------------------------------
# Round loop
# ---------------------------------------------------------------------------


def _agent_turn_scope(
    *,
    issue_number: int,
    exchange_run_id: str,
    round_index: int,
    attempt_index: int,
    role: Role,
    provider: AgentProvider,
) -> AgentTurnArtifactScope:
    return AgentTurnArtifactScope(
        issue_number=IssueNumber(issue_number),
        exchange_run_id=ExchangeRunId(exchange_run_id),
        round_index=PositiveRoundIndex(round_index),
        attempt_index=PositiveAttemptIndex(attempt_index),
        role=_artifact_role(role),
        provider=provider,
    )


def _artifact_role(role: Role) -> AgentRole:
    if role is Role.REVIEWER:
        return AgentRole.REVIEWER
    if role is Role.CODER:
        return AgentRole.CODER
    raise ArtifactContractViolation("AgentRole", "value", f"unsupported role: {role}")


def _turns_dir(exchange_dir: Path) -> Path:
    turns_dir = exchange_dir / "turns"
    turns_dir.mkdir(parents=True, exist_ok=True)
    return turns_dir


def _turn_artifact_stem(
    *,
    round_index: int,
    role: Role,
    attempt_index: int,
) -> str:
    return f"round-{round_index}-{role.value}-attempt-{attempt_index}"


def _turn_prompt_path(
    exchange_dir: Path,
    *,
    round_index: int,
    role: Role,
    attempt_index: int,
) -> Path:
    stem = _turn_artifact_stem(
        round_index=round_index,
        role=role,
        attempt_index=attempt_index,
    )
    return _turns_dir(exchange_dir) / f"{stem}.prompt.md"


def _role_prompt_inbox_path(response_file: Path) -> Path:
    """Stable role-local prompt inbox next to the response file.

    The role process is launched with its worktree as cwd, while its response
    file lives under ``<worktree>/.issue-orchestrator``. Writing each turn's
    full prompt beside that response file keeps the prompt readable inside the
    role's sandbox and lets the PTY carry only a short wake-up message.

    This inbox is intentionally transient and overwritten per turn; the durable
    per-turn prompt remains under ``<exchange_dir>/turns/``.
    """
    return response_file.with_name("review-exchange-turn-prompt.md")


def _write_role_prompt_inbox(response_file: Path, prompt_text: str) -> Path:
    path = _role_prompt_inbox_path(response_file)
    _atomic_write_bytes(path, prompt_text.encode("utf-8"))
    return path


def _clear_role_prompt_inbox(response_file: Path) -> None:
    _role_prompt_inbox_path(response_file).unlink(missing_ok=True)


def _build_prompt_inbox_notice(
    *,
    role: Role,
    round_index: int,
    attempt_index: int,
    prompt_path: Path,
) -> str:
    return (
        f"Review-exchange {role.value} turn round={round_index} "
        f"attempt={attempt_index} is ready.\n"
        f"Read the full instructions from: {prompt_path}\n"
        "Follow that file exactly, then write one JSON response line to "
        "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE."
    )


def _persist_turn_prompt(
    exchange_dir: Path,
    *,
    round_index: int,
    role: Role,
    attempt_index: int,
    prompt_text: str,
) -> Path:
    path = _turn_prompt_path(
        exchange_dir,
        round_index=round_index,
        role=role,
        attempt_index=attempt_index,
    )
    path.write_text(prompt_text, encoding="utf-8")
    return path


def _chapters_path(run_dir: Path, role: Role) -> Path:
    return run_dir / role.value / "chapters.json"


def _build_reviewer_turn_started(
    *,
    scope: AgentTurnArtifactScope,
    prompt_path: Path,
    recording_path: Path,
    chapters_path: Path,
) -> ReviewerTurnStarted:
    return ReviewerTurnStarted(
        scope=scope,
        prompt=PromptArtifact(scope=scope, file=ExistingNonEmptyFile(prompt_path)),
        terminal_recording=TerminalRecordingArtifact(
            scope=scope,
            file=ExistingFile(recording_path),
        ),
        chapters=ChapterSidecarArtifact(
            scope=scope,
            file=ExistingFile(chapters_path),
        ),
    )


def _build_coder_turn_started(
    *,
    scope: AgentTurnArtifactScope,
    prompt_path: Path,
    recording_path: Path,
    chapters_path: Path,
) -> CoderTurnStarted:
    return CoderTurnStarted(
        scope=scope,
        prompt=PromptArtifact(scope=scope, file=ExistingNonEmptyFile(prompt_path)),
        terminal_recording=TerminalRecordingArtifact(
            scope=scope,
            file=ExistingFile(recording_path),
        ),
        chapters=ChapterSidecarArtifact(
            scope=scope,
            file=ExistingFile(chapters_path),
        ),
    )


def _event_artifact_refs(refs: tuple[ArtifactRef, ...]) -> list[dict[str, str]]:
    return [ref.to_event_artifact() for ref in refs]


def _turn_completed(
    started: ReviewerTurnStarted | CoderTurnStarted,
    result_path: Path,
    response: ReviewExchangeResponse,
) -> ReviewerTurnCompleted | CoderTurnCompleted:
    response_artifact = ReviewResponseArtifact(
        scope=started.scope,
        file=ExistingNonEmptyFile(result_path),
    )
    if isinstance(started, ReviewerTurnStarted):
        return ReviewerTurnCompleted(
            started=started,
            response=response_artifact,
            response_type=response.response_type,
            response_text=response.response_text,
        )
    return CoderTurnCompleted(
        started=started,
        response=response_artifact,
        response_type=response.response_type,
        response_text=response.response_text,
    )


def _persist_turn_contract(
    exchange_dir: Path,
    *,
    round_index: int,
    role: Role,
    attempt_index: int,
    suffix: str,
    fields: dict[str, object],
) -> Path:
    stem = _turn_artifact_stem(
        round_index=round_index,
        role=role,
        attempt_index=attempt_index,
    )
    path = _turns_dir(exchange_dir) / f"{stem}.{suffix}.json"
    path.write_text(json.dumps(fields, indent=2, sort_keys=True), encoding="utf-8")
    return path


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
    coder_completion_path: Path,
    validation_record_path: Path,
    prompt_files: ReviewExchangePromptFiles,
    pair_validation: _PairValidationMirror,
    coder_timeout_seconds: float,
    reviewer_timeout_seconds: float,
    max_rounds: int,
    max_no_progress: int,
    require_validation: bool,
    coder_provider: AgentProvider,
    reviewer_provider: AgentProvider,
    before_reviewer_round: Callable[[int], None] | None,
    emit: Callable[[EventName, dict[str, Any]], None],
    coder_mirror: _RoleSliceMirror,
    reviewer_mirror: _RoleSliceMirror,
) -> ReviewExchangeOutcome:
    no_progress_count = 0
    last_reviewer_text: str | None = None
    last_coder_text: str | None = None

    for round_index in range(1, max_rounds + 1):
        if before_reviewer_round is not None:
            before_reviewer_round(round_index)

        # ----- Reviewer turn -----
        reviewer_packet = ReviewExchangeTurnPacket(
            issue_number=issue_number,
            issue_title=issue_title,
            round_index=round_index,
            role=Role.REVIEWER,
            require_validation=require_validation,
            run_dir=run_dir,
            prompt_files=prompt_files,
            last_coder_text=last_coder_text,
            last_reviewer_text=last_reviewer_text,
        )
        _persist_turn_packet(exchange_dir, reviewer_packet)
        reviewer_prompt_text = build_reviewer_prompt(reviewer_packet)
        reviewer_prompt_path = _persist_turn_prompt(
            exchange_dir,
            round_index=round_index,
            role=Role.REVIEWER,
            attempt_index=1,
            prompt_text=reviewer_prompt_text,
        )
        reviewer_scope = _agent_turn_scope(
            issue_number=issue_number,
            exchange_run_id=exchange_run_id,
            round_index=round_index,
            attempt_index=1,
            role=Role.REVIEWER,
            provider=reviewer_provider,
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
            mirror=reviewer_mirror,
        )
        reviewer_started = _build_reviewer_turn_started(
            scope=reviewer_scope,
            prompt_path=reviewer_prompt_path,
            recording_path=reviewer_mirror.session_slice,
            chapters_path=_chapters_path(run_dir, Role.REVIEWER),
        )
        _persist_turn_contract(
            exchange_dir,
            round_index=round_index,
            role=Role.REVIEWER,
            attempt_index=1,
            suffix="started",
            fields=reviewer_started.to_manifest_fields(),
        )
        emit(EventName.REVIEW_EXCHANGE_ROLE_PROMPTED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
            "attempt_index": 1,
            "role": "reviewer",
            "prompt_chars": len(reviewer_prompt_text),
            "artifact_refs": _event_artifact_refs(reviewer_started.artifact_refs()),
        })
        reviewer = _send_role_round(
            session=reviewer_session,
            role=Role.REVIEWER,
            turn_started=reviewer_started,
            response_file=reviewer_response,
            recording_path=reviewer_recording,
            prompt=reviewer_prompt_text,
            timeout_seconds=reviewer_timeout_seconds,
            session_output=session_output,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            cycle_index=round_index,
            session_name=session_name,
            emit=emit,
            mirror=reviewer_mirror,
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
                validation_record_path=validation_record_path,
            )

        validation_error = pair_validation.current_validation_error() if require_validation else None
        if reviewer.response_type == "ok" and validation_error is not None:
            reviewer = ReviewExchangeResponse(
                response_type="changes_requested",
                response_text=(
                    f"{validation_error}. Address the failing "
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
                validation_record_path=validation_record_path,
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
                validation_record_path=validation_record_path,
            )

        last_reviewer_text = reviewer.response_text

        # ----- Coder turn -----
        coder_packet = ReviewExchangeTurnPacket(
            issue_number=issue_number,
            issue_title=issue_title,
            round_index=round_index,
            role=Role.CODER,
            require_validation=require_validation,
            run_dir=run_dir,
            reviewer_feedback=reviewer.response_text,
        )
        _persist_turn_packet(exchange_dir, coder_packet)
        coder_prompt_text = build_coder_prompt(coder_packet)
        coder_prompt_path = _persist_turn_prompt(
            exchange_dir,
            round_index=round_index,
            role=Role.CODER,
            attempt_index=1,
            prompt_text=coder_prompt_text,
        )
        coder_scope = _agent_turn_scope(
            issue_number=issue_number,
            exchange_run_id=exchange_run_id,
            round_index=round_index,
            attempt_index=1,
            role=Role.CODER,
            provider=coder_provider,
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
            mirror=coder_mirror,
        )
        coder_started = _build_coder_turn_started(
            scope=coder_scope,
            prompt_path=coder_prompt_path,
            recording_path=coder_mirror.session_slice,
            chapters_path=_chapters_path(run_dir, Role.CODER),
        )
        _persist_turn_contract(
            exchange_dir,
            round_index=round_index,
            role=Role.CODER,
            attempt_index=1,
            suffix="started",
            fields=coder_started.to_manifest_fields(),
        )
        emit(EventName.REVIEW_EXCHANGE_ROLE_PROMPTED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": round_index,
            "attempt_index": 1,
            "role": "coder",
            "prompt_chars": len(coder_prompt_text),
            "artifact_refs": _event_artifact_refs(coder_started.artifact_refs()),
        })
        # Clear the previous turn's completion artifact so a stale file
        # from round N-1 cannot satisfy round N's protocol guardrail —
        # the guardrail must observe an artifact freshly written during
        # *this* round's coding-done invocation.
        _clear_coder_completion(coder_completion_path)
        coder = _send_role_round(
            session=coder_session,
            role=Role.CODER,
            turn_started=coder_started,
            response_file=coder_response,
            recording_path=coder_recording,
            prompt=coder_prompt_text,
            timeout_seconds=coder_timeout_seconds,
            session_output=session_output,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            cycle_index=round_index,
            session_name=session_name,
            emit=emit,
            mirror=coder_mirror,
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
                validation_record_path=validation_record_path,
            )

        coder, protocol_outcome = _enforce_coder_protocol(
            session_output=session_output,
            coder_session=coder_session,
            coder=coder,
            reviewer=reviewer,
            coder_provider=coder_provider,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            coder_response=coder_response,
            coder_recording=coder_recording,
            coder_completion_path=coder_completion_path,
            validation_record_path=validation_record_path,
            pair_validation=pair_validation,
            coder_timeout_seconds=coder_timeout_seconds,
            require_validation=require_validation,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            session_name=session_name,
            cycle_index=round_index,
            emit=emit,
            coder_mirror=coder_mirror,
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
        validation_record_path=validation_record_path,
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
    coder_provider: AgentProvider,
    run_dir: Path,
    exchange_dir: Path,
    coder_response: Path,
    coder_recording: Path,
    coder_completion_path: Path,
    validation_record_path: Path,
    pair_validation: _PairValidationMirror,
    coder_timeout_seconds: float,
    require_validation: bool,
    exchange_run_id: str,
    issue_number: int,
    session_name: str,
    cycle_index: int,
    emit: Callable[[EventName, dict[str, Any]], None],
    coder_mirror: _RoleSliceMirror,
) -> tuple[ReviewExchangeResponse, ReviewExchangeOutcome | None]:
    """Validate the coder produced its completion-coder.json artifact, retry
    with a remediation prompt up to ``_CODER_PROTOCOL_RETRY_LIMIT`` times,
    and return either the validated response or a terminal outcome.

    Mirrors the active runner's _run_coder_round_with_protocol_retries.
    Without this guardrail a coder could advance the exchange by writing
    only the review-response file while skipping coding-done.
    """
    protocol_error = _validate_coder_completion(
        completion_path=coder_completion_path,
        pair_validation=pair_validation,
        run_validation_record_path=run_dir / "validation-record.json",
        require_validation=require_validation,
    )
    next_attempt_index = 2
    while protocol_error is not None and next_attempt_index <= _CODER_PROTOCOL_RETRY_LIMIT + 1:
        attempt_index = next_attempt_index
        next_attempt_index += 1
        retry_prompt = (
            f"{protocol_error}\n"
            "Run `coding-done completed --implementation '...' --problems '...'` "
            "(or `coding-done blocked --reason '...' --attempted '...'` if you "
            "cannot continue), then write your one-line JSON response again to "
            "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE."
        )
        retry_prompt_path = _persist_turn_prompt(
            exchange_dir,
            round_index=cycle_index,
            role=Role.CODER,
            attempt_index=attempt_index,
            prompt_text=retry_prompt,
        )
        retry_scope = _agent_turn_scope(
            issue_number=issue_number,
            exchange_run_id=exchange_run_id,
            round_index=cycle_index,
            attempt_index=attempt_index,
            role=Role.CODER,
            provider=coder_provider,
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
            mirror=coder_mirror,
        )
        retry_started = _build_coder_turn_started(
            scope=retry_scope,
            prompt_path=retry_prompt_path,
            recording_path=coder_mirror.session_slice,
            chapters_path=_chapters_path(run_dir, Role.CODER),
        )
        _persist_turn_contract(
            exchange_dir,
            round_index=cycle_index,
            role=Role.CODER,
            attempt_index=attempt_index,
            suffix="started",
            fields=retry_started.to_manifest_fields(),
        )
        emit(EventName.REVIEW_EXCHANGE_ROLE_PROMPTED, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": cycle_index,
            "attempt_index": attempt_index,
            "role": "coder",
            "prompt_chars": len(retry_prompt),
            "protocol_retry": True,
            "artifact_refs": _event_artifact_refs(retry_started.artifact_refs()),
        })
        # Same freshness invariant as the initial turn: drop any file
        # left over from the previous attempt before the retry runs.
        _clear_coder_completion(coder_completion_path)
        retry_response = _send_role_round(
            session=coder_session,
            role=Role.CODER,
            turn_started=retry_started,
            response_file=coder_response,
            recording_path=coder_recording,
            prompt=retry_prompt,
            timeout_seconds=coder_timeout_seconds,
            session_output=session_output,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            cycle_index=cycle_index,
            session_name=session_name,
            emit=emit,
            mirror=coder_mirror,
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
                validation_record_path=validation_record_path,
            )
        coder = retry_response
        protocol_error = _validate_coder_completion(
            completion_path=coder_completion_path,
            pair_validation=pair_validation,
            run_validation_record_path=run_dir / "validation-record.json",
            require_validation=require_validation,
        )
    if protocol_error is not None:
        return coder, _build_outcome_for_protocol_error(
            exchange_dir=exchange_dir,
            round_index=cycle_index,
            last_reviewer=reviewer,
            last_coder=coder,
            protocol_error=protocol_error,
            emit=emit,
            validation_record_path=validation_record_path,
            issue_number=issue_number,
            session_name=session_name,
        )
    return coder, None


def _send_role_round(  # noqa: PLR0913
    *,
    session: PersistentSession,
    role: Role,
    turn_started: ReviewerTurnStarted | CoderTurnStarted,
    response_file: Path,
    recording_path: Path,
    prompt: str,
    timeout_seconds: float,
    session_output: SessionOutput,
    run_dir: Path,
    exchange_dir: Path,
    exchange_run_id: str,
    issue_number: int,
    cycle_index: int,
    session_name: str,
    emit: Callable[[EventName, dict[str, Any]], None],
    mirror: _RoleSliceMirror,
) -> ReviewExchangeResponse | None:
    """Send one role's round prompt and convert the response to a domain object.

    Returns ``None`` if the role timed out or died — the caller emits
    REVIEW_EXCHANGE_ROLE_TIMEOUT and bails out of the exchange.

    Persists the per-attempt parsed result as a session artifact under
    ``<exchange_dir>/turns/round-<n>-<role>-attempt-<m>.result.json``
    for replay and diagnostics.
    """
    role_value = role.value
    attempt_index = turn_started.scope.attempt_index.value
    prompt_inbox_path = _write_role_prompt_inbox(response_file, prompt)
    pty_notice = _build_prompt_inbox_notice(
        role=role,
        round_index=cycle_index,
        attempt_index=attempt_index,
        prompt_path=prompt_inbox_path,
    )
    try:
        parsed = send_round(
            session,
            prompt=pty_notice,
            response_file=response_file,
            timeout_seconds=timeout_seconds,
            # Tag heartbeat/diagnostic logs with role + cycle so an
            # interleaved coder + reviewer log is decodable without
            # cross-referencing PIDs (#6160 e2e regression: 17 minutes
            # of unattributed silence).
            role_label=f"{role_value}@round-{cycle_index}",
        )
    except (PersistentRoundTimeoutError, PersistentRoundError) as exc:
        logger.warning(
            "%s round %d failed: %s", role_value, cycle_index, exc,
        )
        # The typed result artifact must exist on the failure path
        # too — this is the case operators most need to inspect, and
        # an asymmetric "result.json only on the happy path" contract
        # would leave the on-disk trail incomplete for exactly the
        # rounds that need replay/forensics.
        typed_result = ReviewExchangeTurnResult.for_no_completion(str(exc))
        result_path = _persist_turn_result(
            exchange_dir,
            round_index=cycle_index,
            role=role,
            attempt_index=attempt_index,
            result=typed_result,
        )
        response = _legacy_response_from_typed_result(typed_result)
        _record_chapter(
            session_output=session_output,
            run_dir=run_dir,
            role=role_value,
            recording_path=recording_path,
            exchange_run_id=exchange_run_id,
            issue_number=issue_number,
            cycle_index=cycle_index,
            section=CHAPTER_SECTION_TIMEOUT,
            label=f"Round {cycle_index} {role_value} timeout/error",
            session_name=session_name,
            emit=emit,
            mirror=mirror,
        )
        completed = _turn_completed(turn_started, result_path, response)
        _persist_turn_contract(
            exchange_dir,
            round_index=cycle_index,
            role=role,
            attempt_index=attempt_index,
            suffix="completed",
            fields=completed.to_manifest_fields(),
        )
        emit(EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT, {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": cycle_index,
            "attempt_index": attempt_index,
            "role": role_value,
            "reason": "no_completion",
            "detail": str(exc),
            "artifact_refs": _event_artifact_refs(completed.artifact_refs()),
        })
        return None

    typed_result = ReviewExchangeTurnResult.from_agent_dict(
        parsed, raw_output=None,
    )
    result_path = _persist_turn_result(
        exchange_dir,
        round_index=cycle_index,
        role=role,
        attempt_index=attempt_index,
        result=typed_result,
    )
    response = _legacy_response_from_typed_result(typed_result)
    _record_chapter(
        session_output=session_output,
        run_dir=run_dir,
        role=role_value,
        recording_path=recording_path,
        exchange_run_id=exchange_run_id,
        issue_number=issue_number,
        cycle_index=cycle_index,
        section=CHAPTER_SECTION_FEEDBACK,
        label=f"Round {cycle_index} {role_value} feedback",
        session_name=session_name,
        emit=emit,
        mirror=mirror,
    )
    completed = _turn_completed(turn_started, result_path, response)
    _persist_turn_contract(
        exchange_dir,
        round_index=cycle_index,
        role=role,
        attempt_index=attempt_index,
        suffix="completed",
        fields=completed.to_manifest_fields(),
    )
    emit(EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": cycle_index,
        "attempt_index": attempt_index,
        "role": role_value,
        "response_type": response.response_type,
        "getting_closer": response.getting_closer,
        "artifact_refs": _event_artifact_refs(completed.artifact_refs()),
    })
    return response


def _legacy_response_from_typed_result(
    result: ReviewExchangeTurnResult,
) -> ReviewExchangeResponse:
    """Adapter for outcome code still typed as ``ReviewExchangeResponse``.

    Remove once exchange outcome/control consumers accept
    ``ReviewExchangeTurnResult`` directly.
    """
    if result.kind is TurnResultKind.PROTOCOL_ERROR:
        return ReviewExchangeResponse(
            response_type="protocol_error",
            response_text=result.response_text,
            getting_closer=result.getting_closer if result.getting_closer is not None else False,
            raw_json=result.raw_json,
            raw_output=result.raw_output,
        )
    return ReviewExchangeResponse(
        response_type=result.kind.value,
        response_text=result.response_text,
        getting_closer=result.getting_closer,
        raw_json=result.raw_json,
        raw_output=result.raw_output,
    )


def _persist_turn_packet(
    exchange_dir: Path, packet: ReviewExchangeTurnPacket,
) -> None:
    """Write the per-turn input packet as a session artifact.

    The artifact lives at
    ``<exchange_dir>/turns/round-<round>-<role>.packet.json`` so a
    failed exchange can be replayed/inspected from the on-disk state
    without walking the recording stream.
    """
    path = _turns_dir(exchange_dir) / (
        f"round-{packet.round_index}-{packet.role.value}.packet.json"
    )
    path.write_text(
        json.dumps(packet.to_manifest_fields(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _persist_turn_result(
    exchange_dir: Path,
    *,
    round_index: int,
    role: Role,
    attempt_index: int,
    result: ReviewExchangeTurnResult,
) -> Path:
    """Write the per-attempt parsed result as a session artifact.

    Sibling to ``_persist_turn_packet``: a failed exchange leaves both
    the packet (what the orchestrator gave the agent) and the result
    (what the orchestrator parsed from the agent's response) on disk.
    """
    stem = _turn_artifact_stem(
        round_index=round_index,
        role=role,
        attempt_index=attempt_index,
    )
    path = _turns_dir(exchange_dir) / f"{stem}.result.json"
    path.write_text(
        json.dumps(result.to_manifest_fields(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


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
    validation_record_path: Path,
) -> ReviewExchangeOutcome:
    summary = _write_summary(
        exchange_dir, round_index,
        status="ok", reason="reviewer_ok", reviewer_response=reviewer,
        validation_record_path=validation_record_path,
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
    validation_record_path: Path,
) -> ReviewExchangeOutcome:
    summary = _write_summary(
        exchange_dir, round_index,
        status="stopped", reason="reviewer_reports_no_progress",
        reviewer_response=reviewer,
        validation_record_path=validation_record_path,
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
    validation_record_path: Path,
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
        validation_record_path=validation_record_path,
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
    validation_record_path: Path,
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
        validation_record_path=validation_record_path,
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


def _write_review_exchange_manifest_header(
    session_output: SessionOutput,
    run_dir: Path,
    header: ReviewExchangeManifestHeader,
) -> None:
    """Stamp the review-exchange manifest header.

    Extracted to keep ``run_persistent_session_exchange`` under the
    C901 ceiling and to give the manifest section a typed name. The
    header itself documents the contract; this helper is the seam
    where the typed value crosses into the loose-dict
    ``update_manifest`` API.
    """
    session_output.update_manifest(run_dir, header.to_manifest_fields())


def _read_validation_facts(
    path: Path | None,
) -> tuple[str | None, bool | None]:
    """Read ``(head_sha, passed)`` from a validation-record.json.

    Returns ``(None, None)`` when the path is None, missing, or
    unreadable as JSON. ``head_sha`` is None when the field is
    absent/empty; ``passed`` is None when the field is absent or
    not a bool.

    The summary writer (and the cache loader, in a later commit)
    use this to populate ``ResumeFacts`` without leaking validation-
    record schema concerns into other modules.
    """
    if path is None or not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    head_sha = data.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        head_sha = None
    passed = data.get("passed")
    if not isinstance(passed, bool):
        passed = None
    return head_sha, passed


def _write_summary(
    exchange_dir: Path,
    round_index: int,
    *,
    status: str,
    reason: str,
    reviewer_response: ReviewExchangeResponse | None,
    validation_record_path: Path | None,
) -> dict[str, Any]:
    """Persist summary.json atomically.

    The summary records *facts* about the exchange that just ran:
    ``status``, ``reason``, ``completed_rounds``, ``response_text``,
    ``timestamp``, plus — when the validation record is readable —
    ``head_sha`` and ``validation_passed``. Policy (cacheable / halt
    / retry / stale) is NOT encoded here; the cache loader feeds
    these fields into ``ReviewExchangeResumeDecision.decide`` to
    determine the next-tick action.

    Pre-this-commit, the writer encoded policy by selectively
    omitting ``head_sha`` based on status. That dual-purpose use of
    one field (fact AND control signal) was the root cause of the
    PR #6270 review-feedback whack-a-mole: every patch that adjusted
    "which statuses cache-hit" mutated which facts got persisted,
    and downstream consumers re-inferred policy at three different
    sites. Recording facts unconditionally and centralizing policy
    in one named helper ends that drift.

    ``head_sha`` and ``validation_passed`` are still omitted (rather
    than written as None) when the validation record cannot be
    read at all — the caller should treat absence as "we don't
    know" rather than "validation explicitly failed." The cache
    loader's ``ResumeFacts`` mapping handles each case.
    """
    summary: dict[str, Any] = {
        "completed_rounds": round_index,
        "status": status,
        "response_text": reviewer_response.response_text if reviewer_response else None,
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    head_sha, passed = _read_validation_facts(validation_record_path)
    if head_sha is not None:
        summary["head_sha"] = head_sha
    if passed is not None:
        summary["validation_passed"] = passed
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
    mirror: _RoleSliceMirror | None = None,
) -> int:
    """Capture the recording's current event index, append a chapter,
    emit ``REVIEW_EXCHANGE_CHAPTER_RECORDED``, and (when ``mirror`` is
    provided) project the new events into the per-session run_dir slice.

    Returns the captured ``event_index`` so callers can chain behavior
    onto it without having to count the recording themselves. The
    returned value is **always pair-relative** so callers tracking
    exchange-wide event progression see absolute positions; the
    chapter sidecar and the SSE payload, by contrast, hold the
    slice-relative offset (when a ``mirror`` is provided) because the
    manifest points the viewer at the slice file.

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

    Per-event slice mirroring is no longer chapter-driven — the role's
    ``MirroredTerminalRecordingWriter`` writes to both the pair file
    and the per-session slice on every event drained from the PTY,
    so chapters only own offset translation here.
    """
    pair_event_index = recording_event_count(recording_path)
    # Slice-relative when a mirror is in play so the viewer can scrub
    # the manifest-pointed slice directly. Without the translation, a
    # cached pair on exchange 2 records pair-relative offsets in the
    # hundreds while the slice file only holds dozens of events, and
    # the web replay route slices ``all_events[chapter_offset:]`` to
    # an empty window.
    sidecar_event_index = (
        mirror.pair_to_slice_offset(pair_event_index)
        if mirror is not None
        else pair_event_index
    )
    session_output.record_exchange_chapter(
        run_dir,
        role=role,
        exchange_run_id=exchange_run_id,
        issue_number=issue_number,
        cycle_index=cycle_index,
        section=section,
        recording_event_index=sidecar_event_index,
        recorded_at=datetime.now(timezone.utc).isoformat(),
        label=label,
    )
    emit(EventName.REVIEW_EXCHANGE_CHAPTER_RECORDED, {
        "issue_number": issue_number,
        "session_name": session_name,
        "round_index": cycle_index,
        "role": role,
        "section": section,
        "recording_event_index": sidecar_event_index,
        "label": label,
    })
    return pair_event_index


def _validation_record_error(
    record_path: Path,
    *,
    current_head_sha: str | None,
) -> str | None:
    if not record_path.exists():
        return "validation-record.json missing"
    try:
        data = json.loads(record_path.read_text())
    except json.JSONDecodeError:
        return "validation-record.json is not valid JSON"
    if not isinstance(data, dict):
        return "validation-record.json must be a JSON object"
    if data.get("passed") is not True:
        return "validation-record.json did not pass"
    if current_head_sha is None:
        return "cannot determine current HEAD for validation-record.json"
    record_head_sha = data.get("head_sha")
    if not isinstance(record_head_sha, str) or not record_head_sha:
        return "validation-record.json missing head_sha"
    if record_head_sha != current_head_sha:
        return (
            "validation-record.json head "
            f"{record_head_sha[:12]} does not match current HEAD "
            f"{current_head_sha[:12]}"
        )
    return None


def _clear_coder_completion(completion_path: Path) -> None:
    """Unlink any prior coder completion artifact so the protocol guardrail
    observes only the file freshly written during the current turn.

    Without this, ``_validate_coder_completion`` sees a stale artifact
    from an earlier round and accepts a coder that skipped coding-done
    on this turn entirely. The active runner avoids this because each
    round spawns a fresh coder process whose env points at a per-round
    path; the persistent runner shares the path across rounds — and
    across exchanges, in B2 — so we have to invalidate explicitly.
    """
    completion_path.unlink(missing_ok=True)


def _validate_coder_completion(
    *,
    completion_path: Path,
    pair_validation: _PairValidationMirror,
    run_validation_record_path: Path,
    require_validation: bool,
) -> str | None:
    """Mirror of control/review_exchange_loop._validate_coder_protocol.

    The coder must produce a completion-coder.json artifact (the
    ``coding-done`` CLI's output) and, when ``require_validation`` is on,
    a passing validation-record.json. A coder that only writes the
    review-response file but skips coding-done would otherwise advance
    the exchange by accident.
    """
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
    validation_source_error = pair_validation.refresh_from_completion(
        payload,
        run_validation_record_path=run_validation_record_path,
    )
    if require_validation:
        if validation_source_error is not None:
            return validation_source_error
        return pair_validation.current_validation_error()
    return None


# ``_atomic_write_json`` is the shared helper from ``infra.atomic_io``;
# re-export under the private name so the existing test that monkeypatches
# ``pse.os.replace`` continues to find the same write path.
from ..infra.atomic_io import atomic_write_json as _atomic_write_json  # noqa: E402
from ..infra.atomic_io import atomic_write_bytes as _atomic_write_bytes  # noqa: E402
