"""Centralized label registry - single source of truth for label semantics.

All label constants and logic live here. This enables:
- IDE autocomplete and typo prevention
- Consistent blocking logic across scheduler, monitor, orchestrator
- Easy refactoring when label names change

Naming convention:
- blocked-* prefix means "don't process this issue"
- The prefix itself carries semantic meaning
"""

from typing import Sequence

# =============================================================================
# Label Constants - use these instead of string literals
# =============================================================================

# Ownership label - orchestrator is actively working on this
IN_PROGRESS = "in-progress"

# PR pending - session completed with PR, awaiting merge
PR_PENDING = "pr-pending"

# Blocking labels
BLOCKED = "blocked"                      # generic blocking (deps, external, etc.)
BLOCKED_FAILED = "blocked-failed"        # session crashed/failed/timed out
BLOCKED_NEEDS_HUMAN = "blocked-needs-human"  # needs human decision
BLOCKED_CROSS_MILESTONE = "blocked-cross-milestone"  # dependency violates milestone scope

# Legacy label names (for backwards compatibility during migration)
LEGACY_NEEDS_HUMAN = "needs-human"
LEGACY_FAILED = "failed"

# The magic prefixes that indicate blocking
BLOCKING_PREFIX = "blocked-"
BLOCKING_COLON_PREFIX = "blocked:"

# =============================================================================
# Claim/Lease Labels - for multi-orchestrator coordination
# =============================================================================

# Issue is claimed by an orchestrator instance
IO_CLAIMED = "io:claimed"

# Claim was lost during active session (work may be salvageable)
BLOCKED_CLAIM_LOST = "blocked:claim-lost"

# Stale claim detected (orchestrator crashed without releasing)
BLOCKED_STALE_CLAIM = "blocked:stale-claim"

# Issue needs reconciliation after claim conflict
NEEDS_RECONCILE = "needs-reconcile"


# =============================================================================
# Query Functions
# =============================================================================

def is_blocking(label: str) -> bool:
    """Check if a single label blocks processing.

    A label blocks if it:
    - Is exactly 'blocked', OR
    - Starts with 'blocked-' prefix, OR
    - Starts with 'blocked:' prefix, OR
    - Is a legacy blocking label (needs-human, failed)
    """
    if label == BLOCKED:
        return True
    if label.startswith(BLOCKING_PREFIX) or label.startswith(BLOCKING_COLON_PREFIX):
        return True
    # Legacy support - remove after migration
    if label in (LEGACY_NEEDS_HUMAN, LEGACY_FAILED):
        return True
    return False


def is_blocking_any(labels: Sequence[str]) -> bool:
    """Check if any label in the list blocks processing."""
    return any(is_blocking(label) for label in labels)


def get_blocking_labels(labels: Sequence[str]) -> list[str]:
    """Return all blocking labels from a list."""
    return [label for label in labels if is_blocking(label)]


def is_in_progress(labels: Sequence[str]) -> bool:
    """Check if issue is marked as in-progress."""
    return IN_PROGRESS in labels


def is_pr_pending(labels: Sequence[str]) -> bool:
    """Check if issue has a PR pending merge."""
    return PR_PENDING in labels


def requires_human(label: str) -> bool:
    """Check if label requires human intervention."""
    return label in (BLOCKED_NEEDS_HUMAN, LEGACY_NEEDS_HUMAN)


def requires_human_any(labels: Sequence[str]) -> bool:
    """Check if any label requires human intervention."""
    return any(requires_human(label) for label in labels)


# =============================================================================
# Label Selection Helpers
# =============================================================================

def pick_blocking_label(
    *,
    failed: bool = False,
    needs_human: bool = False,
) -> str:
    """Pick the appropriate blocking label based on reason.

    Usage:
        label = pick_blocking_label(needs_human=True)
        # Returns: "blocked-needs-human"

    For generic blocking (dependencies, external, etc.), use BLOCKED directly.
    """
    if needs_human:
        return BLOCKED_NEEDS_HUMAN
    if failed:
        return BLOCKED_FAILED
    # Default to generic blocked
    return BLOCKED
