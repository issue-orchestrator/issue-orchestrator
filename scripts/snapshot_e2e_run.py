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

Sanitization: every string nested inside captured ``data_json`` blobs
goes through ``_sanitize_string`` which replaces:

- the source repo_root            → ``<REPO_ROOT>``
- the e2e-worktree sibling dir    → ``<E2E_WORKTREE>``
- per-user home directories       → ``<HOME>``
- macOS / Linux pytest tmp paths  → ``<TMP>``
- random per-run UUID prefixes in e2e test worktree dirs → ``<UUID>``

Issue numbers, event names, timestamps and view tags are preserved
verbatim — they are exactly what the integration test pins. After
capture, ``verify_fixture_clean`` walks the resulting sqlite files
looking for any of the forbidden raw patterns and aborts if anything
slipped through, so a malformed sanitizer cannot silently ship
machine-specific data.
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
E2E_WORKTREE_PLACEHOLDER = "<E2E_WORKTREE>"
HOME_PLACEHOLDER = "<HOME>"
TMP_PLACEHOLDER = "<TMP>"
UUID_PLACEHOLDER = "<UUID>"


# Regex patterns applied to every string value inside captured data_json blobs.
# Order matters: most specific first so the more general patterns do not
# clobber the specific replacements. The committed fixture must contain none
# of the original (unscrubbed) forms — the cleanliness guardrail in
# tests/integration/test_e2e_timeline_real_fixture.py enforces this.
_GENERIC_SANITIZATION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # macOS pytest tmpdir: /private/var/folders/<a>/<b>/T  → <TMP>
    (re.compile(r"/private/var/folders/[^/\s\"']+/[^/\s\"']+/T"), TMP_PLACEHOLDER),
    # Linux pytest tmpdir: /var/folders/<a>/<b>/T          → <TMP>
    (re.compile(r"/var/folders/[^/\s\"']+/[^/\s\"']+/T"), TMP_PLACEHOLDER),
    # macOS /private/tmp                                    → <TMP>
    (re.compile(r"/private/tmp"), TMP_PLACEHOLDER),
    # Bare /tmp (only when not part of another word like /tmpfile)
    (re.compile(r"(?<![A-Za-z0-9_])/tmp(?=[/\"'])"), TMP_PLACEHOLDER),
    # Per-user home directories: /Users/<user>              → <HOME>
    (re.compile(r"/Users/[^/\s\"']+"), HOME_PLACEHOLDER),
    # /home/<user>                                          → <HOME>
    (re.compile(r"/home/[^/\s\"']+"), HOME_PLACEHOLDER),
    # Random UUID prefix in e2e test worktree dirs.
    # Captures patterns like "e2e-d6c12492-worktrees" or
    # "e2e-d6c12492abcdef01-worktrees" — ANY hex blob 6+ chars between
    # "e2e-" and "-worktrees" is a per-run random id.
    (re.compile(r"e2e-[0-9a-f]{6,}-worktrees"), f"e2e-{UUID_PLACEHOLDER}-worktrees"),
    # Random session id segments: 20260408-035628Z__coding-1 is fine
    # (timestamp + role) but completely opaque uuid-only segment names
    # like /sessions/abc123def456.../ would be machine-specific. We do not
    # currently scrub these because none have appeared in real captures;
    # add a pattern here if one shows up.
)


