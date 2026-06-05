"""Run artifact resolution helpers for active session lifecycle paths."""

from __future__ import annotations

import logging
from pathlib import Path

from ..domain.models import Session
from ..domain.session_run import SessionRunAssets
from ..ports.session_output import SessionOutput

logger = logging.getLogger(__name__)


def resolve_session_run_dir(
    _session_output: SessionOutput,
    session: Session,
) -> Path:
    """Transitional read edge for callers that still need a filesystem path."""
    return resolve_session_run_assets(_session_output, session).run_dir


def resolve_session_run_assets(
    _session_output: SessionOutput,
    session: Session,
) -> SessionRunAssets:
    """Resolve the run directory for a session using artifact identity.

    Active and restored sessions carry the exact artifact directory recorded at
    launch or terminal discovery. That path is authoritative and
    provider-agnostic: terminal adapter names, phase names, and provider-
    specific artifact layouts must not be re-derived during timeout/completion
    handling.
    """
    return resolve_run_assets(
        session_name=session.terminal_id,
        recorded_run_assets=session.run_assets,
    )


def resolve_run_assets(
    *,
    session_name: str,
    recorded_run_assets: SessionRunAssets,
) -> SessionRunAssets:
    """Return the recorded session run assets.

    ``recorded_run_assets`` is authoritative even when the directory is no longer
    present. A missing recorded path means the run artifacts were pruned or
    moved; falling back to discovery could attach diagnostics to a different run.
    """
    if not recorded_run_assets.run_dir.exists():
        logger.warning(
            "[%s] Session run_dir is recorded but missing: %s",
            session_name,
            recorded_run_assets.run_dir,
        )
    return recorded_run_assets


def resolve_run_dir(
    *,
    session_name: str,
    recorded_run_assets: SessionRunAssets,
) -> Path:
    """Transitional read edge for callers that still need a filesystem path."""
    return resolve_run_assets(
        session_name=session_name,
        recorded_run_assets=recorded_run_assets,
    ).run_dir
