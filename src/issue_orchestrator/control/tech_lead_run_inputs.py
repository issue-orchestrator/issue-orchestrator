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
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Generator

from ..domain.pending_work import PendingWorkClaim, PendingWorkKind
from ..domain.tech_lead_run_artifacts import TECH_LEAD_DATA_DIRNAME
from ..domain.tech_lead_session import TECH_LEAD_ASSIGNMENT_FILENAME
from ..infra.contained_artifact_copy import (
    CopyBounds,
    CopyBudget,
    close_fd,
    copy_contained_tree,
    open_contained_anchor,
)

if TYPE_CHECKING:
    from ..domain.models import PendingValidationRetry
    from ..domain.tech_lead_session import TechLeadLaunchAuthority
    from ..domain.session_run import SessionRunAssets, SessionRunIdentity
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .launch_transaction import LaunchWorkClaim, SpawnGuard

logger = logging.getLogger(__name__)

# The source is AGENT-WRITABLE, so traversal and aggregate bytes stay bounded and
# links are never followed. But these are LAUNCH INPUTS, not the agent-authored
# decision/report artifacts the archive policy covers, and borrowing that
# policy's limits was wrong (round 7 finding 1): `TechLeadDownloader` emits two
# files for each of as many as 100 PRs, on top of the assignment, manifest,
# snapshots and evidence, so 200 files refuses a LEGITIMATE batch review. A
# fetched diff is likewise not subject to the decision loader's 2 MiB parsing
# limit, and a lower per-file ceiling silently SKIPS an admissible input without
# exhausting the budget -- the retry then launches with evidence missing rather
# than being refused. The traversal bound limits the file count; the aggregate
# ceiling limits any one file.
_TECH_LEAD_INPUT_TOTAL_BYTES = 96 * 1024 * 1024
_TECH_LEAD_INPUT_SCAN_ENTRIES = 5_000
_TECH_LEAD_INPUT_COPY_BOUNDS = CopyBounds(
    files=_TECH_LEAD_INPUT_SCAN_ENTRIES,
    total_bytes=_TECH_LEAD_INPUT_TOTAL_BYTES,
    entries=_TECH_LEAD_INPUT_SCAN_ENTRIES,
    directories=250,
    depth=8,
)


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
    checkout = Path(retry.worktree_path)
    origin = source_data_dir(retry, source)
    try:
        source_run_parts = origin.parent.relative_to(checkout).parts
    except ValueError:
        return (
            f"Validation retry for issue #{retry.issue_number} names run "
            f"{source.run_id}, whose launch inputs are outside its checkout"
        )
    source_run_fd = open_contained_anchor(checkout, source_run_parts)
    if source_run_fd is None:
        return (
            f"Validation retry for issue #{retry.issue_number} names run "
            f"{source.run_id}, whose {TECH_LEAD_DATA_DIRNAME} could not be "
            "safely opened; the resumed run has no launch inputs the completion "
            "owner would trust"
        )
    budget = CopyBudget(_TECH_LEAD_INPUT_COPY_BOUNDS)
    try:
        copied = copy_contained_tree(
            source_run_fd,
            TECH_LEAD_DATA_DIRNAME,
            run.run_dir,
            cap=_TECH_LEAD_INPUT_TOTAL_BYTES,
            budget=budget,
            label=f"validation retry for issue #{retry.issue_number}",
        )
    finally:
        close_fd(source_run_fd)
    if budget.exhausted:
        return (
            f"Validation retry for issue #{retry.issue_number} could not carry "
            f"{TECH_LEAD_DATA_DIRNAME} from run {source.run_id} to "
            f"{run.identity.run_id}: {budget.exhausted_by}"
        )
    destination = run.run_dir / TECH_LEAD_DATA_DIRNAME
    if copied == 0 or not (destination / TECH_LEAD_ASSIGNMENT_FILENAME).is_file():
        return (
            f"Validation retry for issue #{retry.issue_number} names run "
            f"{source.run_id}, whose {TECH_LEAD_DATA_DIRNAME}/"
            f"{TECH_LEAD_ASSIGNMENT_FILENAME} is gone or unsafe; the resumed "
            "run has no launch inputs the completion owner would trust"
        )
    logger.info(
        "Validation retry for issue #%d carried %s forward: %s -> %s",
        retry.issue_number,
        TECH_LEAD_DATA_DIRNAME,
        source.run_id,
        run.identity.run_id,
    )
    return None


def preserved_source_run(retry: "PendingValidationRetry") -> "Path | None":
    """The run whose artifacts this retry still has to read, if any.

    Worktree preparation prunes old runs BEFORE the copy happens, so the source
    has to be named to the pruner or repeated pre-spawn refusals eventually
    delete the only trusted copy of the launch inputs (round 8 finding 2).
    """
    source = retry.authority_run
    return None if source is None else source_data_dir(retry, source).parent


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
    authority: "TechLeadLaunchAuthority"

    def begin(self) -> None:
        """Record the destination, once the durable work claim exists.

        Preparing the transfer and RECORDING it are deliberately separate. The
        row used to be written before the claim, while the guard that settles it
        started after -- so a claim the store refused returned between the two
        and left authority for a run that never existed (round 4 finding 1).
        """
        self.store.record(
            run_id=self.destination.run_id,
            session_name=self.destination.session_name,
            authority=self.authority,
        )
        logger.info(
            "Carried tech-lead launch authority forward: %s -> %s",
            self.source.run_id,
            self.destination.run_id,
        )

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
    transfer: "LaunchAuthorityTransfer | None",
    spawn: "SpawnGuard",
    *,
    work: "LaunchWorkClaim",
    run: "SessionRunAssets",
    retry: "PendingValidationRetry",
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
    began = False
    try:
        if transfer is not None:
            transfer.begin()
            began = True
        yield
    finally:
        # No `return` in here: it would swallow an exception on its way out,
        # and the launch paths this wraps report failure by raising.
        # `began` guards the case where begin() itself RAISED: the destination
        # already holds a different create-once authority, owned by another
        # launch. Settling then would delete a row this transfer never took
        # (round 12 finding 3).
        if transfer is not None and began:
            if spawn.terminal_spawned:
                # The SOURCE authority is about to be retired, so the durable
                # work this live terminal carries must name the destination
                # that survives. Otherwise a provider outage requeues a claim
                # pointing at a deleted row and every later relaunch is refused
                # as `missing_authority` (round 11 finding 1).
                #
                # Before `settle`, deliberately: if the ledger refuses, BOTH
                # authority rows are still there and restart recovery can use
                # the source claim safely.
                work.rebind_held_claim(
                    run,
                    PendingWorkClaim(
                        PendingWorkKind.VALIDATION_RETRY,
                        replace(retry, authority_run=transfer.destination),
                    ),
                )
            transfer.settle(spawned=spawn.terminal_spawned)


__all__ = [
    "LaunchAuthorityTransfer",
    "carry_tech_lead_inputs",
    "preserved_source_run",
    "source_data_dir",
    "transfer_launch_authority",
]
