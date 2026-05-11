"""Canonical lifecycle event-set ownership tests (issue #6310 AC-4).

These tests pin three contracts:

1. ``lifecycle_projection`` owns the canonical event classifiers
   (``CODING_TERMINAL_EVENTS``, ``VALIDATION_PASSED_EVENTS``,
   ``VALIDATION_FAILED_EVENTS``, ``OUTCOME_EVENTS``, ``BLOCKED_EVENT_NAMES``).
2. ``view_models.issue_detail`` re-uses those canonical sets via aliases —
   it must not define its own parallel classifier.
3. Any classification frozenset that still lives locally in
   ``view_models.issue_detail`` (e.g. journey-local skip filters) does not
   overlap with the canonical lifecycle sets.

The drift surfaced by PR #6309 (and that motivates issue #6310) was driven
exactly by parallel classifiers diverging quietly.  These tests catch a
re-introduction at unit-test time, not at Playwright time.
"""

from __future__ import annotations

from issue_orchestrator.view_models import issue_detail as ij
from issue_orchestrator.view_models import lifecycle_projection as lc


def test_canonical_classifier_sets_are_published_by_lifecycle_projection() -> None:
    """The canonical event classifiers live on ``lifecycle_projection``."""
    for name in (
        "CODING_TERMINAL_EVENTS",
        "VALIDATION_PASSED_EVENTS",
        "VALIDATION_FAILED_EVENTS",
        "OUTCOME_EVENTS",
        "BLOCKED_EVENT_NAMES",
    ):
        value = getattr(lc, name)
        assert isinstance(value, frozenset), f"{name} must be a frozenset"
        assert value, f"{name} must not be empty"


def test_issue_detail_aliases_reference_canonical_sets() -> None:
    """Issue-detail re-uses canonical sets (identity, not copy)."""
    assert ij._OUTCOME_EVENTS is lc.OUTCOME_EVENTS
    assert ij._BLOCKED_EVENT_NAMES is lc.BLOCKED_EVENT_NAMES


def test_journey_local_event_sets_do_not_overlap_with_canonical_sets() -> None:
    """Journey-local classifier sets in ``issue_detail`` are disjoint from canonical.

    If a future change adds an event name to a journey-local set that is
    also a canonical classifier, this test fails and points at the drift
    re-introduction (issue #6310 AC-4).
    """
    canonical = (
        lc.CODING_TERMINAL_EVENTS
        | lc.VALIDATION_PASSED_EVENTS
        | lc.VALIDATION_FAILED_EVENTS
        | lc.OUTCOME_EVENTS
        | lc.BLOCKED_EVENT_NAMES
    )
    # _JOURNEY_SKIP_EVENTS is the only remaining journey-local classifier
    # frozenset in issue_detail.  It is intentionally scoped to legacy
    # untagged events that should never appear in a canonical set.
    journey_local = ij._JOURNEY_SKIP_EVENTS
    overlap = canonical & journey_local
    assert not overlap, (
        f"event names duplicated between journey-local and canonical sets: "
        f"{sorted(overlap)}"
    )
