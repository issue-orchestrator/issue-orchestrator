"""How a tech-lead run's agent-visible launch inputs travel (#7273).

``resolve_tech_lead_launch_authority`` admits a completion against two things
created once at the ORIGINAL launch: the authority row, and the
``tech-lead-data/`` copies it reads back out of the RUN DIRECTORY to prove
nothing was edited after launch. A validation retry allocates a new run, so the
resumed run has neither, and carrying only the row fails in a way that looks
fixed -- the row loads, the assignment copy is missing, and the completion is
rejected as ``scope_tampered`` instead of ``missing_authority``. Still
pre-action, still zero push.

The inputs get their own owner rather than another branch inside the launch
policy: they are a distinct thing that has to survive a run boundary, and the
authority row already has one.
"""

from __future__ import annotations

import logging
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Generator

from ..domain.tech_lead_run_artifacts import TECH_LEAD_DATA_DIRNAME
from ..domain.tech_lead_session import TECH_LEAD_ASSIGNMENT_FILENAME

if TYPE_CHECKING:
    from ..domain.models import PendingValidationRetry
    from ..domain.session_run import SessionRunAssets, SessionRunIdentity
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .launch_transaction import SpawnGuard

logger = logging.getLogger(__name__)


def carry_tech_lead_inputs(
    retry: "PendingValidationRetry",
    source: "SessionRunIdentity",
    run: "SessionRunAssets",
) -> str | None:
    """Copy the original run's ``tech-lead-data/`` into the resumed run.

    Everything is COPIED, never re-derived: re-sampling scope from the board
    would let a run's mutation scope grow between attempts, which is exactly
    what the create-once row exists to prevent. Byte-for-byte for the same
    reason -- a regenerated assignment is a new assertion of scope, not evidence
    of the old one.

    Returns a refusal message when the original inputs are gone. Relaunching
    without them would spend an agent session on work whose completion the
    orchestrator is already guaranteed to reject.
    """
    origin = source_data_dir(retry, source)
    if not (origin / TECH_LEAD_ASSIGNMENT_FILENAME).is_file():
        return (
            f"Validation retry for issue #{retry.issue_number} names run "
            f"{source.run_id}, whose {TECH_LEAD_DATA_DIRNAME}/"
            f"{TECH_LEAD_ASSIGNMENT_FILENAME} is gone; the resumed run has no "
            "launch inputs the completion owner would trust"
        )
    destination = run.run_dir / TECH_LEAD_DATA_DIRNAME
    try:
        shutil.copytree(origin, destination, dirs_exist_ok=True)
    except OSError as exc:
        return (
            f"Validation retry for issue #{retry.issue_number} could not carry "
            f"{TECH_LEAD_DATA_DIRNAME} from run {source.run_id} to "
            f"{run.identity.run_id}: {exc}"
        )
    logger.info(
        "Validation retry for issue #%d carried %s forward: %s -> %s",
        retry.issue_number,
        TECH_LEAD_DATA_DIRNAME,
        source.run_id,
        run.identity.run_id,
    )
    return None


def source_data_dir(
    retry: "PendingValidationRetry", source: "SessionRunIdentity"
) -> Path:
    """Where the original run left its inputs, inside the retry's checkout.

    Derived rather than stored: the retry already carries the durable checkout
    and the run it came from, and a run directory is named by its own identity.
    """
    return (
        Path(retry.worktree_path)
        / ".issue-orchestrator"
        / "sessions"
        / f"{source.run_id}__{source.session_name}"
        / TECH_LEAD_DATA_DIRNAME
    )


@dataclass
class LaunchAuthorityTransfer:
    """A carry that is not finished until a terminal is actually running.

    Recording the destination row was treated as the whole transfer, which left
    two leaks pointing in opposite directions (#7273 round 2 finding 4):

    * every post-carry launch failure -- the durable claim, a label, the spawn
      itself, an exception -- left an authority row for a run that never existed
      operationally; and
    * every SUCCESS left the source row behind too, so a retried investigation
      permanently owned two.

    Both violate the store's contract that a row is discarded when its run
    terminalizes or fails to launch. So the transfer commits on the same signal
    the claim guard uses, and there is exactly one of those per launch.
    """

    store: "TechLeadAuthorityStore"
    source: "SessionRunIdentity"
    destination: "SessionRunIdentity"

    def settle(self, *, spawned: bool) -> None:
        """Discard whichever row the launch's outcome says does not survive.

        Spawned, the source is spent and the destination owns the grant. Not
        spawned, the destination names a run that never existed operationally
        and the source is still the retry's authority for the next attempt.
        """
        spent = self.source if spawned else self.destination
        self.store.discard(run_id=spent.run_id, session_name=spent.session_name)


@contextmanager
def transfer_launch_authority(
    transfer: "LaunchAuthorityTransfer | None", spawn: "SpawnGuard"
) -> Generator[None, None, None]:
    """Settle a carried authority on the way out, whichever way that is.

    It reads the CLAIM guard's own spawn decision rather than keeping a second
    one. There is exactly one irreversible moment in a launch, and two guards
    tracking it separately is how a new early return settles one and not the
    other (round 2 finding 4).

    ``None`` for a retry that inherits nothing, so the caller wraps its launch
    unconditionally rather than branching -- a new early return inside cannot
    forget to settle what it did not know was there.
    """
    try:
        yield
    finally:
        # No `return` in here: it would swallow an exception on its way out,
        # and the launch paths this wraps report failure by raising.
        if transfer is not None:
            transfer.settle(spawned=spawn.terminal_spawned)


__all__ = [
    "LaunchAuthorityTransfer",
    "carry_tech_lead_inputs",
    "source_data_dir",
    "transfer_launch_authority",
]
