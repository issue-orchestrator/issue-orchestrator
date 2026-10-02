"""Improver exam case IM1, live: the real improver model on the planted fixture (#7490 step 4).

Not part of ``make validate-pr``: it spends a real Codex run (tens of
minutes). Run it with ``make test-improver-exam``. Knobs:

* ``E2E_IMPROVER_EXAM_MODEL`` — the improver's model (default ``gpt-5.6-sol``,
  as ``make tech-lead-improver`` runs it);
* ``E2E_IMPROVER_EXAM_ENGINE_REF`` — the io commit staged as the engine's
  source (default ``HEAD``); the prompt is always this tree's;
* ``E2E_IMPROVER_EXAM_OUT`` — where each run's directory (the staged inputs,
  the agent's log, its findings and the grade) is kept.

The improver must produce a findings file the validator ACCEPTS, and the
grader (:func:`~issue_orchestrator.testing.exam.improver_blocked_items.grade`)
must pass it: every ignored blocked item a finding, #364 graded
``noticed_not_acted``. The validator-level half is
``test_improver_blocked_items_case.py``, which runs in the gate.
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
from issue_orchestrator.execution.codex_improver_agent import CodexImproverAgent
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.engine_source_archive import GitEngineSourceArchive
from issue_orchestrator.execution.process_group_command_runner import ProcessGroupCommandRunner
from issue_orchestrator.infra.config import Config
from issue_orchestrator.testing.exam.improver_blocked_items import CASE_ID, build_case, grade

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
def test_improver_exam_ignored_blocked_items() -> None:
    ref = os.environ.get("E2E_IMPROVER_EXAM_ENGINE_REF", "HEAD")
    commit = subprocess.run(
        ["git", "rev-parse", f"{ref}^{{commit}}"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.strip()
    run_dir = OUT_DIR / f"{CASE_ID}-{commit[:10]}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True)
    data = build_case(
        run_dir,
        engine_commit=commit,
        charter=TechLeadCharterPolicy.from_config(Config()).effective_charter(),
        export_source=lambda destination: GitEngineSourceArchive(REPO, LocalCommandRunner()).export(
            commit, destination
        ),
    )
    agent = CodexImproverAgent(
        runner=ProcessGroupCommandRunner(),
        model=os.environ.get("E2E_IMPROVER_EXAM_MODEL", "gpt-5.6-sol"),
        timeout_seconds=90 * 60,
    )

    answer = agent.run(
        prompt=f"ISSUE_ORCHESTRATOR_RUN_DIR={run_dir}\n\n{PROMPT.read_text(encoding='utf-8')}", run_dir=run_dir
    )

    assert answer.final_message is not None, f"the improver did not finish: {answer.detail}; see {run_dir}"
    text = findings_text(answer.final_message)
    (run_dir / FINDINGS_FILE).write_text(text, encoding="utf-8")
    # A rejection raises, naming every rule the improver's answer broke.
    result = grade(validate_findings(text, load_staged_evidence(data)))
    (run_dir / "grade.json").write_text(
        json.dumps({"case_id": result.case_id, "passed": result.passed, "failures": list(result.failures),
                    "stalls": {str(k): v for k, v in result.stalls.items()}}, indent=2),
        encoding="utf-8",
    )
    print(f"\n[IMPROVER EXAM] {CASE_ID}: {'PASSED' if result.passed else 'FAILED'}; run {run_dir}", flush=True)
    assert result.passed, f"improver exam failed: {result.failures}; run {run_dir}"
