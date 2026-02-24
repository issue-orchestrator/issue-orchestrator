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
