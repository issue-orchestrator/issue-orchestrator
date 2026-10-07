"""Improver exam cases IM1 and IM2, live: the real improver model on each planted fixture (#7490 step 4).

Not part of ``make validate-pr``: it spends a real improver model run (tens
of minutes). Run it with ``make test-improver-exam``. Knobs:

* ``E2E_IMPROVER_EXAM_PROVIDER`` / ``E2E_IMPROVER_EXAM_MODEL`` — the
  improver's provider (``claude`` or ``codex``) and model (default: the
  improver's default agent, as ``make tech-lead-improver`` runs it, #8001);
* ``E2E_IMPROVER_EXAM_ENGINE_REF`` — the io commit staged as the engine's
  source (default ``HEAD``); the prompt is always this tree's;
* ``E2E_IMPROVER_EXAM_OUT`` — where each run's directory (the staged inputs,
  the agent's log, its findings and the grade) is kept.

The improver must produce a findings file the validator ACCEPTS, and the
case's grader must pass it: for IM1
(:mod:`~issue_orchestrator.testing.exam.improver_blocked_items`) every
ignored blocked item a finding, #364 graded ``noticed_not_acted``; for IM2
(:mod:`~issue_orchestrator.testing.exam.improver_downstream_stall`) the review
#364's block vetoes on every scan keyed in a live finding. The
validator-level halves are ``test_improver_blocked_items_case.py`` and
``test_improver_downstream_stall_case.py``, which run in the gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from issue_orchestrator.contracts.improver_findings import FINDINGS_FILE
from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.domain.improver_findings_validation import validate_findings
from issue_orchestrator.entrypoints.improver_run import findings_text
from issue_orchestrator.entrypoints.improver_staging import load_staged_evidence
from issue_orchestrator.contracts.improver_run import (
    DEFAULT_IMPROVER_AGENT,
    ImproverAgentChoice,
    ImproverProvider,
)
from issue_orchestrator.execution.improver_agents import improver_agent
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.engine_source_archive import GitEngineSourceArchive
from issue_orchestrator.execution.process_group_command_runner import ProcessGroupCommandRunner
from issue_orchestrator.infra.config import Config
from issue_orchestrator.testing.exam import improver_blocked_items, improver_downstream_stall

#: Opt-in, like the live tech-lead exam: a plain test run must not spend a
#: real improver model run. ``make test-improver-exam`` sets it.
ENABLED = os.environ.get("E2E_IMPROVER_EXAM") == "1"
OUT_DIR = Path(os.environ.get("E2E_IMPROVER_EXAM_OUT", "/tmp/e2e-orchestrator-logs/improver-exam"))
REPO = Path(__file__).resolve().parents[4]
PROMPT = REPO / "examples" / "prompts" / "tech-lead-improver.md"


@pytest.mark.live_agent
@pytest.mark.improver_exam
@pytest.mark.skipif(
    not ENABLED, reason="Set E2E_IMPROVER_EXAM=1 (make test-improver-exam) to run the live improver exam."
)
@pytest.mark.timeout(100 * 60)
@pytest.mark.parametrize(
    "case", [improver_blocked_items, improver_downstream_stall], ids=lambda c: c.CASE_ID
)
def test_improver_exam(case) -> None:
    ref = os.environ.get("E2E_IMPROVER_EXAM_ENGINE_REF", "HEAD")
    commit = subprocess.run(
        ["git", "rev-parse", f"{ref}^{{commit}}"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.strip()
    run_dir = OUT_DIR / f"{case.CASE_ID}-{commit[:10]}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True)
    data = case.build_case(
        run_dir,
        engine_commit=commit,
        charter=TechLeadCharterPolicy.from_config(Config()).effective_charter(),
        export_source=lambda destination: GitEngineSourceArchive(REPO, LocalCommandRunner()).export(
            commit, destination
        ),
    )
    provider = ImproverProvider(os.environ.get("E2E_IMPROVER_EXAM_PROVIDER", DEFAULT_IMPROVER_AGENT.provider))
    agent = improver_agent(
        ImproverAgentChoice.for_provider(provider, os.environ.get("E2E_IMPROVER_EXAM_MODEL")),
        runner=ProcessGroupCommandRunner(),
        timeout_seconds=90 * 60,
    )

    answer = agent.run(
        prompt=f"ISSUE_ORCHESTRATOR_RUN_DIR={run_dir}\n\n{PROMPT.read_text(encoding='utf-8')}",
        run_dir=run_dir,
        toolbox=None,
        heat=1,
    )

    assert answer.final_message is not None, f"the improver did not finish: {answer.detail}; see {run_dir}"
    text = findings_text(answer.final_message)
    (run_dir / FINDINGS_FILE).write_text(text, encoding="utf-8")
    # A rejection raises, naming every rule the improver's answer broke.
    result = case.grade(validate_findings(text, load_staged_evidence(data)))
    (run_dir / "grade.json").write_text(
        json.dumps({"case_id": result.case_id, "passed": result.passed, "failures": list(result.failures),
                    "stalls": {str(k): v for k, v in getattr(result, "stalls", {}).items()}}, indent=2),
        encoding="utf-8",
    )
    print(f"\n[IMPROVER EXAM] {case.CASE_ID}: {'PASSED' if result.passed else 'FAILED'}; run {run_dir}", flush=True)
    assert result.passed, f"improver exam failed: {result.failures}; run {run_dir}"
