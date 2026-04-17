"""Nesting helpers for E2E pytest and orchestrator timeline events."""


def _build_test_windows(
    pytest_events: list[dict],
) -> list[tuple[str, str, dict]]:
    """Pair test_started/test_completed into (start_ts, end_ts, parent_dict) windows."""
    windows: list[tuple[str, str, dict]] = []
    started_map: dict[str, tuple[str, dict]] = {}

    for evt in pytest_events:
        evt.setdefault("children", [])
        name = evt.get("event", "")
        if name == "e2e.test_started":
            nodeid = (evt.get("nodeid") or "").strip()
            if nodeid:
                started_map[nodeid] = (evt["timestamp"], evt)
        elif name == "e2e.test_completed":
            nodeid = (evt.get("nodeid") or "").strip()
            if nodeid and nodeid in started_map:
                start_ts, started_dict = started_map.pop(nodeid)
                windows.append((start_ts, evt["timestamp"], started_dict))
    return windows


def _find_nearest_preceding(pytest_events: list[dict], ts: str) -> dict | None:
    """Return the nearest pytest event whose timestamp <= ts, or None."""
    best = None
    for p_evt in pytest_events:
        if p_evt.get("timestamp", "") <= ts:
            best = p_evt
        else:
            break  # pytest_events are in chronological order
    return best


def nest_orchestrator_events(
    pytest_events: list[dict],
    orch_events: list[dict],
) -> None:
    """Mutate pytest_events in-place, adding ``children`` lists.

    Strategy:
    1. Pair test_started / test_completed into windows keyed by nodeid.
    2. For each orchestrator event, find the window whose start <= ts <= end.
    3. If no window matches, attach to the nearest preceding pytest event.
    """
    windows = _build_test_windows(pytest_events)
    sorted_orch = sorted(orch_events, key=lambda e: e.get("timestamp", ""))

    for orch_evt in sorted_orch:
        ts = orch_evt.get("timestamp", "")
        parent = _find_window_parent(windows, ts) or _find_nearest_preceding(pytest_events, ts)
        if parent is not None:
            parent["children"].append(orch_evt)
        elif pytest_events:
            pytest_events[0]["children"].append(orch_evt)


def _find_window_parent(
    windows: list[tuple[str, str, dict]], ts: str,
) -> dict | None:
    """Return the parent dict for the window containing ts, or None."""
    for start_ts, end_ts, parent_evt in windows:
        if start_ts <= ts <= end_ts:
            return parent_evt
    return None
