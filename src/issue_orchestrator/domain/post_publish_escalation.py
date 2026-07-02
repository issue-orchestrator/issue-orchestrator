"""Post-publish escalation message rendering."""

from __future__ import annotations

from .models import PostPublishEscalationKind


def build_post_publish_escalation_comment(
    *,
    kind: PostPublishEscalationKind,
    reason: str,
    needs_human_label: str,
) -> str:
    """Markdown body posted to the PR when post-publish checks escalate."""
    if kind == "checks_pending_timeout":
        title = "⏱ Escalated to Human Review (CI checks timed out)"
    elif kind == "status_rollup_permission_denied":
        title = "🔑 Escalated to Human Review (cannot read check status)"
    elif kind == "merge_queue_failed":
        title = "🚦 Escalated to Human Review (merge queue rejected the PR)"
    else:  # branch_protection_blocked
        title = "🛑 Escalated to Human Review (branch protection)"
    return (
        f"## {title}\n\n"
        f"This PR was approved by the reviewer but cannot be merged "
        f"automatically.\n\n"
        f"**Diagnosis:** {reason}\n\n"
        f"**A human is needed to:**\n"
        f"- Investigate why merge is blocked.\n"
        f"- Either complete the merge manually, adjust branch "
        f"protection, or unblock CI; or\n"
        f"- Provide additional guidance and remove the "
        f"`{needs_human_label}` label so the orchestrator can resume."
    )
