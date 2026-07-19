"""E2E slot / due policy — when the suite is due and how it relates to worker slots.

Factored out of :mod:`issue_orchestrator.infra.e2e_runner` so the observation
layer can gather ``e2e_due`` / ``e2e_occupies_slot`` facts and the trigger can
share one precondition path without the runner module growing unbounded.

The runner itself (``E2ERunnerManager`` and its singleton) stays in
``e2e_runner``; this module only needs the live runner *status*, which it reaches
via a function-level ``from .e2e_runner import get_e2e_runner_manager`` inside
the functions that use it. That keeps ``e2e_runner`` free to import this module
at top level (for ``maybe_trigger_e2e``) with no circular import.
"""

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from .e2e_db import E2EDB

if TYPE_CHECKING:
    from .config import Config, E2EConfig

logger = logging.getLogger(__name__)


def get_e2e_role(
    e2e_config: "E2EConfig",
    instance_id: str | None = None,
) -> str:
    """Determine the E2E role for this orchestrator instance.

    Args:
        e2e_config: E2E configuration from config file
        instance_id: Instance ID (e.g., "orchestrator-1") from INSTANCE_ID env var

    Returns:
        One of: "executor", "reader", "disabled"

    Role resolution:
    1. If role is explicitly set (not "auto"), use that
    2. Otherwise, orchestrator-1 (or single instance) is executor

    For multi-machine setups, use explicit role with env var:
        role: ${E2E_ROLE}  # Set E2E_ROLE=executor on designated machine
    """
    # Explicit role overrides auto-detection
    if e2e_config.role != "auto":
        return e2e_config.role

    # Auto mode: first instance on single-machine setup is executor
    # instance_id is None for single-instance mode, or "orchestrator-1" for first instance
    if instance_id is None or instance_id == "orchestrator-1":
        return "executor"

    return "reader"


def _get_main_head(repo_root: Path) -> Optional[str]:
    """Get the current HEAD commit SHA of the orchestrator's repo.

    Uses HEAD (not origin/main) so that e2e auto-trigger detects changes
    when the orchestrator runs from a worktree or feature branch.

    Returns:
        Commit SHA string, or None if unable to determine.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        logger.warning("Failed to get HEAD: %s", e)
    return None


def _should_skip_e2e_trigger(
    config: "Config", repo_root: Path, orchestrator_id: str, instance_id: str | None
) -> bool:
    """Check if E2E trigger should be skipped. Returns True to skip."""
    if not config.e2e.enabled or config.e2e.auto_run_interval_minutes <= 0:
        return True

    role = get_e2e_role(config.e2e, instance_id=instance_id)
    if role != "executor":
        logger.debug("E2E auto-trigger: skipping (role=%s, not executor)", role)
        return True

    # Function-level import: the runner singleton lives in e2e_runner, which
    # imports THIS module at top level. Importing here avoids a circular import
    # while still resolving (and honouring test patches of) the live symbol.
    from .e2e_runner import get_e2e_runner_manager

    manager = get_e2e_runner_manager()
    status = manager.status(orchestrator_id)
    if status["running"]:
        logger.debug("E2E auto-trigger: already running")
        return True

    return False


def _check_e2e_interval_and_head(config: "Config", repo_root: Path, orchestrator_id: str) -> bool:
    """Check if enough time passed and HEAD changed. Returns True to skip."""
    db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return False

    try:
        db = E2EDB(db_path)
        last_run = db.latest_run(orchestrator_id)
        if not last_run or not last_run.finished_at:
            return False

        from datetime import datetime, timezone
        finished = datetime.fromisoformat(last_run.finished_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        minutes_since = (now - finished).total_seconds() / 60

        if minutes_since < config.e2e.auto_run_interval_minutes:
            logger.debug(
                "E2E auto-trigger: only %.1f min since last run (need %d)",
                minutes_since, config.e2e.auto_run_interval_minutes,
            )
            return True

        current_head = _get_main_head(repo_root)
        if current_head and last_run.commit_sha == current_head:
            logger.debug(
                "E2E auto-trigger: main HEAD unchanged (%s), skipping",
                current_head[:8] if current_head else "unknown",
            )
            return True
    except Exception as e:
        logger.warning("E2E auto-trigger: failed to check last run: %s", e)
    return False


def is_e2e_due(
    config: "Config",
    repo_root: Path,
    orchestrator_id: str,
    instance_id: str | None = None,
) -> bool:
    """Whether the auto-trigger's due-conditions are ALL met (ignoring slots).

    This is exactly the pair of guards ``maybe_trigger_e2e`` applies before it
    starts a run — enabled + interval configured + executor role + not already
    running + interval elapsed + main HEAD changed — factored out so the
    observation layer can gather an ``e2e_due`` fact without duplicating (or
    drifting from) the trigger's own precondition logic. It is deliberately
    slot-agnostic: the worker-slot start-gate is applied only by the trigger.
    """
    if _should_skip_e2e_trigger(config, repo_root, orchestrator_id, instance_id):
        return False
    if _check_e2e_interval_and_head(config, repo_root, orchestrator_id):
        return False
    return True


@dataclass(frozen=True)
class E2ESlotSignals:
    """Observation of how a first-class E2E workload relates to worker slots.

    ``occupies_slot`` — an E2E run is active right now, so it holds one worker
    slot (the planner drops worker capacity by 1). ``due`` — the suite is due
    and no run is active, so the planner reserves a slot for it. At most one is
    ever True; both are False when ``e2e.occupies_session_slot`` is off, which
    the reader factory guards so the default path does zero extra work.
    """

    occupies_slot: bool = False
    due: bool = False


def make_e2e_slot_reader(
    config: "Config",
) -> Callable[[], E2ESlotSignals]:
    """Build the observation reader the fact gatherer threads into the snapshot.

    Returns a zero-arg callable so the planner learns "E2E is running / due"
    through the snapshot fact, never by reaching into the runner. Off by
    default: when ``e2e.occupies_session_slot`` is disabled the reader returns
    empty signals WITHOUT touching the runner, DB, or git, keeping the default
    scheduling path byte-for-byte unchanged.
    """
    repo_root = config.repo_root
    orchestrator_id = config.orchestrator_id

    def _read() -> E2ESlotSignals:
        if not config.e2e.occupies_session_slot:
            return E2ESlotSignals()
        instance_id = os.environ.get("INSTANCE_ID")
        # Function-level import: see _should_skip_e2e_trigger for the rationale.
        from .e2e_runner import get_e2e_runner_manager

        running = get_e2e_runner_manager().status(orchestrator_id)["running"]
        if running:
            return E2ESlotSignals(occupies_slot=True, due=False)
        due = is_e2e_due(config, repo_root, orchestrator_id, instance_id)
        return E2ESlotSignals(occupies_slot=False, due=due)

    return _read
