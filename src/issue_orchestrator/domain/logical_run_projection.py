"""Logical run projection for issue timelines.

A logical run is a lifecycle attempt (coding + review + rework chain),
which may include multiple physical session runs.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LogicalEventGroup:
    """Chronological timeline events that belong to one logical run/cycle."""

    logical_run: int
    logical_cycle: int
    events: tuple[dict[str, Any], ...]


def group_events_by_logical_cycle(
    events: Iterable[dict[str, Any]],
) -> tuple[LogicalEventGroup, ...]:
    """Group timeline events by logical run/cycle with legacy orphan repair.

    Older timeline rows can split a rework session's final completion into a
    new cycle when `rework_cycle` appears on `session.completed` but the earlier
    cached review events inherited the prior cycle. Keep that terminal tail with
    its rework start/review group so semantic projections do not claim review
    was not required.
    """
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        logical_run = _required_positive_int(event, "logical_run")
        logical_cycle = _required_positive_int(event, "logical_cycle")
        grouped[(logical_run, logical_cycle)].append(event)

    groups = tuple(
        LogicalEventGroup(logical_run=key[0], logical_cycle=key[1], events=tuple(items))
        for key, items in ((key, grouped[key]) for key in sorted(grouped))
    )
    return _repair_late_physical_attempt_groups(
        _merge_orphan_rework_terminal_groups(groups)
    )


class LogicalRunProjector:
    """Project timeline cycles into logical runs."""

    def logical_run_key(self, cycle: dict[str, Any]) -> str:
        """Return deterministic logical-run grouping key for a cycle."""
        lifecycle = cycle.get("lifecycle")
        if isinstance(lifecycle, int) and lifecycle > 0:
            return f"lifecycle:{lifecycle}"
        if lifecycle:
            return f"lifecycle:{lifecycle}"
        run_id = cycle.get("run_id")
        if run_id:
            return f"run:{run_id}"
        return "lifecycle:0"

    def filter_last_run_cycles(
        self, cycles: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Filter cycles to only those from the latest logical run."""
        if not cycles:
            return cycles
        max_lifecycle = max(c.get("lifecycle", 0) for c in cycles)
        if max_lifecycle <= 0:
            run_id_cycles = [c for c in cycles if c.get("run_id")]
            if run_id_cycles:
                latest_run_id = run_id_cycles[-1].get("run_id")
                return [c for c in cycles if c.get("run_id") == latest_run_id]
            return cycles
        return [c for c in cycles if c.get("lifecycle") == max_lifecycle]

    def annotate_cycle_in_run(
        self, cycles: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Annotate cycles with logical-run-local sequence numbers."""
        by_run_counts: dict[str, int] = {}
        for cycle in cycles:
            run_key = self.logical_run_key(cycle)
            by_run_counts[run_key] = by_run_counts.get(run_key, 0) + 1
            cycle["cycle_in_run"] = by_run_counts[run_key]
        return cycles

    def build_runs(self, cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:  # noqa: C901
        """Group cycle rows into logical runs."""
        if not cycles:
            return []

        grouped: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for cycle in cycles:
            run_key = self.logical_run_key(cycle)
            if run_key not in grouped:
                grouped[run_key] = []
                order.append(run_key)
            grouped[run_key].append(cycle)

        runs: list[dict[str, Any]] = []
        for index, run_key in enumerate(order, start=1):
            run_cycles = grouped[run_key]
            last_cycle = run_cycles[-1]
            run_id_candidates: list[str] = []
            for cycle in run_cycles:
                cycle_run_ids = cycle.get("session_run_ids")
                if isinstance(cycle_run_ids, list):
                    run_id_candidates.extend(
                        str(item) for item in cycle_run_ids if item
                    )
                elif cycle.get("run_id"):
                    run_id_candidates.append(str(cycle.get("run_id")))
            session_run_ids = list(dict.fromkeys(run_id_candidates))
            runs.append(
                {
                    "run_key": run_key,
                    "run_id": session_run_ids[-1] if session_run_ids else None,
                    "session_run_ids": session_run_ids,
                    "run_number": index,
                    "outcome": last_cycle.get("outcome", "In progress"),
                    "timestamp": last_cycle.get("timestamp", ""),
                    "time_label": last_cycle.get("time_label", ""),
                    "cycles": run_cycles,
                    "expanded": index == len(order),
                }
            )

        # UX invariant: only the latest run can be "In progress". If there is a
        # newer run, any older in-progress run/cycle is superseded.
        for run in runs[:-1]:
            if _is_in_progress_outcome(str(run.get("outcome") or "")):
                run["outcome"] = "Superseded"
            for cycle in run.get("cycles", []):
                if _is_in_progress_outcome(str(cycle.get("outcome") or "")):
                    cycle["outcome"] = "Superseded"
        return runs


def dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    """Return de-duplicated values while preserving insertion order."""
    return list(dict.fromkeys(values))


def _merge_orphan_rework_terminal_groups(
    groups: tuple[LogicalEventGroup, ...],
) -> tuple[LogicalEventGroup, ...]:
    merged: list[LogicalEventGroup] = []
    for group in groups:
        if merged and _is_orphan_rework_terminal_group(merged[-1], group):
            previous = merged.pop()
            merged.append(
                LogicalEventGroup(
                    logical_run=previous.logical_run,
                    logical_cycle=previous.logical_cycle,
                    events=previous.events + group.events,
                )
            )
            continue
        merged.append(group)
    return tuple(merged)


def _repair_late_physical_attempt_groups(
    groups: tuple[LogicalEventGroup, ...],
) -> tuple[LogicalEventGroup, ...]:
    repaired: list[LogicalEventGroup] = []
    next_cycle_by_run: dict[int, int] = {}
    for group in groups:
        segments = _split_late_physical_attempt_segments(group.events)
        cycle = max(
            group.logical_cycle,
            next_cycle_by_run.get(group.logical_run, group.logical_cycle),
        )
        for segment in segments:
            repaired.append(
                LogicalEventGroup(
                    logical_run=group.logical_run,
                    logical_cycle=cycle,
                    events=segment,
                )
            )
            cycle += 1
        next_cycle_by_run[group.logical_run] = cycle
    return tuple(repaired)


def _split_late_physical_attempt_segments(
    events: tuple[dict[str, Any], ...],
) -> tuple[tuple[dict[str, Any], ...], ...]:
    segments: list[tuple[dict[str, Any], ...]] = []
    current: list[dict[str, Any]] = []
    terminal_seen = False
    for event in events:
        if current and terminal_seen and _is_late_physical_rework_start(event):
            segments.append(tuple(current))
            current = []
            terminal_seen = False
        current.append(event)
        if _is_coding_terminal(event):
            terminal_seen = True
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def _is_orphan_rework_terminal_group(
    previous: LogicalEventGroup,
    current: LogicalEventGroup,
) -> bool:
    if current.logical_run != previous.logical_run:
        return False
    if current.logical_cycle != previous.logical_cycle + 1:
        return False
    if _has_iteration_start(current.events):
        return False
    if not _has_rework_start(previous.events):
        return False
    return _has_rework_terminal_tail(current.events)


def _has_iteration_start(events: Iterable[dict[str, Any]]) -> bool:
    return any(
        _event_name(event)
        in {"session.started", "rework.started", "rework.launching", "review.started"}
        for event in events
    )


def _has_rework_start(events: Iterable[dict[str, Any]]) -> bool:
    return any(
        _event_name(event) in {"rework.started", "agent.rework_started"}
        for event in events
    )


def _has_rework_terminal_tail(events: Iterable[dict[str, Any]]) -> bool:
    saw_terminal = False
    saw_rework_signal = False
    for event in events:
        if _event_name(event) in {"session.completed", "agent.completed"}:
            saw_terminal = True
        rework_cycle = event.get("rework_cycle")
        task = event.get("task")
        if (isinstance(rework_cycle, int) and rework_cycle > 0) or task == "rework":
            saw_rework_signal = True
    return saw_terminal and saw_rework_signal


def _event_name(event: dict[str, Any]) -> str:
    return str(event.get("source_event") or event.get("event") or "")


def _is_late_physical_rework_start(event: dict[str, Any]) -> bool:
    event_name = _event_name(event)
    if event_name in {"agent.rework_started", "rework.launching", "rework.started"}:
        return True
    if event.get("task") == "rework":
        return True
    rework_cycle = event.get("rework_cycle")
    return isinstance(rework_cycle, int) and rework_cycle > 0


def _is_coding_terminal(event: dict[str, Any]) -> bool:
    return _event_name(event) in {
        "agent.blocked",
        "agent.coding_completed",
        "agent.completed",
        "agent.failed",
        "agent.timed_out",
        "observation.completion_detected",
        "session.blocked",
        "session.completed",
        "session.failed",
        "session.timeout",
    }


def _required_positive_int(event: dict[str, Any], key: str) -> int:
    value = event.get(key)
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"timeline event missing required positive int field: {key}")
    return value


def _is_in_progress_outcome(outcome: str) -> bool:
    return outcome.strip().lower() == "in progress"
