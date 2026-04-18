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
import os
import tempfile
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
    """Persist ``counters`` atomically.

    Atomicity matters even though ``coding-done`` is single-threaded:
    a future reader (an orchestrator introspection path, a diagnostic
    tool, concurrent sessions in a shared worktree) must never observe
    a torn JSON file. The fallback on torn reads is "treat as empty",
    which would silently reset the counter mid-session — the worst
    case for the budget's integrity: the agent gets an extra retry it
    hasn't earned.

    Write to a tempfile on the same filesystem and ``os.replace`` into
    place; the rename is atomic on POSIX.
    """
    path = _counter_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not counters:
        # Empty dict → delete file so stale ``.issue-orchestrator/`` state
        # doesn't accumulate after happy-path completions.
        path.unlink(missing_ok=True)
        return

    encoded = json.dumps(counters, indent=2, sort_keys=True) + "\n"
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(encoded)
        os.replace(tmp_path_str, path)
    except Exception:
        # Only clean up on failure; the successful path has already
        # renamed the tempfile out of existence.
        try:
            os.unlink(tmp_path_str)
        except FileNotFoundError:
            pass
        raise


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


# Lines of the dirty-file list to include verbatim in escalation
# previews before collapsing the tail to "... and N more". Shared
# between the plain-text ``context`` and the Markdown ``comment_body``
# so a future edit to one cannot drift the other.
_DIRTY_PREVIEW_LIMIT = 20


def _render_dirty_preview(
    dirty_files: list[str], *, indent: str, tail_prefix: str
) -> str:
    """Return a preview of up to ``_DIRTY_PREVIEW_LIMIT`` dirty lines.

    ``indent`` is prepended to each shown line; ``tail_prefix`` leads
    the "and N more" sentinel. Keeping both forms on one helper means
    plain-text and Markdown previews stay consistent on limit and
    formatting.
    """
    shown = dirty_files[:_DIRTY_PREVIEW_LIMIT]
    body = "\n".join(f"{indent}{line}" for line in shown)
    remaining = len(dirty_files) - len(shown)
    if remaining > 0:
        body += f"\n{tail_prefix}and {remaining} more"
    return body


def build_escalation_payload(
    *, session_id: str, dirty_files: list[str], count: int
) -> EscalationRecord:
    """Assemble the ``needs_human`` payload for a budget-exhausted rejection.

    The agent never asked for an escalation — we are fabricating one on
    their behalf. The question, summary, and body therefore must make
    that *explicit* so a reviewing human doesn't mistake this for the
    agent's own judgement call.
    """
    context_preview = _render_dirty_preview(
        dirty_files, indent="  ", tail_prefix="  ... "
    )
    body_preview = _render_dirty_preview(
        dirty_files, indent="", tail_prefix="... "
    )

    question = (
        f"Auto-escalated by coding-done: the working tree stayed dirty "
        f"after {count} consecutive attempts. The agent could not clean "
        f"the tree on its own. Please review the listed files and decide "
        f"whether they should be committed, gitignored at the project "
        f"level, or investigated for a deeper problem."
    )
    context = f"Dirty files ({len(dirty_files)}):\n{context_preview}"
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
        f"```\n{body_preview}\n```\n"
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
