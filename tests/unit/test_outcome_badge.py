"""Tests for the typed ``OutcomeBadge`` and its single-owner tone
classifier (PR #6333 reviewer blocker fix).

Reviewer found that ``inline_agent_attempts.js`` silently rendered
unknown outcome labels (``Changes Requested``, ``Timed out: ...``,
``Needs human: ...``) as green ✓ because the UI string-matched
backend labels and fell through to ``passed`` for anything not in a
tiny exact-match set.

Path B fix: the projection layer constructs every label *and* its
tone via ``outcome_badge(label)``.  UI just reads ``outcome.tone``
to pick its visual treatment — no string-matching at the UI
boundary, single owner for "is this red/green/yellow/grey?".

These tests pin the tone mapping for every label the reviewer
called out, plus the canonical labels the projection helpers
(``_outcome_label`` / ``_round_completed_outcome_label`` /
``_session_outcome_label`` / ``_issue_blocked_outcome_label`` /
``derive_cycle_outcome``) emit.  Adding a new label requires adding
a fixture below; an unknown label must land on ``neutral``, never
``passed``.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.view_models.journey_projection import outcome_badge
from issue_orchestrator.view_models.lifecycle_semantics import OutcomeBadge


@pytest.mark.parametrize(
    ("label", "expected_tone"),
    [
        # ── Direct labels (``_DIRECT_OUTCOME_LABELS``) ──────────────
        ("Completed", "passed"),
        ("Approved", "passed"),
        ("Merged", "passed"),
        ("Changes Requested", "failed"),
        ("Escalated", "failed"),
        # ── Session-derived labels (``_session_outcome_label``) ────
        ("Failed: compile error", "failed"),
        ("Failed:", "failed"),  # empty summary still failed
        ("Timed out: agent did not call coding-done", "failed"),
        ("Timed out — agent didn't invoke completion command", "failed"),
        ("Timed out -- some other variant", "failed"),
        ("Agent blocked: waiting for product decision", "failed"),
        ("Agent blocked: unknown", "failed"),
        # ── Issue-blocked labels (``_issue_blocked_outcome_label``) ─
        ("Blocked: validation kept failing", "failed"),
        ("Blocked: blocked", "failed"),
        ("Needs human: clarify acceptance criteria", "failed"),
        ("Needs human: unknown", "failed"),
        # ── Blocked-explanation prefix (rework limit reached) ──────
        ("Rework limit reached (cycle 3/3) — reviewer keeps requesting changes", "failed"),
        # ── Older-run mutation (``LogicalRunProjector.build_runs``) ─
        ("Superseded", "neutral"),
        # ── Default placeholder when no outcome event seen ─────────
        ("In progress", "in_progress"),
        # ── ``Rework → X`` composition ─────────────────────────────
        ("Rework → Completed", "passed"),
        ("Rework → Approved", "passed"),
        ("Rework → Changes Requested", "failed"),
        ("Rework → Failed: build broke", "failed"),
        ("Rework → Timed out: oops", "failed"),
        ("Rework → In progress", "in_progress"),
        # Unknown inner falls through to in_progress (rework parent
        # implies work is still moving, not green).
        ("Rework → unfamiliar pass-through label", "in_progress"),
        # ── Lifecycle state strings (``_cycle_outcome`` path) ──────
        ("passed", "passed"),
        ("completed", "passed"),
        ("failed", "failed"),
        ("blocked", "failed"),
        ("errored", "error"),
        ("skipped", "neutral"),
        # ── Unknown / pass-through ─────────────────────────────────
        # The reviewer's actual finding: raw pass-through labels
        # from the projection must NOT silently render as green.
        ("Some unfamiliar third-party summary", "neutral"),
        ("", "neutral"),
        ("   ", "neutral"),
    ],
)
def test_outcome_badge_classifies_label_to_expected_tone(
    label: str, expected_tone: str
) -> None:
    """Every canonical label maps to its documented tone; unknown labels
    fall through to ``neutral``, never ``passed``.
    """
    badge = outcome_badge(label)
    assert isinstance(badge, OutcomeBadge)
    assert badge.label == label
    assert badge.tone == expected_tone, (
        f"expected tone={expected_tone!r} for label={label!r}, got {badge.tone!r}.  "
        "PR #6333 blocker: unknown / unrecognized labels must NEVER fall "
        "through to ``passed`` (silent green).  If this label is genuinely "
        "new, add it to the tone tables in journey_projection.py."
    )


def test_outcome_badge_silent_green_is_impossible_for_arbitrary_strings() -> None:
    """Property check: no random sample of plausible labels should map to
    ``passed`` unless it is in the explicit allow-list.

    Defends the contract from accidental regression — if someone adds a
    catch-all ``return "passed"`` to ``outcome_badge`` (which is what
    the original bug looked like), this test fires.
    """
    plausible_unknown_labels = [
        "Custom orchestrator state",
        "Pipeline aborted",
        "Awaiting input",
        "Stuck",
        "Needs review",
        "Unknown transition",
        "Pending classification",
        "compile error",  # raw summary
        "abc",
        "xyz123",
    ]
    for label in plausible_unknown_labels:
        badge = outcome_badge(label)
        assert badge.tone != "passed", (
            f"label={label!r} mapped to tone='passed' — that would resurrect "
            "the PR #6333 silent-green bug for arbitrary unknown labels."
        )
