"""Recording-contract owner for cached persistent review-exchange pairs."""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.review_exchange_run import ReviewExchangeRun, ReviewExchangeRunAssets
from ..domain.review_exchange_turn import Role
from ..events import EventName
from .persistent_exchange_pair_registry_inmemory import (
    InMemoryPersistentExchangePairRegistry,
    PersistentExchangePair,
)

logger = logging.getLogger(__name__)


class PairRecordingContractErrorKind(str, enum.Enum):
    NO_WRITER = "no_writer"
    PATH_MISMATCH = "path_mismatch"
    MISSING_FILE = "missing_file"
    NOT_A_FILE = "not_a_file"


@dataclass(frozen=True)
class PairRecordingContractError:
    role: Role
    kind: PairRecordingContractErrorKind
    detail: str

    def __str__(self) -> str:
        return f"{self.role.value} {self.kind.value}: {self.detail}"


class PairRecordingContractViolation(RuntimeError):
    def __init__(
        self,
        *,
        issue_number: int,
        errors: tuple[PairRecordingContractError, ...],
    ) -> None:
        self.issue_number = issue_number
        self.errors = errors
        joined = "; ".join(str(error) for error in errors)
        super().__init__(
            f"persistent pair recording contract invalid after respawn "
            f"issue={issue_number} errors={joined}"
        )


@dataclass(frozen=True, slots=True)
class PairExchangeRunBinding:
    """Owner-injected exchange-run binding for a cached persistent pair.

    Persistent role processes keep pair-scoped write paths in their environment
    for the lifetime of the process. Each exchange run still has its own typed
    assets; this binding tells the pair owner which run is currently consuming
    the stable pair-scoped writes.
    """

    run_id: str
    assets: ReviewExchangeRunAssets

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("pair exchange binding requires run_id")
        if not self.assets.run_dir.is_dir():
            raise ValueError(
                "pair exchange binding requires an existing run_dir: "
                f"{self.assets.run_dir}"
            )
        if not (self.assets.run_dir / "manifest.json").is_file():
            raise ValueError(
                "pair exchange binding requires run_dir/manifest.json: "
                f"{self.assets.run_dir}"
            )

    @classmethod
    def from_exchange_run(
        cls, exchange_run: ReviewExchangeRun
    ) -> "PairExchangeRunBinding":
        return cls(run_id=exchange_run.run_id, assets=exchange_run.assets)

    @property
    def run_dir(self) -> Path:
        return self.assets.run_dir


def _pair_recording_contract_errors(
    pair: PersistentExchangePair,
) -> tuple[PairRecordingContractError, ...]:
    """Return recording-path contract violations for a cached pair."""
    errors: list[PairRecordingContractError] = []
    for role, session, recording_path in (
        (Role.CODER, pair.coder_session, pair.coder_recording_path),
        (Role.REVIEWER, pair.reviewer_session, pair.reviewer_recording_path),
    ):
        writer = session.log_writer
        if writer is None:
            errors.append(
                PairRecordingContractError(
                    role=role,
                    kind=PairRecordingContractErrorKind.NO_WRITER,
                    detail="session has no terminal recording writer",
                )
            )
        elif Path(writer.recording_path) != recording_path:
            errors.append(
                PairRecordingContractError(
                    role=role,
                    kind=PairRecordingContractErrorKind.PATH_MISMATCH,
                    detail=(
                        f"writer path {writer.recording_path} does not match "
                        f"pair path {recording_path}"
                    ),
                )
            )
        if not recording_path.exists():
            errors.append(
                PairRecordingContractError(
                    role=role,
                    kind=PairRecordingContractErrorKind.MISSING_FILE,
                    detail=f"recording path missing: {recording_path}",
                )
            )
        elif not recording_path.is_file():
            errors.append(
                PairRecordingContractError(
                    role=role,
                    kind=PairRecordingContractErrorKind.NOT_A_FILE,
                    detail=f"recording path is not a file: {recording_path}",
                )
            )
    return tuple(errors)


def _pair_matches_exchange_run(
    pair: PersistentExchangePair,
    binding: PairExchangeRunBinding,
) -> bool:
    """Return True when the live pair was spawned for this run binding."""
    return pair.exchange_run_id == binding.run_id and pair.run_dir == binding.run_dir


