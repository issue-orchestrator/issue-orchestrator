"""Stack base, partial-delivery and review-authority checks before a PR is written.

Shared by every publication path: the live completion pipeline, manual
publication and retained-work recovery. A refusal is typed so a caller that
retries on its own schedule (recovery's drain) keeps the host's rate limit
behind it instead of reading an error string (#7426, #7350).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..domain.host_rate_limit import HostRateLimit, require_limit_only_on_failure
from ..domain.models import CompletionRecord
from ..domain.publication_remote import attributed_publication_body
from ..domain.runtime_identity import RuntimeIdentity
from .completion_preparation import PreparedPullRequest
from .completion_result_artifacts import build_pr_body
from .completion_review_exchange import CompletionReviewExchange
from .completion_types import ERROR_PREFIX_CREATE_PR, REVIEW_EXCHANGE_ERROR_PREFIX
from .partial_delivery_guard import PartialDeliveryGuard

if TYPE_CHECKING:
    from .stack_base import StackBaseDecision
    from .stack_publish_gate import StackBaseGate

logger = logging.getLogger(__name__)


class PublishFailedEmitter(Protocol):
    def __call__(
        self,
        *,
        issue_number: int,
        stage: str,
        error: str,
        retryable: bool | None = None,
        branch: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PullRequestPreparationRefusal:
    """The PR must not be written yet: the error lines, and any host rate limit.

    ``rate_limit`` is set when a GitHub read behind the refusal was rate
    limited, so a retrying owner waits for the reset instead of spending an
    attempt.
    """

    errors: tuple[str, ...]
    rate_limit: HostRateLimit | None = None

    def __post_init__(self) -> None:
        if not self.errors or not all(self.errors):
            raise ValueError("a PR preparation refusal names at least one error")
        require_limit_only_on_failure(
            self.rate_limit, failed=True, result="PR preparation refusal"
        )

    @property
    def message(self) -> str:
        return "; ".join(self.errors)


class PullRequestPreparation:
    def __init__(
        self,
        *,
        stack_gate: Callable[[], StackBaseGate | None],
        base_branch: Callable[[], str],
        partial_delivery: PartialDeliveryGuard,
        review_exchange: CompletionReviewExchange,
        runtime_identity: RuntimeIdentity | None,
        emit_publish_failed: PublishFailedEmitter,
    ) -> None:
        self._stack_gate = stack_gate
        self._base_branch = base_branch
        self._partial_delivery = partial_delivery
        self._review_exchange = review_exchange
        self._runtime_identity = runtime_identity
        self._emit_publish_failed = emit_publish_failed

    def prepare(
        self, *, worktree: Path, record: CompletionRecord, issue_number: int,
        issue_title: str, branch: str, agent_label: str | None,
        exchange_mode: str | None, exchange_result: Any | None,
    ) -> PreparedPullRequest | PullRequestPreparationRefusal:
        """Share stack, review authority and PR content across publication paths."""
        # Stack publish gate (ADR-0029 / #6596): for a Stack-after: successor,
        # base the PR on the predecessor branch and fail fast when the publish
        # gate is blocked. Non-stack issues keep the default base selection.
        base = self._publish_base(issue_number, worktree)
        if isinstance(base, PullRequestPreparationRefusal):
            return base
        base_branch_resolver, stack_decision = base
        # Resolve the base once: it is both the base a fresh PR is created on and
        # the base an existing PR must already target before it can be reused.
        expected_base = base_branch_resolver()

        pr_title = f"#{issue_number}: {issue_title}"
        pr_body = build_pr_body(
            record,
            issue_number,
            runtime_identity=self._runtime_identity,
        )
        pr_body = attributed_publication_body(pr_body, issue_number, branch)
        partial = self.partial_delivery_refusal(
            record=record, worktree=worktree, issue_number=issue_number, branch=branch,
        )
        if partial is not None:
            return partial
        errors: list[str] = []
        exchange_mode, exchange_resolution_failed = self._review_exchange.resolve_create_pr_exchange_mode(
            exchange_mode=exchange_mode,
            agent_label=agent_label,
            errors=errors,
        )
        if exchange_resolution_failed:
            return PullRequestPreparationRefusal(tuple(errors))
        if self._review_exchange.missing_review_exchange_outcome(exchange_mode, exchange_result):
            return PullRequestPreparationRefusal(
                (f"{REVIEW_EXCHANGE_ERROR_PREFIX} missing exchange outcome before PR creation",)
            )

        return PreparedPullRequest(
            pr_title, pr_body, expected_base, stack_decision, exchange_mode, record.partial_pr
        )

    def partial_delivery_refusal(
        self, *, record: CompletionRecord, worktree: Path, issue_number: int, branch: str,
    ) -> PullRequestPreparationRefusal | None:
        """Run the partial-delivery guard before a branch write; report a refusal.

        Not retryable when the agent's words, or the existing PR's reference
        line, have to change first (#7288). Retryable when the guard only failed
        to read GitHub (a rate limit, #7297): the delivery itself is not at fault.
        """
        refusal = self._partial_delivery.refusal(
            worktree, issue_number=issue_number, branch=branch,
            claimed=record.partial_pr, claim_body=build_pr_body(record, issue_number),
        )
        if refusal is None:
            return None
        logger.error("Partial publication refused for #%d: %s", issue_number, refusal.reason)
        self._emit_publish_failed(
            issue_number=issue_number, stage=ERROR_PREFIX_CREATE_PR,
            error=refusal.reason, retryable=refusal.retryable, branch=branch,
        )
        return PullRequestPreparationRefusal(
            (f"{ERROR_PREFIX_CREATE_PR}: {refusal.reason}",), rate_limit=refusal.host_rate_limit
        )

    def _publish_base(
        self, issue_number: int, worktree: Path,
    ) -> tuple[Callable[[], str], StackBaseDecision | None] | PullRequestPreparationRefusal:
        """Resolve the PR base for a (possibly stacked) successor (#6596).

        Returns the callable to pass as the PR base: the predecessor branch for
        a stack successor whose gate is open, or the normal base for a non-stack
        issue (or when no gate is wired), with the raw gate verdict (``None``
        when no gate is wired) so the reuse path can confirm an existing PR
        targets the same base the gate requires. A blocked gate is a refusal.

        The refusal keys off ``not decision.allowed`` (not ``is_stack``): a
        fail-closed read error blocks publish even though the gate could not
        confirm the slice is a stack successor.
        """
        gate = self._stack_gate()
        if gate is None:
            return self._base_branch, None
        decision = gate.decide_publish(issue_number, worktree)
        if not decision.allowed:
            reason = decision.reason or "stack publish gate blocked"
            logger.error("Stack publish blocked for #%d: %s", issue_number, reason)
            self._emit_publish_failed(
                issue_number=issue_number,
                stage=ERROR_PREFIX_CREATE_PR,
                error=reason,
                retryable=decision.retryable,
            )
            return PullRequestPreparationRefusal(
                (f"{ERROR_PREFIX_CREATE_PR}: {reason}",), rate_limit=decision.host_rate_limit
            )
        if decision.base_branch:
            stacked_base = decision.base_branch
            logger.info(
                "Stack successor #%d PR will base on predecessor branch %s",
                issue_number,
                stacked_base,
            )
            return (lambda: stacked_base), decision
        return self._base_branch, decision
