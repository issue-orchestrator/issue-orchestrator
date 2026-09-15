"""The orchestrator's own rendering of a tech-lead escalation (#7080).

A tech-lead escalation's comment IS its decision, so it must reach GitHub -- but
which comment reaches GitHub is the ORCHESTRATOR's call, not the agent's.
``CompletionRecord.comment_body`` is agent-supplied free prose and only bounded,
so a rule keyed on the completion OUTCOME alone would let an ``## Implementation``
write-up be posted under a ``needs_human`` outcome, walking straight through the
no-work-report rule the shaping exists to enforce (#7262 review F9).

Rendering here from the record's VALIDATED structured fields is the same
Agent-Intent / Orchestrator-Authority split the rest of the completion path uses:
the agent chooses what to escalate, the orchestrator chooses what the issue says.
It lives in the domain because it is a pure projection of a completion record and
decides no policy.
"""

from __future__ import annotations

from .models import CompletionOutcome, CompletionRecord


def render_tech_lead_escalation_comment(record: CompletionRecord) -> str | None:
    """Render a tech-lead escalation from its VALIDATED structured fields.

    The agent declares intent in typed fields the completion schema already
    requires -- ``question``/``context``/``options``/``default_action`` for
    needs-human, ``blocked_reason``/``attempted``/``blocked_by``/
    ``when_unblocked`` for blocked. ``comment_body`` is free prose the agent
    also supplies, and nothing constrains its content, so an outcome check alone
    would let an ``## Implementation`` write-up be posted under a ``needs_human``
    outcome (#7262 review F9).

    Returning the orchestrator's own rendering closes that: the agent chooses
    WHAT to escalate, the orchestrator chooses what the issue says. Returns
    ``None`` for a non-escalation outcome, leaving its body untouched.
    """
    if record.outcome is CompletionOutcome.NEEDS_HUMAN:
        parts = [f"## Needs Human Input\n\n**Question:** {record.question or ''}".rstrip()]
        if record.context:
            parts.append(f"**Context:** {record.context}")
        if record.options:
            parts.append("**Options:**")
            parts.extend(
                f"{index}. {option}" for index, option in enumerate(record.options, 1)
            )
        if record.default_action:
            parts.append(f"**Default if no response:** {record.default_action}")
        return "\n".join(parts)
    if record.outcome is CompletionOutcome.BLOCKED:
        blocked_by = ""
        if record.blocked_by:
            refs = ", ".join(f"#{number}" for number in record.blocked_by)
            blocked_by = f"\n**Blocked by:** {refs}"
        when_unblocked = ""
        if record.when_unblocked:
            when_unblocked = f"\n\n**When unblocked:** {record.when_unblocked}"
        return (
            f"## Blocked\n\n**Reason:** {record.blocked_reason or ''}{blocked_by}"
            f"\n**Attempted:** {record.attempted or ''}{when_unblocked}"
        )
    return None
