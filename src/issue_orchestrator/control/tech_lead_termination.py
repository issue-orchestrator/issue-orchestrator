"""Behaviour-complete termination of a tech-lead session (#6824 R7).

Extracted from the orchestrator facade, which coordinates rather than executes:
the effects below and — more importantly — the rule that each is attempted
INDEPENDENTLY are policy, and policy belongs beside the typed outcome it
produces rather than inside a facade method.

``kill_session`` only stops the terminal, and the one-shot driver that calls
this runs NO further tick afterwards — so a recorded cleanup fact would never be
applied. The termination is therefore self-contained, mirroring the outcomes
normal completion produces: remove the session state machine, stop the terminal,
reconcile the session out of ``active_sessions``, release BOTH coordination
holds (the per-issue claim and the repository-wide tech-lead run), and
FORCE-remove the disposable scratch worktree.

Both coordination layers, deliberately (#6994 round 2 F10/A7). The per-issue
claim says who may write to an issue; the run hold says which whole-repository
or focused tech-lead run is executing. Releasing only the first leaves every
conflicting tech-lead run blocked until the lease expires, with no later tick to
notice — so the run release is its own independent effect with its own field in
the typed outcome.

A failure of one effect never aborts the others, and the result is a typed
:class:`~.tech_lead_trigger.TechLeadTerminationOutcome` — the SOLE owner of a
failed one-shot cleanup. On a scratch-worktree removal failure the outcome
carries the exact ``leaked_worktree`` path so the caller can require explicit
operator removal; there is no second, tick-based retry mechanism to defer to.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Optional, Protocol

from ..ports.worktree_custody import (
    CustodyError,
    CustodyGrant,
    WorktreeInCustodyError,
)
from ..domain.validated_work_commands import ValidatedWorkDispositionBatch

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, Session
    from .tech_lead_trigger import TechLeadTerminationOutcome

logger = logging.getLogger(__name__)


class TechLeadTerminationHost(Protocol):
    """The facade surface a termination drives.

    Structural, so this control owner never imports the infra facade — and so a
    test can supply exactly the collaborators the effects touch.
    """

    @property
    def state(self) -> "OrchestratorState": ...

    @property
    def deps(self) -> object: ...

    def kill_session(self, name: str) -> None: ...

    def preserve_issue_work(self, issue_number: int, reason: str) -> ValidatedWorkDispositionBatch: ...


def terminate_tech_lead_session(
    host: TechLeadTerminationHost, session: "Session"
) -> "TechLeadTerminationOutcome":
    """Stop the session and clean up after it, reporting what actually worked."""
    from .tech_lead_trigger import TechLeadTerminationOutcome

    number = session.issue.number
    batch = host.preserve_issue_work(number, "tech-lead-termination")
    attempt = _effect_runner(number)
    deps = host.deps

    smm = getattr(deps, "state_machine_manager", None)
    machine_removed = attempt(
        _void(lambda: smm.remove_session_machine(session.terminal_id) if smm else None),
        "remove state machine",
    )
    terminal_stopped = attempt(
        _void(lambda: host.kill_session(session.terminal_id)), "stop terminal"
    )
    # Settled BEFORE the record goes, and only once the terminal is known to be
    # stopped: the run ended on purpose, so its claim is consumed rather than
    # left HELD with no live holder for the recovery sweep to re-admit (#7348).
    # A terminal that would not stop keeps its claim.
    work_settled = terminal_stopped and attempt(
        _void(lambda: _settle_work_claim(host, session)), "settle the work claim"
    )
    if terminal_stopped and work_settled:
        host.state.drop_active_session(session.terminal_id)  # pure in-memory owner op
    else:
        # The record is what tells the recovery sweep this run is still live.
        # Dropping it beside a terminal that may still be running, or a claim
        # still HELD, lets the next tick re-admit the run and start a second
        # one next to it (#7348 review r2). The unclean outcome reports it.
        logger.warning(
            "[TECH_LEAD] Keeping %s tracked: terminal stopped=%s, work settled=%s",
            session.terminal_id,
            terminal_stopped,
            work_settled,
        )

    claims = getattr(deps, "claim_manager", None)
    lease_id = getattr(session, "lease_id", None)
    claim_released = attempt(
        _void(
            lambda: claims.release_claim(number, lease_id)
            if (claims and lease_id)
            else None
        ),
        "release claim",
    )
    # Only a STOPPED run hands its run back (#7348 review r3). A terminal that
    # would not stop may still be running, and a peer engine cannot see our
    # local session record -- releasing the shared hold would let it start a
    # conflicting run beside the live one. The lease is the backstop. (For an
    # ownership LOSS the hold is already a peer's, so there is nothing to keep.)
    # NOT "did it raise?": the run-ownership owner reports an unreachable
    # coordination store as a typed refusal, so the verdict is its own.
    run_released = terminal_stopped and attempt(
        lambda: _release_run_hold(deps, session), "release the tech-lead run"
    )
    # The local record is closed on the same terminal, for the same reason the
    # hold is: no further tick runs after this, so a record left at RUNNING
    # would sit on the activity surface forever claiming work that stopped
    # (ADR-0033 / #6858). A run that did not stop is still RUNNING.
    if terminal_stopped:
        attempt(
            _void(lambda: _close_run_record(deps, session)), "close the run record"
        )

    worktrees = getattr(deps, "worktree_manager", None)
    disposable = bool(
        getattr(session, "scratch_worktree", False) and session.worktree_path
    )
    # A custody refusal is NOT a failed effect wearing the same clothes. It is
    # the system doing its job, and the operator needs the holder and the
    # release workflow -- not "remove it manually", which is the destruction
    # custody exists to prevent (round 9 finding 2). The removal attempt passes
    # this typed refusal THROUGH rather than reducing it to False -- wrapping
    # `attempt` did not work, because `attempt` catches Exception itself, so the
    # handler below was unreachable and round 9's fix never ran (round 12
    # finding 1).
    retained_custody: "CustodyGrant | None" = None
    custody_unavailable: str | None = None
    try:
        removal = _void(
            lambda: worktrees.remove_checkout_and_branch(
                session.worktree_path,
                force=True,
            )
            if (disposable and worktrees)
            else None
        )
        removal_attempt = _effect_runner(
            session.issue.number, propagate=(CustodyError,)
        )
        worktree_removed = removal_attempt(removal, "remove scratch worktree")
    except WorktreeInCustodyError as refusal:
        retained_custody = refusal.grant
        worktree_removed = False
        logger.info(
            "[TECH_LEAD] Scratch worktree for issue #%d is held by %s; "
            "termination left it in place",
            session.issue.number,
            refusal.grant.holder,
        )
    except CustodyError as refusal:
        # NOT a known grant and NOT an unprotected leak. The checkout was kept
        # precisely because the system could not say whether anyone holds it,
        # and "remove it manually" is the one instruction that must not follow
        # from that (round 15 finding 1).
        custody_unavailable = str(refusal)
        worktree_removed = False
        logger.warning(
            "[TECH_LEAD] Scratch worktree for issue #%d was retained because "
            "custody could not be determined: %s",
            session.issue.number,
            refusal,
        )
    return TechLeadTerminationOutcome(
        validated_work=batch,
        terminal_stopped=terminal_stopped,
        machine_removed=machine_removed,
        claim_released=claim_released,
        run_released=run_released,
        work_settled=work_settled,
        worktree_removed=worktree_removed,
        # A failed removal surfaces the EXACT leaked path for explicit operator
        # action before exit — this is the single cleanup-failure owner.
        leaked_worktree=(
            str(session.worktree_path)
            if (
                disposable
                and not worktree_removed
                and retained_custody is None
                and custody_unavailable is None
            )
            else None
        ),
        retained_custody=retained_custody,
        custody_unavailable=custody_unavailable,
    )


def _settle_work_claim(host: TechLeadTerminationHost, session: "Session") -> None:
    """Consume the pending-work claim the stopped session holds, if any."""
    from .in_flight_work import InFlightWorkLedger, SettlementOutcome

    claims = host.deps.pending_work_claims  # type: ignore[attr-defined]
    InFlightWorkLedger(host.state, claims).settle(session, SettlementOutcome.CONSUMED)


def _release_run_hold(deps: object, session: "Session") -> bool:
    """Hand the terminated session's repository-wide run hold back.

    The scope is derived from the SESSION's own launch stamp, so a global review
    releases ``global:*`` and a focused investigation releases ``issue:N`` — the
    same identity the launch authority took. A session carrying no stamp holds
    no run, and that is a genuine success rather than a skipped effect.

    A session that DOES hold a run with no ownership owner wired is a
    composition error, and it fails loudly: reporting it as a clean release
    would claim the repository-wide hold is gone when nothing ever looked.
    """
    from .tech_lead_run_admission import scope_of_session

    scope = scope_of_session(session)
    if scope is None:
        return True
    ownership = getattr(deps, "run_ownership", None)
    if ownership is None:
        raise RuntimeError(
            f"no tech-lead run ownership is wired, so run {scope.run_key} cannot"
            " be handed back"
        )
    return ownership.end_run(scope.run_key).released


def _close_run_record(deps: object, session: "Session") -> None:
    """Mark the terminated session's local run record withdrawn.

    The activity owner is a REQUIRED composition dependency (#6858 round 1 A2),
    so a missing one is a wiring error and fails loudly here — the same rule the
    run hold above follows. Reporting a clean withdrawal when nothing recorded it
    is how the activity surface ends up claiming stopped work is still running.
    The effect runner around this call already isolates the failure so one broken
    receipt cannot abort the rest of the cleanup.
    """
    activity = getattr(deps, "tech_lead_run_activity", None)
    if activity is None:
        raise RuntimeError(
            "no tech-lead run activity owner is wired, so the terminated run's"
            " local record cannot be closed"
        )
    activity.note_withdrawn(session)


def _void(effect: Callable[[], object]) -> Callable[[], Optional[bool]]:
    """An effect that can only signal failure by RAISING.

    Its return value is discarded explicitly, so a collaborator that happens to
    return something falsy can never be misread as a failed effect.
    """

    def run() -> Optional[bool]:
        effect()
        return None

    return run


def _effect_runner(
    issue_number: int,
    *,
    propagate: tuple[type[Exception], ...] = (),
) -> Callable[[Callable[[], Optional[bool]], str], bool]:
    """Attempt one effect, reporting success without letting it stop the rest.

    An effect that can fail WITHOUT raising returns its own boolean verdict, and
    that verdict is reported verbatim; an effect wrapped in :func:`_void`
    returns ``None`` and succeeds by not raising. Both shapes are needed because
    the two coordination layers report failure differently — the run ledger
    returns a typed refusal rather than raising (#6994 round 3 F12).

    Exceptions in ``propagate`` are typed OUTCOMES owned by the caller, not
    generic effect failures, so they keep their identity instead of collapsing
    to False (round 12 finding 1).
    """

    def attempt(effect: Callable[[], Optional[bool]], what: str) -> bool:
        try:
            verdict = effect()
            return True if verdict is None else verdict
        except propagate:
            raise
        except Exception:
            logger.warning(
                "[TECH_LEAD] Failed to %s for issue #%d on timeout terminate",
                what,
                issue_number,
                exc_info=True,
            )
            return False

    return attempt


__all__ = ["TechLeadTerminationHost", "terminate_tech_lead_session"]
