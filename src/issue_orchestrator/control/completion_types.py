"""Shared completion-processing result types."""

from ..domain.completion_processing import ProcessingResult as ProcessingResult

ERROR_PREFIX_PUSH = "push_branch"
ERROR_PREFIX_CREATE_PR = "create_pr"
ERROR_PREFIX_PUBLISH_BLOCKED = "publish_blocked"
# A COMPLETED tech_lead session whose decision artifact pair is missing or
# rejected. Classified critical so the session's authoritative outcome is
# FAILED (ADR-0031 / #6761 finding 3), not a quiet success.
ERROR_PREFIX_TECH_LEAD_DECISION = "tech_lead_decision"
# A COMPLETED tech_lead session whose orchestrator-owned launch authority is
# missing, or whose agent-writable worktree copies (assignment / manifest)
# no longer match it — tamper evidence. Critical like the decision prefix
# (#6761 re-review finding 1).
ERROR_PREFIX_TECH_LEAD_AUTHORITY = "tech_lead_authority"
# A label that only NeedsHumanBlock may write reached an ordinary label writer
# (#6999 F2 round 5). Its own prefix, and NOT one of the publish prefixes,
# because the two consequences differ: a publish error is retried by the
# publish-recovery lane, whereas re-running this one would refuse again. What it
# must do instead is force the completion to FAIL, so an untrusted block request
# that was dropped can never be reported as a success.
ERROR_PREFIX_GOVERNED_LABEL = "governed_label"
REVIEW_EXCHANGE_ERROR_PREFIX = "review_exchange:"
