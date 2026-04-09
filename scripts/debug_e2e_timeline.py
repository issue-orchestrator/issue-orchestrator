#!/usr/bin/env python
"""Debug ``/api/e2e-run-detail/{id}`` issue-number matching against real DBs.

When the dashboard's E2E run drawer shows missing or wrong ``#N`` issue
affordances on test rows, this script lets you reproduce the exact
endpoint pipeline against your local timeline DBs without having to
restart the orchestrator or wait for a PR cycle.

It loads the real ``e2e.db`` + base-repo ``timeline.sqlite`` + e2e-worktree
``timeline.sqlite`` from a checkout, runs the production matcher
(``read_orchestrator_events_by_window`` →
``_attach_issue_numbers_to_test_windows``), and prints which test
windows have issue numbers attached and which agent events are
unmatched.

Workflow when investigating a regression::

    # 1. See the current state — note which test rows are EMPTY
    .venv/bin/python scripts/debug_e2e_timeline.py --run-id 87

    # 2. Edit the matcher / reader / view-filter code

    # 3. Re-run to see if the fix worked
    .venv/bin/python scripts/debug_e2e_timeline.py --run-id 87

    # 4. Once happy, snapshot the run as a regression fixture
    .venv/bin/python scripts/snapshot_e2e_run.py --run-id 87

For ad-hoc fixture-vs-live cross-validation: this script imports from
the working directory's source tree, so its output is what the endpoint
would produce after a restart with the current code. Compare it against
``curl http://localhost:<port>/api/e2e-run-detail/<id>`` to surface
endpoint-vs-helper drift (that's how the view-filter regression was
found).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _default_repo_root() -> Path:
    """Walk up from this script to the repo root."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists() and (parent / "src" / "issue_orchestrator").exists():
            return parent
    raise RuntimeError(f"Could not find repo root from {here}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", type=int, required=True, help="E2E run ID to inspect",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=None,
        help="Source repo root to read DBs from (default: current checkout)",
    )
    parser.add_argument(
        "--worktree-path", type=Path, default=None,
        help="Override the e2e-worktree path (default: <repo>/../<repo>-e2e-worktree)",
    )
    args = parser.parse_args()

    repo_root = (args.repo_root or _default_repo_root()).resolve()
    worktree_path = (
        args.worktree_path
        or repo_root.parent / f"{repo_root.name}-e2e-worktree"
    )

    sys.path.insert(0, str(_default_repo_root() / "src"))

    from issue_orchestrator.domain.timeline_key import TimelineKey
    from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
    from issue_orchestrator.entrypoints.control_api import (
        _attach_issue_numbers_to_test_windows,
        _build_test_windows,
    )
    from issue_orchestrator.entrypoints.web import _filter_timeline_events
    from issue_orchestrator.timeline import TimelineStream
    from issue_orchestrator.infra.e2e_db import E2EDB
    from issue_orchestrator.infra.e2e_timeline import read_orchestrator_events_by_window

    base_state = repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite"
    e2e_db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    wt_timeline = worktree_path / ".issue-orchestrator" / "state" / "timeline.sqlite"

    for path in (base_state, e2e_db_path):
        if not path.exists():
            print(f"ERROR: required source not found: {path}", file=sys.stderr)
            return 1

    # ---- E2E run records (base repo timeline) ----
    base_store = SqliteTimelineStore(db_path=base_state)
    run_key = TimelineKey.for_e2e_run(args.run_id).to_store_key()
    records = base_store.read(run_key, limit=10000)
    print(f"E2E run records (key={run_key}): {len(records)}")

    e2e_records = [r for r in records if r.event != "e2e.agent_snapshot"]
    snapshot_records = [r for r in records if r.event == "e2e.agent_snapshot"]
    print(f"  e2e_records={len(e2e_records)} snapshot_records={len(snapshot_records)}")

    if not e2e_records:
        print(f"No E2E records for run {args.run_id}", file=sys.stderr)
        return 1

    stream = TimelineStream.from_records(run_key, e2e_records)
    raw_events = [evt.to_dict() for evt in stream.events]
    for evt, rec in zip(raw_events, e2e_records):
        nodeid = rec.data.get("nodeid") if isinstance(rec.data, dict) else None
        if isinstance(nodeid, str) and nodeid:
            evt["nodeid"] = nodeid
    e2e_events = _filter_timeline_events(raw_events)

    # ---- Agent events (worktree fallback path or snapshots) ----
    db = E2EDB(e2e_db_path)
    run = db.get_run(args.run_id)
    if run is None:
        print(f"Run {args.run_id} not found in {e2e_db_path}", file=sys.stderr)
        return 1
    print(f"Run window: {run.started_at} → {run.finished_at}")

    if snapshot_records:
        print("Using snapshot records (production prefers these)")
        agent_events = [r.data for r in snapshot_records if isinstance(r.data, dict)]
    else:
        if not wt_timeline.exists():
            print(f"WARNING: worktree timeline missing: {wt_timeline}")
            agent_events = []
        else:
            print("No snapshots — using worktree fallback")
            agent_events = read_orchestrator_events_by_window(
                wt_timeline, started_at=run.started_at, finished_at=run.finished_at,
            )
    print(f"Agent events loaded: {len(agent_events)}")

    distinct_issues = sorted({
        e.get("issue_number") for e in agent_events
        if isinstance(e.get("issue_number"), int) and e.get("issue_number", 0) > 0
    })
    print(f"Distinct issue_numbers in agent events: {distinct_issues}")

    # ---- Build windows and run matcher ----
    windows = _build_test_windows(e2e_events)
    print(f"\nTest windows: {len(windows)}")
    for start_ts, end_ts, parent in windows:
        nodeid = parent.get("nodeid", "?").split("::")[-1]
        end_str = end_ts[11:23] if end_ts else "OPEN"
        print(f"  [{start_ts[11:23]} → {end_str}] {nodeid}")

    result = _attach_issue_numbers_to_test_windows(
        e2e_events, agent_events, run_id=args.run_id,
    )

    # ---- Per-test summary ----
    print("\nPer-test issue_affordances AFTER matching:")
    for evt in result:
        if evt.get("event") not in ("e2e.test_started", "e2e.test_completed"):
            continue
        nodeid = evt.get("nodeid", "?").split("::")[-1]
        affordances = evt.get("issue_affordances") or []
        issue_nums = sorted(a["issue_number"] for a in affordances)
        ts = evt["timestamp"][11:23]
        marker = "OK   " if issue_nums else "EMPTY"
        print(f"  {marker} {ts} {evt['event']:20s} {nodeid:60s} issues={issue_nums}")

    # ---- Diagnose unmatched events ----
    print("\nUnmatched agent events (didn't fall in any window):")
    unmatched = 0
    for ae in agent_events:
        ts = ae.get("timestamp", "")
        issue_num = ae.get("issue_number")
        if not isinstance(issue_num, int) or issue_num <= 0:
            continue
        in_any = False
        for start_ts, end_ts, _ in windows:
            if ts < start_ts:
                continue
            if end_ts is not None and ts > end_ts:
                continue
            in_any = True
            break
        if not in_any:
            unmatched += 1
            if unmatched <= 10:
                print(f"  ts={ts} issue={issue_num} event={ae.get('event')}")
    print(f"Total unmatched: {unmatched}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
