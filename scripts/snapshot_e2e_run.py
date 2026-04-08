#!/usr/bin/env python
"""Capture an E2E run into a sanitized fixture for integration tests.

Reads a real E2E run from the local checkout's databases and writes a
sanitized, self-contained fixture under ``tests/fixtures/e2e_runs/run_<id>/``.

The fixture lets the integration test
``tests/integration/test_e2e_timeline_real_fixture.py`` replay the run
through the live ``/api/e2e-run-detail/{id}`` pipeline against ground
truth, catching regressions in the matcher / view-filter / reader stack
that synthetic unit tests miss.

Usage::

    .venv/bin/python scripts/snapshot_e2e_run.py --run-id 87
    .venv/bin/python scripts/snapshot_e2e_run.py --run-id 87 \
        --repo-root /path/to/checkout --output tests/fixtures/e2e_runs/run_87

Sanitization: absolute filesystem paths in the captured rows are
rewritten to ``<REPO_ROOT>`` placeholders, worker PIDs are zeroed, and
log/artifact paths are dropped. Issue numbers, event names, timestamps
and view tags are preserved verbatim — they are exactly what the
integration test pins.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any


REPO_ROOT_PLACEHOLDER = "<REPO_ROOT>"


def _default_repo_root() -> Path:
    """Walk up from this script to find the repo root."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists() and (parent / "src" / "issue_orchestrator").exists():
            return parent
    raise RuntimeError(f"Could not find repo root from {here}")


def _sanitize_path(value: str | None, repo_root: Path) -> str | None:
    """Replace absolute repo paths with the placeholder."""
    if not value or not isinstance(value, str):
        return value
    return value.replace(str(repo_root), REPO_ROOT_PLACEHOLDER)


def _sanitize_data_blob(blob: str, repo_root: Path) -> str:
    """Sanitize an opaque JSON data_json blob.

    We do not parse and round-trip the JSON because the integration test
    cares about exact-match contracts. Instead we do a string substitution
    of any embedded absolute paths.
    """
    return blob.replace(str(repo_root), REPO_ROOT_PLACEHOLDER)


