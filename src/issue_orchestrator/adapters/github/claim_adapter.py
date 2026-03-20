"""GitHub adapter for issue claim management.

This module implements the ClaimManager protocol using GitHub issue comments
as the storage mechanism for distributed coordination between orchestrators.
"""

import logging
import random
import time
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol as TypingProtocol

from ...domain.claim import Claim, ClaimFetchError, ClaimResult, ClaimState
from ...domain.lease_config import LeaseConfig
from ...infra import gh_audit
from ...ports.claim_manager import ClaimManager
from .claim_parser import extract_all_claims, format_claim_comment

if TYPE_CHECKING:
    from .http_client import GitHubHttpClient
    from ...ports.event_sink import EventSink

logger = logging.getLogger(__name__)


class GitHubClaimAdapter(ClaimManager):
    """GitHub-based implementation of the ClaimManager protocol.

    Claims are stored as comments on GitHub issues using a YAML format.
    Multiple orchestrators coordinate by reading each other's claims and
    using a convergence protocol to determine the winner.

    Tie-breaking rules:
    1. Higher priority (epoch milliseconds) wins
    2. If equal priority, lexicographically larger lease_id wins
    """

    def __init__(
        self,
        client: "GitHubHttpClient",
        claimant_id: str,
        config: LeaseConfig | None = None,
        events: "EventSink | None" = None,
        label_adapter: "LabelSetProtocol | None" = None,
        io_claimed_label: str = "io:claimed",
    ):
        """Initialize the claim adapter.

        Args:
            client: GitHub HTTP client for API calls.
            claimant_id: Unique identifier for this orchestrator instance.
            config: Lease configuration. Defaults to LeaseConfig() if None.
            events: Event sink for publishing claim events. Optional.
            label_adapter: LabelSet adapter for managing claim labels. Optional.
        """
        self._client = client
        self._claimant_id = claimant_id
        self._config = config or LeaseConfig()
        self._events = events
        self._labels = label_adapter
        self._io_claimed_label = io_claimed_label

    def attempt_claim(self, issue_number: int) -> ClaimResult:
        """Attempt to claim an issue by posting a claim comment.

        Creates a new claim with the current timestamp and posts it as a
        comment on the issue. Also adds the io:claimed label.

        Args:
            issue_number: The GitHub issue number to claim.

        Returns:
            ClaimResult with success=True and lease_id if comment was posted.
        """
        now = datetime.now()
        priority = int(now.timestamp() * 1000)  # Epoch milliseconds
        lease_id = f"{uuid.uuid4().hex[:12]}-{priority}"

        claim = Claim(
            lease_id=lease_id,
            claimant=self._claimant_id,
            issue_number=issue_number,
            started_at=now,
            expires_at=now + timedelta(seconds=self._config.lease_seconds),
            priority=priority,
        )

        try:
            # Post claim comment
            comment_body = format_claim_comment(claim)
            with gh_audit.context(
                reason=gh_audit.AuditReason.GH_WRITE,
                issue_key=str(issue_number),
                scope=gh_audit.AuditScope.UNKNOWN,
            ):
                self._client.add_comment(issue_number, comment_body)

            # Add claim label
            if self._labels:
                self._labels.add_label(issue_number, self._io_claimed_label)

            self._emit_event("CLAIM_ATTEMPTED", {
                "issue_number": issue_number,
                "lease_id": lease_id,
                "claimant": self._claimant_id,
                "priority": priority,
            })

            logger.info(
                "Posted claim for issue #%d: lease_id=%s, claimant=%s",
                issue_number,
                lease_id,
                self._claimant_id,
            )

            return ClaimResult(
                success=True,
                lease_id=lease_id,
                state=ClaimState.CLAIMING,  # Not yet converged
            )

        except Exception as e:
            logger.error("Failed to post claim for issue #%d: %s", issue_number, e)
            return ClaimResult.failed(str(e))

    def run_convergence(self, issue_number: int, lease_id: str) -> bool:
        """Run convergence protocol to confirm claim ownership.

        Polls the issue comments repeatedly until we see ourselves as the
        winner for N consecutive reads.

        Args:
            issue_number: The GitHub issue number.
            lease_id: Our claim's lease_id.

        Returns:
            True if we won the convergence, False otherwise.
        """
        consecutive_wins = 0
        deadline = time.monotonic() + self._config.convergence_timeout_seconds
        max_polls = self._config.convergence_max_polls
        poll_count = 0

        logger.debug(
            "Starting convergence for issue #%d, lease_id=%s, timeout=%.1fs, max_polls=%d",
            issue_number,
            lease_id,
            self._config.convergence_timeout_seconds,
            max_polls,
        )

        while time.monotonic() < deadline and poll_count < max_polls:
            poll_count += 1

            try:
                claims = self._fetch_all_claims(issue_number, use_cache=False)
            except ClaimFetchError:
                logger.warning(
                    "Issue #%d: API error during convergence poll %d - "
                    "resetting consecutive wins",
                    issue_number,
                    poll_count,
                )
                consecutive_wins = 0
                # Sleep and retry
                jitter_ms = random.randint(
                    self._config.convergence_poll_min_ms,
                    self._config.convergence_poll_max_ms,
                )
                time.sleep(jitter_ms / 1000)
                continue

            winner = self._determine_winner(claims)

            if winner and winner.lease_id == lease_id:
                consecutive_wins += 1
                logger.debug(
                    "Issue #%d: we are winner (%d/%d consecutive)",
                    issue_number,
                    consecutive_wins,
                    self._config.convergence_required_wins,
                )

                if consecutive_wins >= self._config.convergence_required_wins:
                    self._emit_event("CLAIM_CONVERGED", {
                        "issue_number": issue_number,
                        "lease_id": lease_id,
                    })
                    logger.info(
                        "Convergence succeeded for issue #%d, lease_id=%s",
                        issue_number,
                        lease_id,
                    )
                    return True
            else:
                if consecutive_wins > 0:
                    logger.debug(
                        "Issue #%d: lost winner status (winner=%s)",
                        issue_number,
                        winner.lease_id if winner else "none",
                    )
                consecutive_wins = 0

                if winner and winner.lease_id != lease_id:
                    self._emit_event("CLAIM_CONTESTED", {
                        "issue_number": issue_number,
                        "our_lease_id": lease_id,
                        "winner_lease_id": winner.lease_id,
                        "competing_claims": len(claims),
                    })

            # Sleep with jitter
            jitter_ms = random.randint(
                self._config.convergence_poll_min_ms,
                self._config.convergence_poll_max_ms,
            )
            time.sleep(jitter_ms / 1000)

        reason = "max polls reached" if poll_count >= max_polls else "timeout"
        logger.warning(
            "Convergence failed for issue #%d, lease_id=%s (%s after %d polls)",
            issue_number,
            lease_id,
            reason,
            poll_count,
        )
        return False

    def renew_claim(self, issue_number: int, lease_id: str) -> bool:
        """Renew an existing claim by posting an updated comment.

        Args:
            issue_number: The GitHub issue number.
            lease_id: The lease_id of the claim to renew.

        Returns:
            True if renewal succeeded.
        """
        # Verify we're still the winner before renewing.
        # ClaimFetchError propagates to caller so they can distinguish
        # "genuinely lost" from "API unavailable".
        current_claim = self.get_current_claim(issue_number)

        if not current_claim or current_claim.lease_id != lease_id:
            logger.warning(
                "Cannot renew claim for issue #%d: not the current winner",
                issue_number,
            )
            return False

        # Create renewed claim with same lease_id but new expiry
        now = datetime.now()
        renewed_claim = Claim(
            lease_id=lease_id,
            claimant=self._claimant_id,
            issue_number=issue_number,
            started_at=current_claim.started_at,
            expires_at=now + timedelta(seconds=self._config.lease_seconds),
            priority=current_claim.priority,
        )

        try:
            comment_body = format_claim_comment(renewed_claim)
            with gh_audit.context(
                reason=gh_audit.AuditReason.GH_WRITE,
                issue_key=str(issue_number),
                scope=gh_audit.AuditScope.UNKNOWN,
            ):
                self._client.add_comment(issue_number, comment_body)

            self._emit_event("CLAIM_RENEWED", {
                "issue_number": issue_number,
                "lease_id": lease_id,
            })

            logger.info("Renewed claim for issue #%d, lease_id=%s", issue_number, lease_id)
            return True

        except Exception as e:
            logger.error("Failed to renew claim for issue #%d: %s", issue_number, e)
            return False

    def release_claim(self, issue_number: int, lease_id: str) -> None:
        """Release a claim on an issue.

        Removes the io:claimed label. The claim comment is left in place
        (it will expire naturally).

        Args:
            issue_number: The GitHub issue number.
            lease_id: The lease_id of the claim to release.
        """
        try:
            # Remove claim label
            if self._labels:
                self._labels.remove_label(issue_number, self._io_claimed_label)

            self._emit_event("CLAIM_RELEASED", {
                "issue_number": issue_number,
                "lease_id": lease_id,
            })

            logger.info("Released claim for issue #%d, lease_id=%s", issue_number, lease_id)

        except Exception as e:
            logger.error("Failed to release claim for issue #%d: %s", issue_number, e)

    def check_winner(self, issue_number: int, lease_id: str) -> bool:
        """Check if we are currently the winning claimant.

        Does a single fresh read (no convergence loop).

        Args:
            issue_number: The GitHub issue number.
            lease_id: Our claim's lease_id.

        Returns:
            True if we are the current winner.

        Raises:
            ClaimFetchError: If the GitHub API call fails. Callers must
                decide their own policy: fail-open (liveness, e.g.
                LeaseRenewer) or fail-closed (writes, e.g. ClaimGate).
        """
        winner = self.get_current_claim(issue_number)
        return winner is not None and winner.lease_id == lease_id

    def get_current_claim(self, issue_number: int) -> Claim | None:
        """Get the current winning claim for an issue.

        Args:
            issue_number: The GitHub issue number.

        Returns:
            The winning Claim if one exists, None otherwise.

        Raises:
            ClaimFetchError: If the GitHub API call fails.
        """
        claims = self._fetch_all_claims(issue_number)
        return self._determine_winner(claims)

    def _fetch_all_claims(
        self, issue_number: int, *, use_cache: bool = True
    ) -> list[Claim]:
        """Fetch and parse all claims from issue comments.

        Args:
            issue_number: The GitHub issue number.
            use_cache: If True, allow ETag-based HTTP caching.
                Set to False during convergence polling to avoid
                stale 304 Not Modified responses.

        Returns:
            List of parsed Claim objects.

        Raises:
            ClaimFetchError: If the GitHub API call fails. Callers must
                handle this to avoid interpreting API failures as claim loss.
        """
        try:
            with gh_audit.context(
                reason=gh_audit.AuditReason.GH_READ,
                issue_key=str(issue_number),
                scope=gh_audit.AuditScope.UNKNOWN,
            ):
                comments = self._client.get_issue_comments(
                    issue_number, use_cache=use_cache
                )
            return extract_all_claims(comments, issue_number)
        except Exception as e:
            raise ClaimFetchError(
                f"Failed to fetch claims for issue #{issue_number}: {e}"
            ) from e

    def _determine_winner(self, claims: list[Claim]) -> Claim | None:
        """Determine the winning claim from a list.

        Tie-breaking:
        1. Only consider non-expired claims
        2. Higher priority wins
        3. If equal priority, lexicographically larger lease_id wins

        Args:
            claims: List of claims to evaluate.

        Returns:
            The winning Claim or None if no valid claims.
        """
        now = datetime.now()
        valid_claims = [c for c in claims if not c.is_expired(now)]

        if not valid_claims:
            return None

        # Sort by (priority DESC, lease_id DESC) and take first
        return max(valid_claims, key=lambda c: (c.priority, c.lease_id))

    def _emit_event(self, event_name: str, data: dict) -> None:
        """Emit an event if event sink is configured.

        Args:
            event_name: The event name (without "CLAIM_" prefix for catalog lookup).
            data: Event data dict.
        """
        if not self._events:
            return

        try:
            from ...events.catalog import EventName
            from ...ports.event_sink import TraceEvent

            # Map string name to EventName enum
            full_name = f"CLAIM_{event_name}" if not event_name.startswith("CLAIM_") else event_name
            event_enum = getattr(EventName, full_name, None)

            if event_enum:
                self._events.publish(TraceEvent(event_enum, data))
        except Exception as e:
            logger.debug("Failed to emit event %s: %s", event_name, e)


# Type alias for label adapter protocol (using Protocol for structural typing)
class LabelSetProtocol(TypingProtocol):
    """Protocol for label operations (matches LabelSet port)."""

    def add_label(self, issue_number: int, label: str) -> None:
        ...

    def remove_label(self, issue_number: int, label: str) -> None:
        ...
