"""Correctable agent-side contract check, sharing the authoritative loader.

This is feedback, not an authority grant: the orchestrator still checks its
trusted launch record and validates the pair before executing any decision.
Adapted from the stranded #7040 implementation.
"""
from __future__ import annotations

from ..domain.session_run import SessionRunAssets
from ..domain.tech_lead_session import TECH_LEAD_ASSIGNMENT_FILENAME
from .tech_lead_decision_loader import (
    TechLeadArtifactLoadResult,
    load_tech_lead_artifact_pair_for_run,
)


def precheck_tech_lead_artifacts(
    run_assets: SessionRunAssets,
) -> TechLeadArtifactLoadResult | None:
    """Check launch-declared tech-lead artifacts while their author can repair them.

    Ordinary coding runs owe no pair. The worktree assignment identifies the
    feedback contract only; modifying it cannot bypass orchestrator validation.
    """
    assignment = run_assets.run_dir / "tech-lead-data" / TECH_LEAD_ASSIGNMENT_FILENAME
    if not assignment.is_file():
        return None
    return load_tech_lead_artifact_pair_for_run(run_assets.run_dir)
