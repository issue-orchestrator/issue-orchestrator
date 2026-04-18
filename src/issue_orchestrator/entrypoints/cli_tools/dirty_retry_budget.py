"""Retry budget + auto-escalation for coding-done dirty-tree rejections.

## Why

When ``coding-done`` rejects a dirty tree, the agent retries. Without a
cap, retries continue until the session-level timeout (90 minutes on
our current config) kills the session. That 90 minutes is almost
entirely wasted: either the dirty state is resolvable (the agent
fails to commit for some reason — misunderstanding, tool failure,
missed file) and a single successful retry is enough, or it's
fundamentally unresolvable and no amount of retrying will help.

Issue #5949 item 2 specifies: cap consecutive rejections at a small
budget, then auto-escalate by writing a ``needs_human`` completion
record and exiting cleanly. Saves 80+ minutes per unresolvable
rejection vs. the session-level timeout.

## State model

- One counter file at ``<worktree>/.issue-orchestrator/dirty-rejection-count.json``
  keyed by session id. Multiple concurrent sessions in the same
  worktree (rare) each get their own counter.
- Counter increments on each rejection. On the Nth rejection
  (``DIRTY_REJECTION_BUDGET``) the caller escalates instead of
  rejecting again.
- Counter clears as soon as a call passes the dirty check — the agent
  has demonstrated recovery, so subsequent rejections start from zero.
- In standalone mode (no ``ISSUE_ORCHESTRATOR_SESSION_ID``) the
  caller should not record at all; the budget is an
  orchestrator-managed-session concern. Developer dry-runs that call
  ``coding-done`` repeatedly must not trip the auto-escalation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Escalate on the Nth consecutive rejection (1-indexed). Two chosen so
# the agent gets exactly one "try again" cycle after the first failure:
# ample room for "I forgot to ``git add``" misses, no room for an
# agent to silently burn the full session-level timeout.
DIRTY_REJECTION_BUDGET: int = 2

COUNTER_RELATIVE_PATH: Path = Path(".issue-orchestrator/dirty-rejection-count.json")


@dataclass(frozen=True)
class EscalationRecord:
    """Fields the caller needs to fabricate a ``needs_human`` completion.

    Callers pass these to ``build_completion_record``/``write_completion_record``
    in the agent-done shared core rather than reaching into the
    dataclass directly — that keeps the runtime CompletionRecord import
    out of this pure-logic module.
    """

    session_id: str
    question: str
    context: str
    summary: str
    comment_body: str


def _counter_path(worktree: Path) -> Path:
    return worktree / COUNTER_RELATIVE_PATH


def _load(worktree: Path) -> dict[str, int]:
    path = _counter_path(worktree)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # Corruption is unrecoverable state; treat as empty and rewrite.
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, int)}


def _save(worktree: Path, counters: dict[str, int]) -> None:
    path = _counter_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    if counters:
        path.write_text(json.dumps(counters, indent=2, sort_keys=True) + "\n")
    else:
        # Empty dict → delete file so stale ``.issue-orchestrator/`` state
        # doesn't accumulate after happy-path completions.
        path.unlink(missing_ok=True)


def record_rejection(worktree: Path, session_id: str) -> int:
    """Increment the rejection counter for ``session_id``. Return new count."""
    counters = _load(worktree)
    new_count = counters.get(session_id, 0) + 1
    counters[session_id] = new_count
    _save(worktree, counters)
    return new_count


def reset_rejection_counter(worktree: Path, session_id: str) -> None:
    """Clear the counter for ``session_id`` after a call passes the guard.

    Successful coding-done (dirty check passed) proves the agent has
    recovered from whatever caused the prior rejections. Subsequent
    rejections in the same session should start counting from zero
    rather than continuing from the prior streak.
    """
    counters = _load(worktree)
    if session_id not in counters:
        return
    counters.pop(session_id)
    _save(worktree, counters)


def is_budget_exhausted(count: int) -> bool:
    """Return True when ``count`` warrants auto-escalation."""
    return count >= DIRTY_REJECTION_BUDGET


def build_escalation_payload(
    *, session_id: str, dirty_files: list[str], count: int
) -> EscalationRecord:
    """Assemble the ``needs_human`` payload for a budget-exhausted rejection.

    The agent never asked for an escalation — we are fabricating one on
    their behalf. The question, summary, and body therefore must make
    that *explicit* so a reviewing human doesn't mistake this for the
    agent's own judgement call.
    """
    preview_count = 20
    preview = "\n".join(f"  {line}" for line in dirty_files[:preview_count])
    if len(dirty_files) > preview_count:
        preview += f"\n  ... and {len(dirty_files) - preview_count} more"

    question = (
        f"Auto-escalated by coding-done: the working tree stayed dirty "
        f"after {count} consecutive attempts. The agent could not clean "
        f"the tree on its own. Please review the listed files and decide "
        f"whether they should be committed, gitignored at the project "
        f"level, or investigated for a deeper problem."
    )
    context = f"Dirty files ({len(dirty_files)}):\n{preview}"
    summary = (
        f"Auto-escalated to needs_human after {count} dirty-tree rejections"
    )
    comment_body = (
        f"### Auto-escalation from coding-done\n\n"
        f"`coding-done` rejected this session {count} times in a row because "
        f"the working tree was dirty each time. The agent could not resolve "
        f"it, so the orchestrator is escalating to a human rather than "
        f"letting the session burn to the 90-minute timeout.\n\n"
        f"**Dirty files ({len(dirty_files)}):**\n\n"
        f"```\n"
        + "\n".join(dirty_files[:preview_count])
        + (
            f"\n... and {len(dirty_files) - preview_count} more"
            if len(dirty_files) > preview_count
            else ""
        )
        + "\n```\n"
    )

    return EscalationRecord(
        session_id=session_id,
        question=question,
        context=context,
        summary=summary,
        comment_body=comment_body,
    )


def build_completion_record_for_escalation(
    payload: EscalationRecord,
    *,
    completion_record_cls: Any,
    completion_outcome_cls: Any,
    status_to_actions: dict[str, list[Any]],
    needs_human_status: str,
) -> Any:
    """Build a ``CompletionRecord`` of outcome ``needs_human`` from ``payload``.

    Factored out so the ``coding_done`` entry point can stay thin and so
    unit tests can feed simple stand-in classes without pulling in the
    runtime domain model.
    """
    return completion_record_cls(
        session_id=payload.session_id,
        timestamp=datetime.now().isoformat(),
        outcome=completion_outcome_cls.NEEDS_HUMAN,
        summary=payload.summary,
        requested_actions=status_to_actions[needs_human_status],
        question=payload.question,
        context=payload.context,
        options=None,
        default_action=None,
        comment_body=payload.comment_body,
    )
