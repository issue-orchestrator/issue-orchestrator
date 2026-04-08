"""Replay captured E2E runs through the live timeline endpoints.

These tests load sanitized snapshots of real ``/api/e2e-run-detail/{id}``
inputs (E2E run records + worktree agent events + e2e.db row) and assert
the response matches a captured ``expected.json`` of test → issue_numbers
mappings.

The point is to catch bugs that synthetic unit tests miss because the
test author cannot anticipate every realistic event shape — for example,
the view-filter regression that silently dropped issues whose only
in-window events were debug-tagged.

Adding a new fixture = adding new coverage. To capture a fixture from
a real run::

    .venv/bin/python scripts/snapshot_e2e_run.py --run-id 87 \
        --repo-root /path/to/checkout

If a captured fixture begins to fail because the contract LEGITIMATELY
changed (matcher logic, view-model shape), re-run the snapshot script
against the same run to bless the new expectations.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.web import app, set_orchestrator
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "e2e_runs"


def _discover_fixtures() -> list[Path]:
    """Find all fixture directories under tests/fixtures/e2e_runs/."""
    if not FIXTURE_DIR.exists():
        return []
    return sorted(
        d for d in FIXTURE_DIR.iterdir()
        if d.is_dir() and (d / "expected.json").exists()
    )


def _stage_fixture_layout(fixture: Path, tmp_path: Path) -> Path:
    """Copy a fixture into a tmp_path layout that the live endpoint accepts.

    The endpoint resolves the worktree timeline via
    ``get_e2e_worktree_path(repo_root)`` which expects a sibling directory
    named ``<repo_name>-e2e-worktree``. We honour that contract here so the
    real production code path runs unmodified against the fixture data.
    """
    repo_root = tmp_path / "repo"
    (repo_root / ".issue-orchestrator" / "state").mkdir(parents=True)
    wt_state = tmp_path / "repo-e2e-worktree" / ".issue-orchestrator" / "state"
    wt_state.mkdir(parents=True)

    shutil.copy(fixture / "e2e.db", repo_root / ".issue-orchestrator" / "e2e.db")
    shutil.copy(
        fixture / "base_timeline.sqlite",
        repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
    )
    shutil.copy(
        fixture / "worktree_timeline.sqlite",
        wt_state / "timeline.sqlite",
    )
    return repo_root


@pytest.mark.parametrize(
    "fixture",
    _discover_fixtures(),
    ids=lambda f: f.name,
)
def test_e2e_run_detail_matches_captured_fixture(fixture: Path, tmp_path: Path) -> None:
    """Replay a captured E2E run through /api/e2e-run-detail and diff vs ground truth.

    For each fixture under ``tests/fixtures/e2e_runs/``, we stage a real
    repo_root + e2e-worktree layout in ``tmp_path``, point a mock
    orchestrator at it, hit the production endpoint via TestClient, and
    assert each test row's ``issue_numbers`` matches ``expected.json``.

    A failure here means either:
      a) the matcher / reader / view-model pipeline regressed, OR
      b) the contract LEGITIMATELY changed and the fixture needs to be
         re-blessed via ``scripts/snapshot_e2e_run.py``.

    A diff in the assertion error tells you exactly which test row drifted.
    """
    expected = json.loads((fixture / "expected.json").read_text())
    run_id = expected["run_id"]

    repo_root = _stage_fixture_layout(fixture, tmp_path)

    # Build a real SqliteTimelineStore over the staged base timeline so the
    # endpoint reads E2E run records (negative issue_number) the same way
    # production does.
    base_store = SqliteTimelineStore(
        db_path=repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
    )

    mock_orch = MagicMock()
    mock_orch.config.repo_root = repo_root
    mock_orch.deps.timeline_store = base_store

    set_orchestrator(mock_orch)
    try:
        client = TestClient(app)
        response = client.get(f"/api/e2e-run-detail/{run_id}")
        assert response.status_code == 200, (
            f"Expected 200 from /api/e2e-run-detail/{run_id}, got "
            f"{response.status_code}: {response.text[:300]}"
        )
        payload = response.json()
        events = payload.get("events", [])

        # Build (nodeid -> issue_numbers) from the response, taking
        # test_started events as the canonical row identity.
        actual: dict[str, list[int]] = {}
        for evt in events:
            if evt.get("event") != "e2e.test_started":
                continue
            nodeid = evt.get("nodeid", "")
            if not nodeid:
                continue
            actual[nodeid] = sorted(evt.get("issue_numbers") or [])

        # Diff every expected row.
        mismatches: list[str] = []
        for entry in expected["tests"]:
            nodeid = entry["nodeid"]
            want = sorted(entry["issue_numbers"])
            got = actual.get(nodeid)
            if got is None:
                mismatches.append(
                    f"  MISSING test_started for {nodeid} (expected issues={want})"
                )
            elif got != want:
                mismatches.append(f"  {nodeid}\n    expected={want}\n    actual  ={got}")

        # Also catch unexpected new test rows so the fixture stays canonical.
        expected_nodeids = {t["nodeid"] for t in expected["tests"]}
        unexpected = sorted(set(actual.keys()) - expected_nodeids)
        for nodeid in unexpected:
            mismatches.append(
                f"  UNEXPECTED test_started for {nodeid} (issues={actual[nodeid]})"
            )

        assert not mismatches, (
            f"Fixture {fixture.name} drift "
            f"({len(mismatches)} row(s)):\n" + "\n".join(mismatches) +
            "\n\nIf this is a legitimate contract change, re-bless via:"
            f"\n  scripts/snapshot_e2e_run.py --run-id {run_id} --repo-root <path>"
        )
    finally:
        set_orchestrator(None)


def test_at_least_one_fixture_exists() -> None:
    """Sanity check: there must be at least one captured fixture."""
    fixtures = _discover_fixtures()
    assert fixtures, (
        "No E2E run fixtures found. Capture one with:\n"
        "  scripts/snapshot_e2e_run.py --run-id <id> --repo-root <path>"
    )
