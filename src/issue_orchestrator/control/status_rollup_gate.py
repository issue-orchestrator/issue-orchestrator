"""Bounded, capability-aware reader for PR status-check rollups.

The awaiting-merge reconciler needs a PR's ``statusCheckRollup`` only to
disambiguate ``mergeable_state in {unstable, blocked}`` between "checks
still running" (wait) and "a check actually failed" (rework). Reading the
rollup costs a GraphQL round-trip, and on a token that lacks the
Checks / commit-status read scope it fails the *same way every tick*.

``StatusRollupGate`` owns *whether and how* to perform that read:

- after the primary (GraphQL) rollup source is observed to lack read
  scope it suppresses further GraphQL probes repo-wide for a backoff
  window, so normal ticks stop paying that round-trip and stop logging the
  same permission error — but it STILL reads the REST check-run /
  commit-status fallback each tick, so a fallback-readable failure on this
  (or another) PR is never masked by the GraphQL backoff;
- a read whose primary source is readable again clears the denial
  (self-heal once an operator fixes the token);
- transient failures are passed through untouched (retry next tick).

The persistent backoff state lives on :class:`StatusRollupCapability`
(carried by ``OrchestratorState``) because the reconciler and this gate are
rebuilt every tick.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..ports.pull_request_tracker import StatusCheckRollupRead, StatusCheckRollupState

if TYPE_CHECKING:
    from ..domain.models import StatusRollupCapability
    from ..ports.pull_request_tracker import PRInfo
    from ..ports.repository_host import RepositoryHost

logger = logging.getLogger(__name__)

# Once the token is observed to lack rollup-read capability, suppress
# further GraphQL rollup probes (and the repeated permission warning)
# repo-wide for this long. After the window we re-probe once, in case the
# token was fixed.
STATUS_ROLLUP_PERMISSION_BACKOFF_SECONDS = 3600.0

# The status-check rollup only changes the post-approval decision when
# GitHub's `mergeable_state` is ambiguous about merge-readiness. For
# clean/dirty/behind (and terminal) PRs the classifier ignores the rollup,
# so reading it would be a wasted GraphQL round-trip (and a needless
# permission-wall exposure). Keep this in lockstep with the
# `state in ("unstable", "blocked")` branch of `classify_post_approval_state`.
_ROLLUP_DECISIVE_STATES: frozenset[str] = frozenset({"unstable", "blocked"})


def rollup_is_decisive(mergeable_state: str | None) -> bool:
    """Whether the status-check rollup actually changes the merge decision."""
    return (mergeable_state or "").strip().lower() in _ROLLUP_DECISIVE_STATES


def rollup_permission_denied_reason(
    pr_number: int, mergeable_state: str | None
) -> str:
    """Actionable escalation copy when the token cannot read check status."""
    merge_state = (mergeable_state or "").strip().lower() or "unknown"
    return (
        f"PR #{pr_number} is reviewer-approved but its merge-readiness is "
        f"'{merge_state}', which can only be resolved by reading the head-commit "
        f"status-check rollup. The configured GitHub token lacks permission to "
        f"read check status (statusCheckRollup), so the orchestrator cannot tell "
        f"'checks still running' from 'a required check failed' and has stopped "
        f"guessing. Grant the token the Checks / commit-status read scope (or "
        f"merge manually) to unblock."
    )


@dataclass(frozen=True)
class RollupResolution:
    """What the reconciler should do with a decisive PR's rollup.

    ``rollup_state`` is the value to apply to the PR before classifying
    (``None`` when not decisive, no checks, or a transient read failure —
    all PENDING-equivalent). ``permission_denied`` means the decision
    genuinely needs the rollup but the token cannot read it, so the caller
    must escalate using ``reason``.
    """

    rollup_state: StatusCheckRollupState | None
    permission_denied: bool = False
    reason: str = ""


@dataclass(frozen=True)
class StatusRollupGate:
    """Capability-bounded gateway for reading PR status-check rollups."""

    repository_host: "RepositoryHost"
    repo: str | None = None
    clock: Callable[[], float] = time.time
    backoff_seconds: float = STATUS_ROLLUP_PERMISSION_BACKOFF_SECONDS

    def resolve_decisive(
        self,
        capability: "StatusRollupCapability",
        *,
        pr: "PRInfo",
        issue_number: int,
        issue_key: str,
    ) -> RollupResolution:
        """Resolve a post-publish-eligible PR's rollup, bounding the read.

        Reads the rollup ONLY when it is decisive for the merge decision
        (``mergeable_state`` unstable/blocked); otherwise no GraphQL call is
        made. A permission denial is surfaced as an escalation signal rather
        than collapsed into a PENDING default.
        """
        if not rollup_is_decisive(pr.mergeable_state):
            return RollupResolution(rollup_state=pr.status_check_rollup)
        read = self.read(
            capability,
            pr_number=pr.number,
            issue_number=issue_number,
            issue_key=issue_key,
        )
        if read.permission_denied:
            return RollupResolution(
                rollup_state=None,
                permission_denied=True,
                reason=rollup_permission_denied_reason(pr.number, pr.mergeable_state),
            )
        # ok → real state; transient_error → None (PENDING-equivalent, so we
        # wait and retry next tick rather than rework on a bad signal).
        return RollupResolution(rollup_state=read.state)

    def read(
        self,
        capability: "StatusRollupCapability",
        *,
        pr_number: int,
        issue_number: int,
        issue_key: str,
    ) -> StatusCheckRollupRead:
        """Read a decisive PR's rollup, bounding repeated permission failures.

        Returns the typed read. A ``permission_denied`` outcome means the
        caller must surface a loud, actionable diagnostic for this PR — the
        gate only bounds the *repo-wide GraphQL probing and logging*, never
        the per-PR decision impact, and never the REST fallback that can
        still classify a failure.
        """
        now = self.clock()
        if self._suppressed(capability, now):
            # GraphQL is inside its permission-backoff window: skip re-probing
            # it (no wasted round-trip, no repeated repo-wide warning) but STILL
            # read the REST fallback sources, which can classify a now-readable
            # failure for THIS PR. The backoff state is left untouched — a
            # fallback-only read cannot prove the GraphQL source recovered, so
            # the window self-heals via the post-window re-probe below.
            return self.repository_host.read_pr_status_check_rollup(
                pr_number, skip_primary_source=True
            )

        read = self.repository_host.read_pr_status_check_rollup(pr_number)
        if read.primary_source_denied:
            # The primary (GraphQL) source lacks read scope. Back it off
            # repo-wide so future ticks skip the wasted probe and its repeated
            # warning — even when the REST fallback saved THIS read
            # (``capability == "ok"``), because re-probing GraphQL stays wasted
            # until an operator fixes the token.
            first_observation = capability.permission_denied_since is None
            capability.permission_denied_since = now
            if first_observation:
                logger.warning(
                    "status_check_rollup primary (GraphQL) source permission "
                    "denied on %s for PR #%d (issue #%d / %s): the configured "
                    "GitHub token cannot read the GraphQL statusCheckRollup. "
                    "Backing off that probe repo-wide for %.0fs; the REST "
                    "check-run / commit-status fallback is still consulted each "
                    "tick so a fallback-readable failure is not masked.",
                    self.repo or "repository",
                    pr_number,
                    issue_number,
                    issue_key,
                    self.backoff_seconds,
                )
        elif read.capability == "ok":
            # GraphQL is readable again — clear any prior denial so a fresh
            # denial starts a new backoff window.
            capability.permission_denied_since = None
        return read

    def _suppressed(self, capability: "StatusRollupCapability", now: float) -> bool:
        denied_since = capability.permission_denied_since
        if denied_since is None:
            return False
        return (now - denied_since) < self.backoff_seconds
