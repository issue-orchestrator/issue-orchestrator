"""Canonical lifecycle event-set ownership tests (issue #6310 AC-4).

These tests pin three contracts:

1. ``lifecycle_event_sets`` owns the canonical event classifiers
   (``CODING_TERMINAL_EVENTS``, ``VALIDATION_PASSED_EVENTS``,
   ``VALIDATION_FAILED_EVENTS``, ``OUTCOME_EVENTS``, ``BLOCKED_EVENT_NAMES``).
   Both ``lifecycle_projection`` and ``journey_projection`` import from
   here — neither copies a set.
2. ``view_models.issue_detail`` and ``lifecycle_projection`` reference
   those canonical sets by identity, not by copy.
3. Any classification frozenset that still lives locally in
   ``view_models.issue_detail`` (e.g. journey-local skip filters) does not
   overlap with the canonical sets.

The drift surfaced by PR #6309 (and that motivates issue #6310) was driven
exactly by parallel classifiers diverging quietly.  These tests catch a
re-introduction at unit-test time, not at Playwright time.
"""

from __future__ import annotations

from issue_orchestrator.view_models import (
    issue_detail as ij,
    lifecycle_event_sets as classifiers,
    lifecycle_projection as lc,
)


def test_canonical_classifier_sets_are_published_by_lifecycle_event_sets() -> None:
    """The canonical event classifiers live on ``lifecycle_event_sets``."""
    for name in (
        "CODING_TERMINAL_EVENTS",
        "VALIDATION_PASSED_EVENTS",
        "VALIDATION_FAILED_EVENTS",
        "OUTCOME_EVENTS",
        "BLOCKED_EVENT_NAMES",
    ):
        value = getattr(classifiers, name)
        assert isinstance(value, frozenset), f"{name} must be a frozenset"
        assert value, f"{name} must not be empty"


def test_lifecycle_projection_re_exports_canonical_sets_by_identity() -> None:
    """``lifecycle_projection`` re-publishes the canonical sets by identity."""
    assert lc.CODING_TERMINAL_EVENTS is classifiers.CODING_TERMINAL_EVENTS
    assert lc.VALIDATION_PASSED_EVENTS is classifiers.VALIDATION_PASSED_EVENTS
    assert lc.VALIDATION_FAILED_EVENTS is classifiers.VALIDATION_FAILED_EVENTS
    assert lc.OUTCOME_EVENTS is classifiers.OUTCOME_EVENTS
    assert lc.BLOCKED_EVENT_NAMES is classifiers.BLOCKED_EVENT_NAMES


def test_issue_detail_aliases_reference_canonical_sets() -> None:
    """Issue-detail re-uses canonical sets (identity, not copy)."""
    assert ij._OUTCOME_EVENTS is classifiers.OUTCOME_EVENTS
    assert ij._BLOCKED_EVENT_NAMES is classifiers.BLOCKED_EVENT_NAMES


def test_journey_local_event_sets_do_not_overlap_with_canonical_sets() -> None:
    """Journey-local classifier sets in ``issue_detail`` are disjoint from canonical.

    If a future change adds an event name to a journey-local set that is
    also a canonical classifier, this test fails and points at the drift
    re-introduction (issue #6310 AC-4).
    """
    canonical = (
        classifiers.CODING_TERMINAL_EVENTS
        | classifiers.VALIDATION_PASSED_EVENTS
        | classifiers.VALIDATION_FAILED_EVENTS
        | classifiers.OUTCOME_EVENTS
        | classifiers.BLOCKED_EVENT_NAMES
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
