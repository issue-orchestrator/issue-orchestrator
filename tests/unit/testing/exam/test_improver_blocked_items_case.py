"""Improver exam case IM1 at the validator level (#7490 step 4).

The deterministic half of the case: the staged fixture is the shape porchpin
showed when the improver's first blind run accepted zero findings. The
validator must refuse that answer (no blocked item may be silently dropped),
refuse the dismissals the blind run used, and accept the right one, which
the grader then passes. The live half runs the real model on the same
fixture (``tests/e2e/test_improver_exam.py``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.domain.improver_findings_validation import (
    ImproverFindingsRejected,
    Rule,
    StagedEvidence,
    validate_findings,
)
from issue_orchestrator.entrypoints.improver_staging import load_staged_evidence
from issue_orchestrator.infra.config import Config
from issue_orchestrator.testing.exam.improver_blocked_items import (
    AUDITED_REPO,
    CASE_FILE_364,
    CUTOFF,
    ENGINE_ID,
    STARTED,
    build_case,
    grade,
)

COMMIT = "027aee2ca8d32eee3ee2c083c15ba72ff30317e7"
SOURCE_FILE = "src/issue_orchestrator/control/completion_pr_labels.py"


def _source(destination: Path) -> None:
    path = destination / SOURCE_FILE
    path.parent.mkdir(parents=True)
    path.write_text("# the reserved-label door\n", encoding="utf-8")


@pytest.fixture(scope="module")
def evidence(tmp_path_factory: pytest.TempPathFactory) -> StagedEvidence:
    data = build_case(
        tmp_path_factory.mktemp("im1"), engine_commit=COMMIT,
        charter=TechLeadCharterPolicy.from_config(Config()).effective_charter(),
        export_source=_source,
    )
    return load_staged_evidence(data)


def _snapshot(index: int) -> dict:
    return {"at": CUTOFF.isoformat(), "kind": "snapshot", "source": f"audit.json#/anomalies/{index}",
            "supports": "present_after_start"}


def _remedy(owner: str) -> dict:
    return {
        "root_cause": {"owner": owner, "why": "w", "same_shape_sites": []},
        "reproduction": {
            "kind": "unit_test", "harness": "tests/unit/control/test_x.py", "planted_state": "s",
            "assertions": ["fails today"], "fails_on": COMMIT,
        },
        "proposal": "p",
    }


def reference() -> dict:
    """The right answer: every blocked item accounted for by a finding."""
    engine = {"id": ENGINE_ID, "repo": AUDITED_REPO}
    published = {
        "id": "recovery-publish-refused-on-reserved-pr-label",
        "engine": engine,
        "anomaly_keys": [
            {"kind": "parked_action", "subject": "validated_work:r1:d953bc782eaae5b5",
             "signature": "recover_validated_work:ed7763482019c4d04bb89a7deff8c557"},
            {"kind": "attention_label", "subject": "#364", "signature": "needs-human"},
            {"kind": "attention_label", "subject": "#364", "signature": "recovery-pending"},
        ],
        "present_after_start": "true", "recurs_after_start": "unknown", "origin": "before_start",
        "observed": [
            _snapshot(0), _snapshot(3), _snapshot(4),
            {"at": "2026-10-02T05:24:50+00:00", "kind": "occurrence",
             "source": "audit.json#/action_liveness/parked/0/last_failed_at", "supports": "origin"},
            {"at": "2026-10-02T05:05:22+00:00", "kind": "occurrence",
             "source": "blocked-items.json#/items/2/blocking_labels/1/since_at", "supports": "origin"},
        ],
        "grading_window": {"from": "2026-10-02T05:05:22+00:00", "to": CUTOFF.isoformat()},
        "classification": "new_defect",
        # Diagnosed in a case file, never fixed: a diagnosis is not a remedy.
        "stall_point": "noticed_not_acted",
        "stall_evidence": [CASE_FILE_364],
        "output": "capability_issue",
        **_remedy("issue_orchestrator.control.completion_pr_labels:validate_pr_labels"),
    }
    untriaged = {
        "id": "blocked-items-never-triaged",
        "engine": engine,
        "anomaly_keys": [
            {"kind": "attention_label", "subject": "#262", "signature": "needs-human"},
            {"kind": "attention_label", "subject": "#326", "signature": "needs-human"},
            {"kind": "attention_label", "subject": "#326", "signature": "blocked-cross-milestone"},
        ],
        "present_after_start": "true", "recurs_after_start": "unknown", "origin": "before_start",
        "observed": [
            _snapshot(1), _snapshot(2), _snapshot(5),
            {"at": "2026-09-23T05:28:29+00:00", "kind": "occurrence",
             "source": "blocked-items.json#/items/1/blocking_labels/0/since_at", "supports": "origin"},
        ],
        "grading_window": {"from": "2026-09-23T05:28:29+00:00", "to": CUTOFF.isoformat()},
        "classification": "new_defect",
        "stall_point": "unknown",
        "stall_evidence": [],
        "output": "prompt_proposal",
        **_remedy("examples/prompts/tech-lead.md"),
    }
    return {
        "schema_version": 5, "engine_commit": COMMIT, "engine_started_at": STARTED.isoformat(), "design_findings": [],
        "findings": [published, untriaged],
        "blocked_items": [
            {"number": 262, "disposition": "finding", "finding_id": "blocked-items-never-triaged", "why": "w"},
            {"number": 326, "disposition": "finding", "finding_id": "blocked-items-never-triaged", "why": "w"},
            {"number": 364, "disposition": "finding",
             "finding_id": "recovery-publish-refused-on-reserved-pr-label", "why": "w"},
        ],
        "trend": {"exam_scores": "unobserved", "operator_interventions": "unobserved", "notes": ""},
    }


def _rejection(doc: dict, evidence: StagedEvidence) -> ImproverFindingsRejected:
    with pytest.raises(ImproverFindingsRejected) as rejected:
        validate_findings(json.dumps(doc), evidence)
    return rejected.value


def test_the_right_answer_is_accepted_and_passes(evidence: StagedEvidence) -> None:
    result = grade(validate_findings(json.dumps(reference()), evidence))

    assert result.passed, result.failures
    assert result.stalls == {262: "unknown", 326: "unknown", 364: "noticed_not_acted"}


def test_the_first_blind_runs_answer_is_refused_for_dropping_every_blocked_item(evidence: StagedEvidence) -> None:
    """Handover grade #1: zero findings, the items dismissed as history."""
    blind = {**reference(), "findings": [], "blocked_items": []}

    rejected = _rejection(blind, evidence)

    assert rejected.rules == {Rule.BLOCKED_ITEMS_ACCOUNTED}
    assert "#262, #326, #364" in str(rejected)


