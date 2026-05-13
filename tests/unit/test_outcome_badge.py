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

from dataclasses import dataclass

import pytest

from issue_orchestrator.view_models.journey_projection import (
    blocked_explanation_for_event,
    outcome_badge,
)
from issue_orchestrator.view_models.lifecycle_semantics import (
    IssueProjectionContext,
    OutcomeBadge,
)


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
        # ── blocked_explanation_for_event labels (PR #6333 round 2) ─
        # Reviewer found these labels — emitted by the projection's
        # OWN ``blocked_explanation_for_event`` helper — were
        # falling through to ``neutral`` (and thus rendering blocked
        # outcomes as grey rather than red).  Each one now classifies
        # explicitly as ``failed`` via the tone tables.
        ("Validation failed — project tests did not pass", "failed"),
        ("Validation failed: project tests did not pass", "failed"),
        # ``Validation failed — retrying`` is mid-flight (session has
        # not given up yet), so it MUST stay ``in_progress`` — the
        # more-specific prefix beats the broader "Validation failed —"
        # entry below.
        ("Validation failed — retrying", "in_progress"),
        ("Validation failed — retrying (cycle 2/3)", "in_progress"),
        ("Needs human input: clarify acceptance criteria", "failed"),
        ("Needs investigation: clarify acceptance criteria", "failed"),
        ("Needs human investigation", "failed"),
        ("Agent reported blocked: waiting for product decision", "failed"),
        ("Agent reported blocked: unknown reason", "failed"),
        ("Publishing failed: push rejected", "failed"),
        ("Publishing failed — could not push or create PR", "failed"),
        ("Session failed", "failed"),
        ("Session failed — session crashed", "failed"),
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


@dataclass(frozen=True)
class _BlockedExplanationCase:
    """One scenario for the ``blocked_explanation_for_event`` guardrail.

    Lives in the test module so it can be both parametrized AND
    documented inline next to the label-family expectation.  Mirrors
    the dispatch branches inside ``blocked_explanation_for_event`` —
    if a new branch is added there, add a matching case here.
    """

    event_name: str
    summary: str
    labels: tuple[str, ...]
    expected_substring: str
    expected_tone: str
    rework_cycle: int = 0
    max_rework_cycles: int = 5


_BLOCKED_EXPLANATION_CASES: tuple[_BlockedExplanationCase, ...] = (
    _BlockedExplanationCase(
        event_name="review.escalated",
        summary="",
        labels=(),
        expected_substring="Rework limit reached",
        expected_tone="failed",
        rework_cycle=3,
        max_rework_cycles=3,
    ),
    _BlockedExplanationCase(
        event_name="session.timeout",
        summary="",
        labels=(),
        expected_substring="Timed out",
        expected_tone="failed",
    ),
    _BlockedExplanationCase(
        event_name="session.validation_failed",
        summary="project tests did not pass",
        labels=(),
        expected_substring="Validation failed",
        expected_tone="failed",
    ),
    _BlockedExplanationCase(
        event_name="session.validation_failed",
        summary="",
        labels=(),
        expected_substring="Validation failed",
        expected_tone="failed",
    ),
    _BlockedExplanationCase(
        event_name="issue.needs_human",
        summary="clarify acceptance criteria",
        labels=("needs-human",),
        expected_substring="Needs human input",
        expected_tone="failed",
    ),
    _BlockedExplanationCase(
        event_name="blocked.generic",
        summary="clarify acceptance criteria",
        labels=("blocked-needs-human",),
        expected_substring="Needs investigation",
        expected_tone="failed",
    ),
    _BlockedExplanationCase(
        event_name="blocked.generic",
        summary="",
        labels=("needs-human",),
        expected_substring="Needs human investigation",
        expected_tone="failed",
    ),
    _BlockedExplanationCase(
        event_name="session.blocked",
        summary="waiting for product decision",
        labels=(),
        expected_substring="Agent reported blocked",
        expected_tone="failed",
    ),
    _BlockedExplanationCase(
        event_name="session.failed",
        summary="session crashed",
        labels=(),
        expected_substring="Session failed",
        expected_tone="failed",
    ),
    _BlockedExplanationCase(
        event_name="blocked.generic",
        summary="push rejected",
        labels=("publish-failed",),
        expected_substring="Publishing failed",
        expected_tone="failed",
    ),
    _BlockedExplanationCase(
        event_name="blocked.generic",
        summary="",
        labels=("publish-failed",),
        expected_substring="Publishing failed",
        expected_tone="failed",
    ),
    _BlockedExplanationCase(
        event_name="blocked.generic",
        summary="some failure summary",
        labels=("blocked-failed",),
        expected_substring="Failed",
        expected_tone="failed",
    ),
    _BlockedExplanationCase(
        event_name="blocked.generic",
        summary="",
        labels=("blocked-failed",),
        expected_substring="Session failed",
        expected_tone="failed",
    ),
    _BlockedExplanationCase(
        event_name="blocked.generic",
        summary="some summary",
        labels=(),
        expected_substring="Blocked",
        expected_tone="failed",
    ),
)


@pytest.mark.parametrize("case", _BLOCKED_EXPLANATION_CASES, ids=lambda c: c.expected_substring)
def test_blocked_explanation_labels_classify_as_terminal(
    case: _BlockedExplanationCase,
) -> None:
    """Every label ``blocked_explanation_for_event`` emits classifies as a
    terminal/problem tone — never ``neutral``.

    Guardrail for PR #6333 round-2 reviewer blocker: the single-owner
    ``outcome_badge()`` table was incomplete for labels emitted by the
    projection's own helper, so blocked/needs-human/publish-failed
    outcomes silently rendered as grey instead of red.  This test
    enumerates representative labels from every branch of
    ``blocked_explanation_for_event`` and asserts the expected tone is
    one of the terminal/problem tones (``failed`` / ``error``), never
    ``neutral`` — adding a new branch over there without extending the
    tone tables here will fire this test.
    """
    context = IssueProjectionContext(
        labels=case.labels,
        current_rework_cycle=case.rework_cycle,
        max_rework_cycles=case.max_rework_cycles,
    )
    label = blocked_explanation_for_event(
        context=context,
        event_name=case.event_name,
        summary=case.summary,
    )
    assert case.expected_substring in label, (
        f"blocked_explanation_for_event branch returned {label!r} for "
        f"event={case.event_name!r}, summary={case.summary!r}, "
        f"labels={case.labels!r} — expected substring "
        f"{case.expected_substring!r} to anchor the tone-table match."
    )
    badge = outcome_badge(label)
    assert badge.tone == case.expected_tone, (
        f"label={label!r} (from event={case.event_name!r}) classifies as "
        f"tone={badge.tone!r}, expected {case.expected_tone!r}.  PR #6333 "
        "round-2 blocker: terminal/problem labels MUST classify as a "
        "terminal tone, not ``neutral``."
    )
    assert badge.tone != "neutral", (
        f"label={label!r} mapped to tone='neutral' — that would resurrect "
        "the PR #6333 round-2 silent-grey bug for blocked outcomes."
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
