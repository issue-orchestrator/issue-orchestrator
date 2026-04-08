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


def _stage_fixture_with_real_timeline_reader(
    fixture: Path, tmp_path: Path,
):
    """Stage a fixture and return (repo_root, mock_orchestrator).

    The orchestrator gets a real ``timeline_reader`` over the staged base
    timeline so the click-through path through ``get_issue_detail`` runs
    against actual on-disk data.
    """
    from unittest.mock import MagicMock
    from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader
    from issue_orchestrator.execution.timeline_store import SqliteTimelineStore

    repo_root = _stage_fixture_layout(fixture, tmp_path)
    base_store = SqliteTimelineStore(
        db_path=repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
    )

    mock_orch = MagicMock()
    mock_orch.config.repo_root = repo_root
    mock_orch.deps.timeline_store = base_store
    mock_orch.deps.timeline_reader = DefaultTimelineReader(base_store)
    return repo_root, mock_orch


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

    repo_root, mock_orch = _stage_fixture_with_real_timeline_reader(fixture, tmp_path)

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


@pytest.mark.parametrize(
    "fixture",
    _discover_fixtures(),
    ids=lambda f: f.name,
)
def test_issue_affordance_click_through_returns_real_events(
    fixture: Path, tmp_path: Path,
) -> None:
    """Sequenced HTTP commands: timeline → click → issue detail.

    Pins the full navigation contract a user follows when they click
    a ``#N`` issue affordance in the E2E run drawer:

    1. GET /api/e2e-run-detail/{run_id}     → discover issue numbers
    2. for each issue number with affordances, GET /api/issue-detail/{N}
       → assert the click-through resolves to a payload with real events

    Without the e2e-worktree fallback in get_issue_detail, step 2
    returns ``events: []`` for ephemeral E2E issues whose orchestrator
    activity lives only in the worktree timeline (the regression that
    prompted this assertion). The drawer technically opens, but the
    affordance is useless.

    This is the "command pattern" alternative to the Playwright browser
    test: it exercises the exact HTTP path the dashboard JS follows on
    click, but without launching Chromium. Both layers should pass.
    """
    expected = json.loads((fixture / "expected.json").read_text())
    run_id = expected["run_id"]

    # Pick the first issue number that the matcher attached to any test row.
    # Skip fixtures that have no agent activity (e.g. run_86 — pruned worktree).
    click_through_targets: list[int] = []
    for entry in expected["tests"]:
        for n in entry.get("issue_numbers", []):
            if n not in click_through_targets:
                click_through_targets.append(n)
    if not click_through_targets:
        pytest.skip(f"Fixture {fixture.name} has no issue affordances to click")

    repo_root, mock_orch = _stage_fixture_with_real_timeline_reader(fixture, tmp_path)

    set_orchestrator(mock_orch)
    try:
        client = TestClient(app)

        # Command 1: pull the run drawer payload (matches what
        # showUnifiedRunView fetches in dashboard.js).
        run_response = client.get(f"/api/e2e-run-detail/{run_id}")
        assert run_response.status_code == 200
        run_payload = run_response.json()

        # Verify the run payload actually contains the affordances we
        # plan to click — guards against the test silently passing if
        # the matcher regresses.
        rendered_issue_nums: set[int] = set()
        for evt in run_payload.get("events", []):
            for n in evt.get("issue_numbers") or []:
                rendered_issue_nums.add(n)
        for target in click_through_targets:
            assert target in rendered_issue_nums, (
                f"Run payload is missing issue {target}; the matcher dropped "
                f"an affordance the click-through test was about to verify."
            )

        # Command 2: simulate clicking each affordance. The dashboard JS
        # calls /api/issue-detail/{N}?view=user, which lands on the web
        # app's get_issue_detail. That endpoint MUST surface real events
        # for the issue — an empty payload means the drawer opens to a
        # useless "no activity" view.
        broken: list[str] = []
        for issue_num in click_through_targets:
            detail_response = client.get(
                f"/api/issue-detail/{issue_num}", params={"view": "user"},
            )
            if detail_response.status_code != 200:
                broken.append(
                    f"  #{issue_num}: HTTP {detail_response.status_code}"
                )
                continue
            detail = detail_response.json()
            if detail.get("issue_number") != issue_num:
                broken.append(
                    f"  #{issue_num}: payload issue_number = "
                    f"{detail.get('issue_number')!r}"
                )
                continue
            events = detail.get("events") or []
            if not events:
                broken.append(
                    f"  #{issue_num}: drawer opened but events=[]; "
                    f"e2e-worktree fallback in get_issue_detail likely "
                    f"missing. Click-through is useless for this issue."
                )

        assert not broken, (
            f"Click-through navigation broken for {len(broken)} issue(s) in "
            f"fixture {fixture.name}:\n" + "\n".join(broken)
        )
    finally:
        set_orchestrator(None)


@pytest.mark.parametrize(
    "fixture",
    _discover_fixtures(),
    ids=lambda f: f.name,
)
def test_fixture_contains_no_machine_specific_paths(fixture: Path) -> None:
    """Standing guardrail: every committed fixture must be sanitized.

    Walks the fixture's sqlite files and asserts none of them carry raw
    ``/Users/<user>``, ``/private/tmp``, ``/private/var/folders``, or
    ``e2e-<random-uuid>-worktrees`` patterns. The same check runs as a
    hard guardrail at the end of ``scripts/snapshot_e2e_run.py``, but
    pinning it here as well means a developer adding a fixture by hand
    (or with a stale snapshot script) cannot bypass the contract.

    If this fails, re-snapshot the fixture::

        scripts/snapshot_e2e_run.py --run-id <id> --repo-root <path>

    Or, if a new path family needs scrubbing, add a pattern to
    ``_GENERIC_SANITIZATION_PATTERNS`` and ``_FORBIDDEN_FIXTURE_PATTERNS``
    in ``scripts/snapshot_e2e_run.py`` and re-snapshot.
    """
    # Import lazily so this test does not require scripts/ to be on
    # sys.path at collection time.
    import importlib.util

    script_path = (
        Path(__file__).parent.parent.parent / "scripts" / "snapshot_e2e_run.py"
    )
    spec = importlib.util.spec_from_file_location("snapshot_e2e_run", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    problems = module.verify_fixture_clean(fixture)
    assert not problems, (
        f"Fixture {fixture.name} contains machine-specific data:\n"
        + "\n".join(f"  - {p}" for p in problems)
    )