def test_a_case_file_is_not_a_hand_over(evidence: StagedEvidence) -> None:
    """"Already diagnosed in a case file" dismissed #364: a diagnosis with no
    applied remedy or escalation is a finding, never a disposition."""
    doc = reference()
    doc["findings"] = doc["findings"][1:]
    doc["blocked_items"][2] = {"number": 364, "disposition": "awaiting_operator",
                               "evidence": [CASE_FILE_364], "why": "already diagnosed"}

    assert Rule.BLOCKED_ITEM_HANDED_OVER in _rejection(doc, evidence).rules


def test_a_block_from_before_the_start_still_present_is_live(evidence: StagedEvidence) -> None:
    """"Originated before this engine start" dismissed #364: still present
    after the start is live, whatever its origin."""
    doc = reference()
    doc["findings"][0]["present_after_start"] = "false"

    assert Rule.BLOCKED_ITEM_FINDING_ABOUT_IT in _rejection(doc, evidence).rules


def test_an_untriaged_item_cannot_be_graded_noticed(evidence: StagedEvidence) -> None:
    """Zero tech-lead records about #262 or #326: no notice grade is possible."""
    doc = reference()
    doc["findings"][1]["stall_point"] = "noticed_not_acted"
    doc["findings"][1]["stall_evidence"] = [CASE_FILE_364]

    assert Rule.STALL_EVIDENCE_ABOUT_THE_ANOMALY in _rejection(doc, evidence).rules


def test_the_grader_fails_an_accepted_answer_with_the_wrong_stall_point(evidence: StagedEvidence) -> None:
    doc = copy.deepcopy(reference())
    doc["findings"][0]["stall_point"] = "unknown"
    doc["findings"][0]["stall_evidence"] = []

    result = grade(validate_findings(json.dumps(doc), evidence))

    assert not result.passed
    assert result.failures == ("#364: graded unknown; expected noticed_not_acted",)


def test_a_pre_start_block_onset_makes_the_origin_before_start(evidence: StagedEvidence) -> None:
    """The item's onset (blocked-items.json ``since_at``) is a dated record."""
    doc = reference()
    doc["findings"][1]["origin"] = "unknown"
    doc["findings"][1]["observed"] = doc["findings"][1]["observed"][:3]
    doc["findings"][1]["grading_window"]["from"] = "unknown"

    assert Rule.ORIGIN_MATCHES_PRE_START_OCCURRENCE in _rejection(doc, evidence).rules
