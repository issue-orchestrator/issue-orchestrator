"""On-demand triage dispatch — the tech lead, aimed by hand (ADR-0031).

The reactive triage triggers all *discover* the issue to investigate from the
board: the stuck-sweep backstop (:mod:`.stuck_sweep`), the per-failure reaction
model (:mod:`.triage_reaction`), and the periodic / problem-storm health
review. This owner is their manual counterpart — an operator names one or more
specific issues and the tech lead (a ``failure_investigation``) is dispatched
at each, on demand.

It reuses the REAL triage-launch machinery
(``orchestrator.launch_triage_session``) so evidence-map staging, authority
recording, and agent sandboxing are byte-for-byte identical to a reactive
launch: this module builds the same :class:`PendingTriageReview` a discovered
failure would and hands it to the same facade method. It then drives the
launched session to completion with the planner PAUSED, so no OTHER board work
starts while the investigation runs — a paused planner returns an empty plan,
yet ``tick()`` still drives already-active sessions to completion and drains
background completion jobs. Completion is applied under ``triage.authority``
exactly as usual, so ``propose``-gated actions stay gated (an ``--advise-only``
run pins every dial to ``propose``).

Boundaries kept deliberately narrow:

* **Owner only.** No CLI / argparse here — the entrypoint injects the clock and
  sleep and prints the results.
* **Injected time.** ``now`` / ``sleep`` are passed in so the drive loop is
  deterministic under test; this owner never touches ``time`` directly.
* **Reuse, don't reimplement.** The failure fact is modelled on
  :func:`.stuck_sweep._recovered_failure` (``timed_out`` reason so the reaction
  model always INVESTIGATES a leaf issue); the launch goes through the one
  facade method the planner already uses.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..domain.models import DiscoveredFailure, PendingTriageReview, SessionStatus
from ..domain.triage_session import TriageSessionFlavor

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, Session
    from ..ports import Issue, RepositoryHost

logger = logging.getLogger(__name__)

# Fallback blocking-label context when the operator aims at an issue carrying no
# ``blocked*`` label (e.g. a plain open issue they want the tech lead to look
# at). It rides along in ``DiscoveredFailure.blocking_label`` for evidence-map
# context only; ``failure_reason`` is always ``timed_out`` so the reaction model
# investigates regardless of the label.
MANUAL_TRIAGE_LABEL = "manual-triage"
_BLOCKED_LABEL_PREFIX = "blocked"


class TriageDispatchHost(Protocol):
    """The orchestrator-facade surface an on-demand dispatch drives.

    Named structurally rather than importing the concrete ``Orchestrator`` so
    this control-layer owner stays decoupled from the infra facade and a test
    can supply a lightweight fake. ``launch_triage_session`` is the same public
    facade method the reactive planner path uses to launch a triage session —
    reusing it verbatim is the whole point (identical evidence-map staging +
    authority recording).
    """

    @property
    def repository_host(self) -> "RepositoryHost": ...

    @property
    def state(self) -> "OrchestratorState": ...

    def launch_triage_session(
        self, triage: "PendingTriageReview"
    ) -> "Session | None": ...

    def ensure_health_review_anchor(self) -> "PendingTriageReview | None": ...

    def pause(self) -> None: ...

    def tick(self) -> bool: ...

    def terminate_triage_session(
        self, session: "Session"
    ) -> "TriageTerminationOutcome": ...


@dataclass(frozen=True)
class TriageTerminationOutcome:
    """Per-effect result of terminating a triage session (#6824 R7).

    Each field is True when its effect SUCCEEDED or was not needed, and False
    when it was attempted and FAILED. ``clean`` is False when any effect failed —
    so a caller never reports a leak-free termination after, say, the disposable
    scratch worktree failed to remove (the intent is retained for a retry, but
    this run did not succeed).
    """

    terminal_stopped: bool = True
    machine_removed: bool = True
    claim_released: bool = True
    worktree_removed: bool = True
    # Path of the disposable scratch worktree that could NOT be removed (a leak
    # requiring explicit operator action); None when there was nothing to remove
    # or removal succeeded.
    leaked_worktree: str | None = None

    @property
    def clean(self) -> bool:
        return (
            self.terminal_stopped
            and self.machine_removed
            and self.claim_released
            and self.worktree_removed
        )

    def failures(self) -> tuple[str, ...]:
        """Human-readable names of the effects that failed (for the result detail)."""
        names = {
            "terminal_stopped": "terminal stop",
            "machine_removed": "state-machine removal",
            "claim_released": "claim release",
            "worktree_removed": "scratch-worktree removal",
        }
        return tuple(
            label for attr, label in names.items() if not getattr(self, attr)
        )


@dataclass(frozen=True)
class InvestigationResult:
    """The outcome of one on-demand tech-lead dispatch.

    ``launched`` is whether a triage session actually started (False when the
    issue was not found or the launch path declined). ``completed`` is whether
    the launched session left ``active_sessions`` before the timeout — a
    ``launched`` but not ``completed`` result means the drive loop hit
    ``timeout_s`` with the session still running.
    """

    issue_number: int
    launched: bool
    completed: bool
    detail: str
    # The structured termination outcome when a launched session TIMED OUT (None
    # when it completed or never launched); the command surfaces its cleanliness
    # rather than always printing "session terminated" (#6824 R7).
    termination: "TriageTerminationOutcome | None" = None


def run_targeted_investigations(
    orchestrator: TriageDispatchHost,
    issue_numbers: Sequence[int],
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_interval: float = 3.0,
    timeout_s: float = 1800.0,
) -> list[InvestigationResult]:
    """Dispatch the tech lead at each issue and drive it to completion.

    Pauses the planner ONCE up front so no other board work launches while the
    targeted investigations run, then processes each issue in order: look it up,
    build the same failure-investigation :class:`PendingTriageReview` a
    discovered failure would, launch it through the real facade path, and tick
    until the session leaves ``active_sessions`` or ``timeout_s`` elapses.

    Returns one :class:`InvestigationResult` per requested issue, in order.
    """
    orchestrator.pause()
    logger.info(
        "[TRIAGE_TRIGGER] planner paused for %d on-demand investigation(s) (#6823)",
        len(issue_numbers),
    )
    return [
        _investigate_one(
            orchestrator,
            issue_number,
            now=now,
            sleep=sleep,
            poll_interval=poll_interval,
            timeout_s=timeout_s,
        )
        for issue_number in issue_numbers
    ]


@dataclass(frozen=True)
class HealthReviewResult:
    """The outcome of an on-demand whole-board health review.

    ``anchor_issue_number`` is the health-review anchor that was launched, or
    ``None`` when none could be prepared (e.g. no triage agent configured, or
    anchor creation failed). ``launched``/``completed`` mirror
    :class:`InvestigationResult`: ``launched`` but not ``completed`` means the
    drive loop hit ``timeout_s`` with the session still running.
    """

    anchor_issue_number: int | None
    launched: bool
    completed: bool
    detail: str
    # Structured termination outcome on a launched-but-timed-out review (#6824 R7).
    termination: "TriageTerminationOutcome | None" = None


def run_health_review(
    orchestrator: TriageDispatchHost,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_interval: float = 3.0,
    timeout_s: float = 1800.0,
) -> HealthReviewResult:
    """Force a whole-board health review NOW and drive it to completion.

    The on-demand counterpart of the timer-based periodic review (ADR-0031 §4).
    Like :func:`run_targeted_investigations` it pauses the planner up front so no
    other board work starts, then reuses the anchor lifecycle: an already-open
    anchor is reused, otherwise one is created + queued through the SAME owner
    the timer path uses (``ensure_health_review_anchor`` bypasses only the
    interval/fingerprint debounce, never the fingerprint recording). The queued
    anchor is launched through the real triage-launch path and ticked to
    completion, exactly as a targeted investigation is.
    """
    orchestrator.pause()
    logger.info("[TRIAGE_TRIGGER] planner paused for an on-demand health review")
    # The anchor decision (board fingerprint + interval) is sourced on the
    # WALL clock by the facade, matching the timer path (fact_gatherer uses
    # ``time.time()``); the injected ``now`` here is the monotonic drive-loop
    # clock, used only to bound the wait below.
    triage = orchestrator.ensure_health_review_anchor()
    if triage is None:
        return HealthReviewResult(
            None, launched=False, completed=False,
            detail=(
                "no health-review anchor could be prepared (no triage agent"
                " configured, or anchor creation failed)"
            ),
        )
    session = orchestrator.launch_triage_session(triage)
    if session is None:
        logger.warning(
            "[TRIAGE_TRIGGER] health-review launch declined for anchor #%d",
            triage.issue_number,
        )
        return HealthReviewResult(
            triage.issue_number, launched=False, completed=False,
            detail=f"health-review launch failed for anchor #{triage.issue_number}",
        )
    identity = _session_identity(session)
    logger.info(
        "[TRIAGE_TRIGGER] dispatched health review at anchor #%d (session %s)",
        triage.issue_number,
        identity,
    )
    termination = _drive_session_to_completion(
        orchestrator,
        identity,
        now=now,
        sleep=sleep,
        poll_interval=poll_interval,
        timeout_s=timeout_s,
    )
    if termination is not None:
        return HealthReviewResult(
            triage.issue_number, launched=True, completed=False,
            detail=_timeout_detail("health review", timeout_s, termination),
            termination=termination,
        )
    logger.info(
        "[TRIAGE_TRIGGER] on-demand health review completed (anchor #%d)",
        triage.issue_number,
    )
    return HealthReviewResult(
        triage.issue_number, launched=True, completed=True,
        detail=f"health review completed for anchor #{triage.issue_number}",
    )


def _investigate_one(
    orchestrator: TriageDispatchHost,
    issue_number: int,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_interval: float,
    timeout_s: float,
) -> InvestigationResult:
    """Look up, launch, and drive a single on-demand investigation."""
    issue = orchestrator.repository_host.get_issue(issue_number)
    if issue is None:
        logger.warning(
            "[TRIAGE_TRIGGER] issue #%d not found; skipping investigation",
            issue_number,
        )
        return InvestigationResult(
            issue_number, launched=False, completed=False,
            detail=f"issue #{issue_number} not found",
        )

    triage = PendingTriageReview(
        issue_number=issue_number,
        title=issue.title,
        flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
        failure=_focus_failure(issue, _blocking_label(issue), now()),
    )
    session = orchestrator.launch_triage_session(triage)
    if session is None:
        logger.warning(
            "[TRIAGE_TRIGGER] triage launch declined for issue #%d", issue_number
        )
        return InvestigationResult(
            issue_number, launched=False, completed=False,
            detail=f"triage launch failed for issue #{issue_number}",
        )

    identity = _session_identity(session)
    logger.info(
        "[TRIAGE_TRIGGER] dispatched tech lead at issue #%d (session %s)",
        issue_number,
        identity,
    )
    return _drive_to_completion(
        orchestrator,
        issue_number,
        identity,
        now=now,
        sleep=sleep,
        poll_interval=poll_interval,
        timeout_s=timeout_s,
    )


def _drive_to_completion(
    orchestrator: TriageDispatchHost,
    issue_number: int,
    identity: str,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_interval: float,
    timeout_s: float,
) -> InvestigationResult:
    """Drive a launched failure investigation to completion and label the result."""
    termination = _drive_session_to_completion(
        orchestrator,
        identity,
        now=now,
        sleep=sleep,
        poll_interval=poll_interval,
        timeout_s=timeout_s,
    )
    if termination is not None:
        return InvestigationResult(
            issue_number, launched=True, completed=False,
            detail=_timeout_detail("investigation", timeout_s, termination),
            termination=termination,
        )
    logger.info(
        "[TRIAGE_TRIGGER] issue #%d investigation completed", issue_number
    )
    return InvestigationResult(
        issue_number, launched=True, completed=True,
        detail=f"investigation completed for issue #{issue_number}",
    )


def _timeout_detail(
    what: str, timeout_s: float, termination: "TriageTerminationOutcome"
) -> str:
    """Timeout result detail that is HONEST about a failed cleanup (#6824 R7).

    A clean termination reports "session terminated"; if any effect failed the
    detail says so, so a caller never reports a leak-free outcome after, e.g.,
    the disposable scratch worktree failed to remove.
    """
    detail = f"{what} timed out after {timeout_s:.0f}s (session terminated"
    if not termination.clean:
        detail += f"; cleanup INCOMPLETE — {', '.join(termination.failures())} failed"
    return detail + ")"


def _drive_session_to_completion(
    orchestrator: TriageDispatchHost,
    identity: str,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    poll_interval: float,
    timeout_s: float,
) -> "TriageTerminationOutcome | None":
    """Tick until the launched session leaves ``active_sessions`` or times out.

    Shared drive loop for every on-demand triage flavor (targeted failure
    investigations and the whole-board health review). The planner is already
    paused, so ``tick()`` starts no new work — it only drives the active session
    and drains background completion jobs. The session is matched by its stable
    :class:`SessionKey` identity (``triage:N``) rather than by raw issue number,
    so a restored session with the same slot still counts as the one we
    launched. Returns None when the session drained (completed); on timeout it
    terminates the session and returns the :class:`TriageTerminationOutcome`.
    """
    deadline = now() + timeout_s
    while _session_active(orchestrator, identity):
        if now() >= deadline:
            logger.warning(
                "[TRIAGE_TRIGGER] session %s still active after %.0fs;"
                " terminating it and giving up the drive",
                identity,
                timeout_s,
            )
            return _terminate_session(orchestrator, identity)
        orchestrator.tick()
        sleep(poll_interval)
    return None


def _terminate_session(
    orchestrator: TriageDispatchHost, identity: str
) -> "TriageTerminationOutcome":
    """Terminate the timed-out session so ownership of the timeout is EXPLICIT.

    Routes through the reconciling ``terminate_triage_session`` owner (#6824
    F7/R7), which returns a typed outcome — so the drive loop's
    ``_session_active`` check is immediately false, a multi-issue batch does not
    co-drive a dead session, and the caller learns whether cleanup was clean.
    Iterates a COPY because the reconciling terminate mutates ``active_sessions``.
    """
    outcome: "TriageTerminationOutcome | None" = None
    for session in list(orchestrator.state.active_sessions):
        if _session_identity(session) == identity:
            outcome = orchestrator.terminate_triage_session(session)
    return outcome if outcome is not None else TriageTerminationOutcome()


def _session_active(orchestrator: TriageDispatchHost, identity: str) -> bool:
    """True while a session with the launched slot identity is still active."""
    return any(
        _session_identity(session) == identity
        for session in orchestrator.state.active_sessions
    )


def _session_identity(session: "Session") -> str:
    """Stable slot identity for the launched triage session.

    ``SessionKey.stable_id()`` is the domain identity (``triage:<issue>``) — it
    is value-based and survives session restore, so it is preferred over a raw
    issue-number match that could not distinguish a triage slot from any other
    session on the same issue.
    """
    return session.key.stable_id()


def _blocking_label(issue: "Issue") -> str:
    """The issue's first ``blocked*`` label, else the manual-triage fallback."""
    for name in issue.labels:
        if name.casefold().startswith(_BLOCKED_LABEL_PREFIX):
            return name
    return MANUAL_TRIAGE_LABEL


def _focus_failure(
    issue: "Issue", blocking_label: str, observed_at: float
) -> DiscoveredFailure:
    """Build the focus failure fact fed to the triage launch.

    Modelled on :func:`.stuck_sweep._recovered_failure`: ``failure_reason`` is
    ``timed_out`` (never ``blocked``) so the reaction model always INVESTIGATES
    rather than treating a leaf issue as healthy waiting. The issue's real
    terminal label rides along in ``blocking_label`` for evidence-map context.
    """
    return DiscoveredFailure(
        issue_number=issue.number,
        issue_title=issue.title,
        failure_reason=SessionStatus.TIMED_OUT.value,
        blocking_label=blocking_label,
        issue_body=issue.body or "",
        issue_milestone=issue.milestone,
        observed_at=observed_at,
        artifact_hints=(),
    )
