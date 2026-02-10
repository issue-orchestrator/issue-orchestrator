from issue_orchestrator.events import EventHub
from issue_orchestrator.ports import TraceEvent


def test_event_hub_stats_and_replay_counts() -> None:
    hub = EventHub(max_events=2)

    hub.publish(TraceEvent("tick.started", {"tick_id": 1}).with_event_id(1))  # type: ignore
    hub.publish(TraceEvent("tick.started", {"tick_id": 2}).with_event_id(2))  # type: ignore
    hub.publish(TraceEvent("tick.started", {"tick_id": 3}).with_event_id(3))  # type: ignore

    stats = hub.stats()
    assert stats["buffer_size"] == 2
    assert stats["oldest_event_id"] == 2
    assert stats["newest_event_id"] == 3
    assert stats["total_published"] == 3

    events = hub.get_since(0)
    stats_after = hub.stats()
    assert [event.event_id for event in events] == [2, 3]
    assert stats_after["total_replay_requests"] == 1
    assert stats_after["total_replay_events"] == 2
    assert stats_after["total_replay_out_of_range"] == 1
