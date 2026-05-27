"""Shared path grammar for review-exchange turn artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .review_artifacts import REVIEW_REPORT_FILENAME
from .review_exchange_turn import Role

REVIEW_EXCHANGE_DIR_NAME = "review-exchange"
REVIEW_EXCHANGE_TURNS_DIR_NAME = "turns"
TURN_RESULT_SUFFIX = "result.json"
TURN_RESULT_GLOB = f"round-*-*-attempt-*.{TURN_RESULT_SUFFIX}"

_TURN_RESULT_RE = re.compile(
    r"^round-(?P<round>\d+)-(?P<role>coder|reviewer)-attempt-(?P<attempt>\d+)"
    rf"\.{re.escape(TURN_RESULT_SUFFIX)}$"
)
_ROLE_ORDER = {Role.REVIEWER: 0, Role.CODER: 1}


@dataclass(frozen=True, slots=True)
class ReviewExchangeTurnResultArtifact:
    """Parsed location of one persisted review-exchange turn result."""

    round_index: int
    role: Role
    attempt_index: int
    result_path: Path

    @property
    def stem(self) -> str:
        return turn_artifact_stem(
            round_index=self.round_index,
            role=self.role,
            attempt_index=self.attempt_index,
        )

    @property
    def review_report_path(self) -> Path:
        return self.result_path.parent / f"{self.stem}.{REVIEW_REPORT_FILENAME}"

    @property
    def sort_key(self) -> tuple[int, int, int]:
        return (
            self.round_index,
            _ROLE_ORDER[self.role],
            self.attempt_index,
        )


def review_exchange_dir(run_dir: Path) -> Path:
    """Return the exchange subdirectory for a run dir."""
    return run_dir / REVIEW_EXCHANGE_DIR_NAME


def review_exchange_turns_dir(exchange_dir: Path, *, create: bool = False) -> Path:
    """Return the exchange turn-artifact directory."""
    turns_dir = exchange_dir / REVIEW_EXCHANGE_TURNS_DIR_NAME
    if create:
        turns_dir.mkdir(parents=True, exist_ok=True)
    return turns_dir


def scoped_review_exchange_turns_dir(
    *, run_dir: Path, exchange_dir: Path
) -> Path | None:
    """Return the turns dir only when it is the canonical dir under run_dir."""
    try:
        run_root = run_dir.resolve()
        turns_dir = review_exchange_turns_dir(exchange_dir).resolve()
        expected_turns_dir = review_exchange_turns_dir(
            review_exchange_dir(run_root)
        ).resolve()
        turns_dir.relative_to(run_root)
    except (OSError, ValueError):
        return None
    if turns_dir != expected_turns_dir:
        return None
    return turns_dir


def turn_artifact_stem(
    *,
    round_index: int,
    role: Role,
    attempt_index: int,
) -> str:
    """Return the stable stem for one role attempt."""
    return f"round-{round_index}-{role.value}-attempt-{attempt_index}"


def turn_artifact_path(
    exchange_dir: Path,
    *,
    round_index: int,
    role: Role,
    attempt_index: int,
    suffix: str,
    create_dir: bool = False,
) -> Path:
    """Return a persisted artifact path for one role attempt."""
    stem = turn_artifact_stem(
        round_index=round_index,
        role=role,
        attempt_index=attempt_index,
    )
    return review_exchange_turns_dir(exchange_dir, create=create_dir) / f"{stem}.{suffix}"


def turn_packet_path(exchange_dir: Path, *, round_index: int, role: Role) -> Path:
    """Return the per-round packet artifact path."""
    return review_exchange_turns_dir(exchange_dir, create=True) / (
        f"round-{round_index}-{role.value}.packet.json"
    )


def parse_turn_result_artifact(path: Path) -> ReviewExchangeTurnResultArtifact | None:
    """Parse one turn result artifact path, returning None for non-matches."""
    match = _TURN_RESULT_RE.match(path.name)
    if match is None:
        return None
    return ReviewExchangeTurnResultArtifact(
        round_index=int(match.group("round")),
        role=Role(match.group("role")),
        attempt_index=int(match.group("attempt")),
        result_path=path,
    )


def iter_scoped_turn_result_artifacts(
    *, run_dir: Path, exchange_dir: Path
) -> tuple[ReviewExchangeTurnResultArtifact, ...]:
    """Return sorted turn result artifacts from the canonical turns directory."""
    turns_dir = scoped_review_exchange_turns_dir(run_dir=run_dir, exchange_dir=exchange_dir)
    if turns_dir is None or not turns_dir.is_dir():
        return ()
    artifacts = [
        artifact
        for path in turns_dir.glob(TURN_RESULT_GLOB)
        if (artifact := parse_turn_result_artifact(path)) is not None
    ]
    return tuple(sorted(artifacts, key=lambda artifact: artifact.sort_key))
