"""Review-cache boundary facts for completion processing."""

from __future__ import annotations

import logging

from ..domain.session_run import SessionRunAssets
from ..ports.session_output import SessionOutput

logger = logging.getLogger(__name__)


def review_cache_boundary_started_at(
    *,
    session_output: SessionOutput,
    run_assets: SessionRunAssets,
) -> str | None:
    manifest = session_output.read_manifest(run_assets.run_dir) or {}
    if not manifest.get("reset_from_scratch"):
        return None
    boundary = manifest.get("review_cache_boundary_started_at") or manifest.get("started_at")
    if not isinstance(boundary, str) or not boundary:
        return None
    logger.info(
        "[REVIEW_EXCHANGE] Scratch reset review-cache boundary active: "
        "session=%s run_dir=%s boundary=%s",
        run_assets.session_name,
        run_assets.run_dir,
        boundary,
    )
    return boundary