def _acquire_pair_spawned_for_exchange_run(
    *,
    pair_registry: InMemoryPersistentExchangePairRegistry,
    issue_number: int,
    binding: PairExchangeRunBinding,
    spawn: Callable[[], PersistentExchangePair],
) -> PersistentExchangePair:
    pair = pair_registry.acquire(issue_key=issue_number, spawn=spawn)
    if _pair_matches_exchange_run(pair, binding):
        return pair
    logger.info(
        "[REVIEW_EXCHANGE] cached persistent pair belongs to a different "
        "exchange run; releasing before use issue=%s previous_run_id=%s "
        "current_run_id=%s previous_run_dir=%s current_run_dir=%s",
        issue_number,
        pair.exchange_run_id,
        binding.run_id,
        pair.run_dir,
        binding.run_dir,
    )
    pair_registry.release(
        issue_number,
        reason="exchange-run-changed-on-acquire",
    )
    pair = pair_registry.acquire(issue_key=issue_number, spawn=spawn)
    if not _pair_matches_exchange_run(pair, binding):
        raise RuntimeError(
            "spawned persistent pair does not match current exchange run: "
            f"issue={issue_number} pair_run_id={pair.exchange_run_id} "
            f"binding_run_id={binding.run_id} pair_run_dir={pair.run_dir} "
            f"binding_run_dir={binding.run_dir}"
        )
    return pair


def acquire_pair_with_recording_contract(
    *,
    pair_registry: InMemoryPersistentExchangePairRegistry,
    issue_number: int,
    exchange_run: PairExchangeRunBinding,
    spawn: Callable[[], PersistentExchangePair],
) -> PersistentExchangePair:
    """Acquire a live pair and bind it to the owner-injected exchange run.

    Role process environment contains owner-injected run assets
    (``RUN_DIR``, ``SESSION_ID``, validation output dir). A live process from a
    different exchange run is therefore not safe to rebind: it would keep
    writing completion/validation evidence for the run that spawned it. The
    pair owner releases that process and reacquires a fresh pair before
    lower-level round code sees it.
    """
    pair = _acquire_pair_spawned_for_exchange_run(
        pair_registry=pair_registry,
        issue_number=issue_number,
        binding=exchange_run,
        spawn=spawn,
    )
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
    if not _pair_matches_exchange_run(pair, exchange_run):
        pair_registry.release(
            issue_number,
            reason="exchange-run-mismatch-after-recording-respawn",
        )
        raise RuntimeError(
            "respawned persistent pair does not match current exchange run: "
            f"issue={issue_number} pair_run_id={pair.exchange_run_id} "
            f"binding_run_id={exchange_run.run_id} pair_run_dir={pair.run_dir} "
            f"binding_run_dir={exchange_run.run_dir}"
        )
    respawn_errors = _pair_recording_contract_errors(pair)
    if respawn_errors:
        pair_registry.release(
            issue_number,
            reason="recording-contract-invalid-after-respawn",
        )
        raise PairRecordingContractViolation(
            issue_number=issue_number,
            errors=respawn_errors,
        )
    return pair


def emit_review_exchange_failed(
    *,
    emit: Callable[[EventName, dict[str, Any]], None],
    issue_number: int,
    session_name: str,
    exc: Exception,
) -> None:
    emit(
        EventName.REVIEW_EXCHANGE_FAILED,
        {
            "issue_number": issue_number,
            "session_name": session_name,
            "round_index": 0,
            "error": str(exc),
            "exception_type": type(exc).__name__,
        },
    )


def acquire_pair_or_emit_failure(
    *,
    pair_registry: InMemoryPersistentExchangePairRegistry,
    issue_number: int,
    session_name: str,
    exchange_run: PairExchangeRunBinding,
    spawn: Callable[[], PersistentExchangePair],
    emit: Callable[[EventName, dict[str, Any]], None],
) -> PersistentExchangePair:
    try:
        return acquire_pair_with_recording_contract(
            pair_registry=pair_registry,
            issue_number=issue_number,
            exchange_run=exchange_run,
            spawn=spawn,
        )
    except Exception as exc:
        emit_review_exchange_failed(
            emit=emit,
            issue_number=issue_number,
            session_name=session_name,
            exc=exc,
        )
        raise