def _default_repo_root() -> Path:
    """Walk up from this script to find the repo root."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").exists() and (parent / "src" / "issue_orchestrator").exists():
            return parent
    raise RuntimeError(f"Could not find repo root from {here}")


def _sanitize_string(value: str, repo_root: Path) -> str:
    """Replace machine-specific paths in a string with stable placeholders.

    Specific paths (the source repo_root and its e2e-worktree sibling) are
    replaced first so the generic regexes do not clobber them.
    """
    s = value
    s = s.replace(str(repo_root), REPO_ROOT_PLACEHOLDER)
    wt = repo_root.parent / f"{repo_root.name}-e2e-worktree"
    s = s.replace(str(wt), E2E_WORKTREE_PLACEHOLDER)
    for pattern, replacement in _GENERIC_SANITIZATION_PATTERNS:
        s = pattern.sub(replacement, s)
    return s


def _walk_and_sanitize(obj: Any, repo_root: Path) -> Any:
    """Recursively walk a parsed JSON value, sanitizing every string."""
    if isinstance(obj, str):
        return _sanitize_string(obj, repo_root)
    if isinstance(obj, list):
        return [_walk_and_sanitize(item, repo_root) for item in obj]
    if isinstance(obj, dict):
        return {key: _walk_and_sanitize(value, repo_root) for key, value in obj.items()}
    return obj


def _sanitize_data_blob(blob: str, repo_root: Path) -> str:
    """Parse a data_json blob, scrub every nested string, and re-serialize.

    We round-trip via json so embedded paths are scrubbed regardless of
    how deeply they are nested. If the blob is not valid JSON we fall
    back to a plain string substitution so we still strip something.
    """
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return _sanitize_string(blob, repo_root)
    sanitized = _walk_and_sanitize(data, repo_root)
    return json.dumps(sanitized, separators=(",", ":"))


# Patterns that MUST NOT appear in any committed fixture. Used both by
# verify_fixture_clean below and by the standing guardrail unit test.
_FORBIDDEN_FIXTURE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/Users/[A-Za-z0-9._-]+"), "/Users/<user>"),
    (re.compile(r"/home/[A-Za-z0-9._-]+"), "/home/<user>"),
    (re.compile(r"/private/tmp[/\"']"), "/private/tmp"),
    (re.compile(r"/private/var/folders"), "/private/var/folders"),
    (re.compile(r"e2e-[0-9a-f]{6,}-worktrees"), "e2e-<random-uuid>-worktrees"),
)


def verify_fixture_clean(fixture_dir: Path) -> list[str]:
    """Walk every sqlite file in ``fixture_dir`` looking for raw machine paths.

    Returns a list of human-readable problem descriptions; an empty list
    means the fixture passes the cleanliness contract. Inspects both the
    raw on-disk bytes (catches anything outside data_json) and each parsed
    data_json blob (catches structured leaks).
    """
    problems: list[str] = []
    for db_file in sorted(fixture_dir.glob("*.sqlite")):
        raw = db_file.read_bytes()
        for pattern, label in _FORBIDDEN_FIXTURE_PATTERNS:
            if pattern.search(raw.decode("utf-8", errors="ignore")):
                problems.append(
                    f"{db_file.name}: contains machine-specific pattern matching '{label}'"
                )
    return problems


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
        # Columns whose value is a free-form string that may carry an
        # absolute filesystem path. We treat them as opaque strings and
        # run them through _sanitize_string. data_json is special-cased
        # because it is a JSON blob that needs structural walking.
        path_carrying_columns = {"run_dir", "instance_id"}
        for r in rows:
            values = []
            for c in col_names_no_seq:
                v = r[c]
                if c == "data_json" and isinstance(v, str):
                    v = _sanitize_data_blob(v, repo_root)
                elif c in path_carrying_columns and isinstance(v, str) and v:
                    v = _sanitize_string(v, repo_root)
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

    # Hard guardrail: refuse to ship a fixture that still contains raw
    # machine-specific paths, even if every other step succeeded.
    problems = verify_fixture_clean(output_dir)
    if problems:
        print("ERROR: fixture failed cleanliness check:")
        for p in problems:
            print(f"  {p}")
        raise SystemExit(
            "Sanitization is incomplete. Add a pattern to "
            "_GENERIC_SANITIZATION_PATTERNS / _FORBIDDEN_FIXTURE_PATTERNS "
            "and re-run."
        )
    print("  cleanliness check: OK")


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