def _copy_e2e_run_row(
    src_db: Path,
    dst_db: Path,
    run_id: int,
    repo_root: Path,
) -> dict[str, Any]:
    """Copy one e2e_runs row, sanitized, into a fresh DB. Returns the row dict."""
    src = sqlite3.connect(str(src_db))
    src.row_factory = sqlite3.Row

    schema_rows = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='e2e_runs'",
    ).fetchall()
    if not schema_rows:
        src.close()
        raise RuntimeError(f"No e2e_runs table in {src_db}")
    create_sql = schema_rows[0]["sql"]

    row = src.execute("SELECT * FROM e2e_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        src.close()
        raise RuntimeError(f"Run {run_id} not found in {src_db}")
    row_dict = {k: row[k] for k in row.keys()}
    src.close()

    # Sanitize sensitive fields.
    row_dict["repo_root"] = REPO_ROOT_PLACEHOLDER
    row_dict["worker_pid"] = 0
    row_dict["log_path"] = None
    row_dict["artifacts_dir"] = None
    row_dict["note"] = None

    if dst_db.exists():
        dst_db.unlink()
    dst = sqlite3.connect(str(dst_db))
    dst.execute(create_sql)
    cols = list(row_dict.keys())
    placeholders = ", ".join("?" for _ in cols)
    dst.execute(
        f"INSERT INTO e2e_runs ({', '.join(cols)}) VALUES ({placeholders})",
        [row_dict[c] for c in cols],
    )
    dst.commit()
    dst.close()
    return row_dict


def _copy_timeline_rows(
    src_db: Path,
    dst_db: Path,
    where_sql: str,
    where_params: tuple,
    repo_root: Path,
) -> int:
    """Copy timeline_events rows matching ``where_sql`` into a fresh DB.

    Returns the number of rows copied. Sanitizes any embedded absolute
    paths in the data_json blobs.
    """
    if not src_db.exists():
        raise RuntimeError(f"Source timeline DB does not exist: {src_db}")

    src = sqlite3.connect(str(src_db))
    src.row_factory = sqlite3.Row

    schema_rows = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='timeline_events'",
    ).fetchall()
    if not schema_rows:
        src.close()
        raise RuntimeError(f"No timeline_events table in {src_db}")
    create_table_sql = schema_rows[0]["sql"]

    index_sqls = [
        r["sql"] for r in src.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='timeline_events' AND sql IS NOT NULL"
        ).fetchall()
    ]

    rows = src.execute(
        f"SELECT * FROM timeline_events WHERE {where_sql} ORDER BY sequence ASC",
        where_params,
    ).fetchall()

    if dst_db.exists():
        dst_db.unlink()
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(str(dst_db))
    dst.execute(create_table_sql)
    for idx_sql in index_sqls:
        try:
            dst.execute(idx_sql)
        except sqlite3.OperationalError:
            pass

    # Match the source DB's PRAGMA user_version so SqliteTimelineStore
    # does NOT decide the schema is stale and drop our copied rows during
    # its initialize() call. Without this the store rebuilds the table
    # empty when the fixture is loaded.
    src_user_version = src.execute("PRAGMA user_version").fetchone()[0]
    dst.execute(f"PRAGMA user_version = {int(src_user_version)}")

    if rows:
        col_names = [c[0] for c in src.execute("SELECT * FROM timeline_events LIMIT 0").description]
        col_names_no_seq = [c for c in col_names if c != "sequence"]
        placeholders = ", ".join("?" for _ in col_names_no_seq)
        for r in rows:
            values = []
            for c in col_names_no_seq:
                v = r[c]
                if c == "data_json" and isinstance(v, str):
                    v = _sanitize_data_blob(v, repo_root)
                values.append(v)
            dst.execute(
                f"INSERT INTO timeline_events ({', '.join(col_names_no_seq)}) VALUES ({placeholders})",
                values,
            )
    dst.commit()
    dst.close()
    src.close()
    return len(rows)


def _build_expected(
    base_db: Path,
    worktree_db: Path,
    run_id: int,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    """Compute the expected (nodeid -> issue_numbers) mapping by replaying
    the production matcher against the captured DBs.

    Imports the orchestrator code so this naturally tracks code changes —
    if the contract genuinely changes, the captured ``expected.json`` will
    need re-blessing via this script.
    """
    sys.path.insert(0, str(_default_repo_root() / "src"))

    from issue_orchestrator.domain.timeline_key import TimelineKey
    from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
    from issue_orchestrator.entrypoints.control_api import _attach_issue_numbers_to_test_windows
    from issue_orchestrator.entrypoints.web import _filter_timeline_events
    from issue_orchestrator.timeline import TimelineStream
    from issue_orchestrator.infra.e2e_timeline import read_orchestrator_events_by_window

    base_store = SqliteTimelineStore(db_path=base_db)
    run_key = TimelineKey.for_e2e_run(run_id).to_store_key()
    records = base_store.read(run_key, limit=10000)
    e2e_records = [r for r in records if r.event != "e2e.agent_snapshot"]
    stream = TimelineStream.from_records(run_key, e2e_records)
    raw_events = [evt.to_dict() for evt in stream.events]
    for evt, rec in zip(raw_events, e2e_records):
        nodeid = rec.data.get("nodeid") if isinstance(rec.data, dict) else None
        if isinstance(nodeid, str) and nodeid:
            evt["nodeid"] = nodeid
    e2e_events = _filter_timeline_events(raw_events)

    agent_events = read_orchestrator_events_by_window(
        worktree_db, started_at=started_at, finished_at=finished_at,
    )

    matched = _attach_issue_numbers_to_test_windows(e2e_events, agent_events)

    test_results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for evt in matched:
        if evt.get("event") != "e2e.test_started":
            continue
        nodeid = evt.get("nodeid", "")
        if not nodeid or nodeid in seen:
            continue
        seen.add(nodeid)
        test_results.append(
            {
                "nodeid": nodeid,
                "issue_numbers": sorted(evt.get("issue_numbers", [])),
            }
        )

    distinct_issues_in_window = sorted({
        ae.get("issue_number") for ae in agent_events
        if isinstance(ae.get("issue_number"), int) and ae.get("issue_number", 0) > 0
    })

    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "agent_event_count": len(agent_events),
        "distinct_issues_in_window": distinct_issues_in_window,
        "test_count": len(test_results),
        "tests": test_results,
    }


