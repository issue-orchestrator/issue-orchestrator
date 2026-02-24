"""Logical run projection for issue timelines.

A logical run is a lifecycle attempt (coding + review + rework chain),
which may include multiple physical session runs.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


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

    def filter_last_run_cycles(self, cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

    def annotate_cycle_in_run(self, cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                    run_id_candidates.extend(str(item) for item in cycle_run_ids if item)
                elif cycle.get("run_id"):
                    run_id_candidates.append(str(cycle.get("run_id")))
            session_run_ids = list(dict.fromkeys(run_id_candidates))
            runs.append({
                "run_key": run_key,
                "run_id": session_run_ids[-1] if session_run_ids else None,
                "session_run_ids": session_run_ids,
                "run_number": index,
                "outcome": last_cycle.get("outcome", "In progress"),
                "time_label": last_cycle.get("time_label", ""),
                "cycles": run_cycles,
                "expanded": index == len(order),
            })

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


def _is_in_progress_outcome(outcome: str) -> bool:
    return outcome.strip().lower() == "in progress"
