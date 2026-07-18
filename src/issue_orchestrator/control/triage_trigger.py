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

    def pause(self) -> None: ...

    def tick(self) -> bool: ...


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
    """Tick until the launched session leaves ``active_sessions`` or times out.

    The planner is already paused, so ``tick()`` starts no new work — it only
    drives the active investigation and drains background completion jobs. The
    session is matched by its stable :class:`SessionKey` identity (``triage:N``)
    rather than by raw issue number, so a restored session with the same slot
    still counts as the one we launched.
    """
    deadline = now() + timeout_s
    while _session_active(orchestrator, identity):
        if now() >= deadline:
            logger.warning(
                "[TRIAGE_TRIGGER] issue #%d investigation still active after %.0fs;"
                " giving up the drive",
                issue_number,
                timeout_s,
            )
            return InvestigationResult(
                issue_number, launched=True, completed=False,
                detail=(
                    f"investigation timed out after {timeout_s:.0f}s"
                    " (session still active)"
                ),
            )
        orchestrator.tick()
        sleep(poll_interval)
    logger.info(
        "[TRIAGE_TRIGGER] issue #%d investigation completed", issue_number
    )
    return InvestigationResult(
        issue_number, launched=True, completed=True,
        detail=f"investigation completed for issue #{issue_number}",
    )


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
