"""Tech-lead exam (#7304): plant a known fault, run the real engine, grade the outcome.

Not part of ``make validate-pr``: it needs GitHub, real agents and (case B) a
real tech-lead model. Run it with ``make test-tech-lead-exam``. Knobs:

* ``E2E_EXAM_ENGINE_REF`` — the commit the ENGINE runs (default ``HEAD``).
  The harness, shims and grader always come from this tree, so pointing the
  engine at a pre-fix commit proves the exam discriminates.
* ``E2E_EXAM_TECH_LEAD_MODEL`` — the tech lead's model (default ``opus``, as
  in production).
* ``E2E_EXAM_OUT`` — where scorecards are written (default
  ``/tmp/e2e-orchestrator-logs/exam``).

Each case writes ``<case>-<engine sha>-<time>.json`` (the scorecard) and a
``.txt`` summary, then fails if the scorecard failed — a failing exam names
the stall, which is the point of running it.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from issue_orchestrator.infra.config import Config
from issue_orchestrator.testing.exam import Scorecard, render_summary
from issue_orchestrator.testing.exam.cases import (
    BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
    HALTED_EXCHANGE_WITH_VALIDATED_WORK,
)
from issue_orchestrator.testing.support.test_data import cleanup_issues_by_label

from tests.e2e.conftest import e2e_label
from tests.e2e.exam.scenarios import (
    ExamRun,
    case_a,
    case_b,
    run_case_a,
    run_case_b,
    teardown_items,
)
from tests.e2e.flows import E2EFlow

logger = logging.getLogger(__name__)

#: Opt-in, like the other paid live suites: a plain ``make test-e2e`` must not
#: spend a real tech-lead model run. ``make test-tech-lead-exam`` sets it.
EXAM_ENABLED = os.environ.get("E2E_TECH_LEAD_EXAM") == "1"

OUT_DIR = Path(os.environ.get("E2E_EXAM_OUT", "/tmp/e2e-orchestrator-logs/exam"))


def _write(card: Scorecard) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{card.case_id}-{card.engine_commit[:10]}-{time.strftime('%Y%m%d-%H%M%S')}"
    json_path = OUT_DIR / f"{stem}.json"
    json_path.write_text(card.to_json(), encoding="utf-8")
    (OUT_DIR / f"{stem}.txt").write_text(render_summary(card) + "\n", encoding="utf-8")
    return json_path


def _run_label(case_id: str) -> str:
    """A per-run label: the engine's filter, and an ``io:e2e:`` prefix every
    real engine excludes (``filtering.exclude_label_prefixes``)."""
    return e2e_label(f"exam-{case_id[:1].lower()}-{int(time.time())}")


@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.tech_lead_exam
@pytest.mark.skipif(
    not EXAM_ENABLED,
    reason="Set E2E_TECH_LEAD_EXAM=1 (make test-tech-lead-exam) to run the live tech-lead exam.",
)
@pytest.mark.asyncio
@pytest.mark.timeout(90 * 60)
@pytest.mark.gh_activity_limit(test_gh_activity_limit=5000, system_gh_activity_limit=5000)
@pytest.mark.parametrize(
    "case_id",
    [HALTED_EXCHANGE_WITH_VALIDATED_WORK, BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW],
)
async def test_tech_lead_exam(
    case_id: str,
    repo_name: str,
    e2e_project_root: Path,
    e2e_session_config: Config,
) -> None:
    run_label = _run_label(case_id)
    make_case = case_a if case_id == HALTED_EXCHANGE_WITH_VALIDATED_WORK else case_b
    run = ExamRun(
        case=make_case(e2e_session_config),
        repo=repo_name,
        harness_root=e2e_project_root,
        engine_ref=os.environ.get("E2E_EXAM_ENGINE_REF", "HEAD"),
        base_config=e2e_session_config,
        run_label=run_label,
    )
    flows: list[E2EFlow] = []
    try:
        if case_id == HALTED_EXCHANGE_WITH_VALIDATED_WORK:
            card = await run_case_a(run, flows)
        else:
            card = await run_case_b(
                run,
                flows,
                tech_lead_model=os.environ.get("E2E_EXAM_TECH_LEAD_MODEL", "opus"),
            )
    finally:
        created = [number for flow in flows for number in flow.created_issue_numbers]
        teardown_items(repo_name, created)
        for flow in flows:
            flow.cleanup_created_issues()
        # Anchors and follow-ups the engine filed carry the run label.
        cleanup_issues_by_label(repo_name, run_label)
    path = _write(card)
    summary = render_summary(card)
    logger.info("[EXAM] scorecard %s\n%s", path, summary)
    print(f"\n{summary}\n  scorecard: {path}", flush=True)
    assert card.passed, f"exam case failed; scorecard {path}\n{summary}"
