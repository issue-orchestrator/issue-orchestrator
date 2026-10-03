"""Tech-lead exam (#7304): plant a known fault, run the real engine, grade the outcome.

Not part of ``make validate-pr``: it needs GitHub, real agents and (case B) a
real tech-lead model. Run it with ``make test-tech-lead-exam``. Knobs:

* ``E2E_EXAM_ENGINE_REF`` — the commit the ENGINE runs (default ``HEAD``).
  The harness, shims and grader always come from this tree, so pointing the
  engine at a pre-fix commit proves the exam discriminates.
* ``E2E_EXAM_BASE_REF`` — Case U only: the commit the engine is upgraded
  FROM (default ``origin/main``); ``E2E_EXAM_ENGINE_REF`` is the candidate.
* ``E2E_EXAM_TECH_LEAD_MODEL`` — the tech lead's model (default ``opus``, as
  in production).
* ``E2E_EXAM_OUT`` — where scorecards are written (default
  ``/tmp/e2e-orchestrator-logs/exam``).

Each case writes ``<case>-<engine sha>-<time>.json`` (the scorecard) and a
``.txt`` summary, then fails if the scorecard failed — a failing exam names
the stall, which is the point of running it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

import pytest

from issue_orchestrator.infra.config import Config
from issue_orchestrator.testing.exam import render_summary
from issue_orchestrator.testing.exam.cases import (
    BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
    BLOCKED_ITEMS_TRIAGED,
    BLOCK_RESOLUTIONS_PROPOSED,
    BLOCKS_RESOLVED_UNDER_EXECUTE,
    EXAM_CASE_IDS,
    HALTED_EXCHANGE_WITH_VALIDATED_WORK,
    STALE_CLAIM_PAUSED_FOR_RECONCILE,
    UPGRADE_WITH_WORK_IN_FLIGHT,
)
from issue_orchestrator.testing.support.test_data import cleanup_issues_by_label

from tests.e2e.exam.cleanup_steps import run_all_steps
from tests.e2e.exam.recording import run_recorded
from tests.e2e.exam.cleanup import delete_registered_branches, teardown_run
from tests.e2e.exam.run_identity import RunIdentity
from tests.e2e.exam.scenarios import (
    ExamResult,
    ExamRun,
    case_a,
    case_b,
    case_c,
    case_d,
    case_f_resolved,
    case_g_proposed,
    case_u,
    run_case_a,
    run_case_b,
    run_case_c,
    run_case_d,
    run_case_resolution,
    run_case_u,
)
from tests.e2e.flows import E2EFlow

logger = logging.getLogger(__name__)

#: Opt-in, like the other paid live suites: a plain ``make test-e2e`` must not
#: spend a real tech-lead model run. ``make test-tech-lead-exam`` sets it.
EXAM_ENABLED = os.environ.get("E2E_TECH_LEAD_EXAM") == "1"

OUT_DIR = Path(os.environ.get("E2E_EXAM_OUT", "/tmp/e2e-orchestrator-logs/exam"))


def _write(result: ExamResult) -> Path:
    card = result.scorecard
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{card.case_id}-{card.engine_commit[:10]}-{time.strftime('%Y%m%d-%H%M%S')}"
    json_path = OUT_DIR / f"{stem}.json"
    json_path.write_text(card.to_json(), encoding="utf-8")
    (OUT_DIR / f"{stem}.txt").write_text(render_summary(card) + "\n", encoding="utf-8")
    (OUT_DIR / f"{stem}.observation.json").write_text(
        json.dumps(result.observation.to_dict(), indent=2), encoding="utf-8"
    )
    return json_path


def _cleanup(repo: str, run_label: str, flows: list[E2EFlow], branches: list[str]) -> None:
    """Close everything the run touched; see :func:`run_all_steps`."""
    created = [number for flow in flows for number in flow.created_issue_numbers]
    steps: list[tuple[str, Callable[[], object]]] = [
        ("close the run's PRs", lambda: teardown_run(repo, run_label, created)),
        ("delete harness-pushed branches", lambda: delete_registered_branches(repo, branches)),
        *(
            (f"close flow issues {flow.created_issue_numbers}", flow.cleanup_created_issues)
            for flow in flows
        ),
        # Anchors and follow-ups the engine filed carry the run label.
        ("close run-labelled issues", lambda: cleanup_issues_by_label(repo, run_label)),
    ]
    run_all_steps(f"exam cleanup left artifacts for {run_label}", steps)


@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.tech_lead_exam
@pytest.mark.skipif(
    not EXAM_ENABLED,
    reason="Set E2E_TECH_LEAD_EXAM=1 (make test-tech-lead-exam) to run the live tech-lead exam.",
)
@pytest.mark.asyncio
@pytest.mark.timeout(100 * 60)
@pytest.mark.gh_activity_limit(test_gh_activity_limit=5000, system_gh_activity_limit=5000)
@pytest.mark.parametrize(
    "case_id",
    # ``case-<id>`` ids, so ``make test-tech-lead-exam EXAM_CASE=D`` selects
    # with ``-k case-D-``: a bare ``-k D-`` (pytest -k folds case) also
    # matched "paused-for" and "blocked-issue" in the other cases' ids.
    [pytest.param(case_id, id=f"case-{case_id}") for case_id in EXAM_CASE_IDS],
)
async def test_tech_lead_exam(
    case_id: str,
    repo_name: str,
    e2e_project_root: Path,
    e2e_session_config: Config,
) -> None:
    identity = RunIdentity.new(case_id)
    run_label = identity.label
    make_case = {
        HALTED_EXCHANGE_WITH_VALIDATED_WORK: case_a,
        BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW: case_b,
        STALE_CLAIM_PAUSED_FOR_RECONCILE: case_c,
        UPGRADE_WITH_WORK_IN_FLIGHT: case_u,
        BLOCKED_ITEMS_TRIAGED: case_d,
        BLOCKS_RESOLVED_UNDER_EXECUTE: case_f_resolved,
        BLOCK_RESOLUTIONS_PROPOSED: case_g_proposed,
    }[case_id]
    run = ExamRun(
        case=make_case(e2e_session_config),
        repo=repo_name,
        harness_root=e2e_project_root,
        engine_ref=os.environ.get("E2E_EXAM_ENGINE_REF", "HEAD"),
        base_config=e2e_session_config,
        identity=identity,
    )
    flows: list[E2EFlow] = []

    async def run_case() -> ExamResult:
        if case_id == HALTED_EXCHANGE_WITH_VALIDATED_WORK:
            return await run_case_a(run, flows)
        if case_id == STALE_CLAIM_PAUSED_FOR_RECONCILE:
            return await run_case_c(run, flows)
        if case_id == UPGRADE_WITH_WORK_IN_FLIGHT:
            return await run_case_u(
                run, flows, base_ref=os.environ.get("E2E_EXAM_BASE_REF", "origin/main")
            )
        model = os.environ.get("E2E_EXAM_TECH_LEAD_MODEL", "opus")
        if case_id == BLOCKED_ITEMS_TRIAGED:
            return await run_case_d(run, flows, tech_lead_model=model)
        if case_id in (BLOCKS_RESOLVED_UNDER_EXECUTE, BLOCK_RESOLUTIONS_PROPOSED):
            return await run_case_resolution(
                run, flows, tech_lead_model=model,
                resolve_block="execute" if case_id == BLOCKS_RESOLVED_UNDER_EXECUTE else "propose",
            )
        return await run_case_b(run, flows, tech_lead_model=model)

    # The scorecard is written before cleanup, so a GitHub hiccup in cleanup
    # cannot cost the result of an hour-long run (run_recorded).
    result, path = await run_recorded(
        run_case,
        record=_write,
        cleanup=lambda: _cleanup(repo_name, run_label, flows, run.branches),
    )
    card = result.scorecard
    summary = render_summary(card)
    logger.info("[EXAM] scorecard %s\n%s", path, summary)
    print(f"\n{summary}\n  scorecard: {path}", flush=True)
    assert card.passed, f"exam case failed; scorecard {path}\n{summary}"
