from issue_orchestrator.domain.event_taxonomy import (
    EventIntent,
    infer_event_intent,
    is_review_event_name,
    is_review_exchange_event_name,
    is_review_oriented_event,
)
from issue_orchestrator.events import EventName


def test_review_event_classification_uses_catalog_membership() -> None:
    assert is_review_event_name(EventName.REVIEW_STARTED.value)
    assert is_review_event_name(EventName.REVIEW_EXCHANGE_ROUND_COMPLETED.value)
    assert not is_review_event_name(EventName.SESSION_STARTED.value)


def test_review_exchange_subfamily_classification() -> None:
    assert is_review_exchange_event_name(EventName.REVIEW_EXCHANGE_STARTED.value)
    assert not is_review_exchange_event_name(EventName.REVIEW_APPROVED.value)


def test_review_oriented_honors_task_hint() -> None:
    assert is_review_oriented_event(event_name="custom.plugin_event", task="review")
    assert not is_review_oriented_event(event_name="custom.plugin_event", task="code")


def test_infer_event_intent_prefers_typed_review_and_rework() -> None:
    assert infer_event_intent(event_name=EventName.REVIEW_STARTED.value) == EventIntent.REVIEW
    assert infer_event_intent(event_name=EventName.REWORK_STARTED.value) == EventIntent.REWORK
    assert infer_event_intent(event_name=EventName.SESSION_STARTED.value) == EventIntent.CODING


def test_role_level_review_exchange_events_classify_as_review() -> None:
    """ROLE_PROMPTED / ROLE_FEEDBACK / ROLE_TIMEOUT must classify as review-family
    events. Without this, the timeline projection assigns them ``phase=system``
    and ``review_oriented=False``.
    """
    role_events = (
        EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
        EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK,
        EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT,
    )
    for event in role_events:
        assert is_review_event_name(event.value), event.value
        assert is_review_exchange_event_name(event.value), event.value
        assert infer_event_intent(event_name=event.value) == EventIntent.REVIEW, event.value