def snapshot_run(repo_root: Path, run_id: int, output_dir: Path) -> None:
    """Capture run ``run_id`` into ``output_dir``."""
    repo_root = repo_root.resolve()
    worktree_path = repo_root.parent / f"{repo_root.name}-e2e-worktree"

    src_e2e_db = repo_root / ".issue-orchestrator" / "e2e.db"
    src_base_timeline = repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite"
    src_worktree_timeline = worktree_path / ".issue-orchestrator" / "state" / "timeline.sqlite"

    for path in (src_e2e_db, src_base_timeline, src_worktree_timeline):
        if not path.exists():
            raise RuntimeError(f"Required source DB not found: {path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    dst_e2e_db = output_dir / "e2e.db"
    dst_base_timeline = output_dir / "base_timeline.sqlite"
    dst_worktree_timeline = output_dir / "worktree_timeline.sqlite"
    dst_expected = output_dir / "expected.json"

    print(f"Snapshotting run {run_id} from {repo_root}")
    print(f"  → {output_dir}")

    # 1. e2e.db row
    run_row = _copy_e2e_run_row(src_e2e_db, dst_e2e_db, run_id, repo_root)
    started_at = run_row["started_at"]
    finished_at = run_row["finished_at"] or started_at
    print(f"  e2e.db: 1 run row ({started_at} → {finished_at})")

    # 2. Base timeline (E2E run rows under negative key)
    base_count = _copy_timeline_rows(
        src_base_timeline,
        dst_base_timeline,
        "issue_number = ?",
        (-run_id,),
        repo_root,
    )
    print(f"  base_timeline.sqlite: {base_count} rows for key {-run_id}")

    # 3. Worktree timeline (positive issue_number rows in window)
    wt_count = _copy_timeline_rows(
        src_worktree_timeline,
        dst_worktree_timeline,
        "issue_number > 0 AND timestamp >= ? AND timestamp <= ?",
        (started_at, finished_at),
        repo_root,
    )
    print(f"  worktree_timeline.sqlite: {wt_count} agent rows in window")

    # 4. Expected results from replaying the matcher
    expected = _build_expected(
        dst_base_timeline, dst_worktree_timeline, run_id, started_at, finished_at,
    )
    dst_expected.write_text(json.dumps(expected, indent=2) + "\n")
    print(
        f"  expected.json: {expected['test_count']} tests, "
        f"{len(expected['distinct_issues_in_window'])} distinct issues"
    )
    matched_tests = sum(1 for t in expected["tests"] if t["issue_numbers"])
    print(f"  → {matched_tests}/{expected['test_count']} test rows have issue numbers")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", type=int, required=True, help="E2E run ID to capture",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=None,
        help="Source repo root (default: walk up from this script)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Fixture output directory (default: tests/fixtures/e2e_runs/run_<id>)",
    )
    args = parser.parse_args()

    repo_root = args.repo_root or _default_repo_root()
    output_dir = args.output or (
        _default_repo_root() / "tests" / "fixtures" / "e2e_runs" / f"run_{args.run_id}"
    )
    snapshot_run(repo_root, args.run_id, output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
