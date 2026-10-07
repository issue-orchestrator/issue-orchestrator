"""Improver exam case IM2 at the validator level (#7490 step 4).

The deterministic half of the case: the fixture is the REAL audit and
blocked-items assembly over porchpin's raw records of 2026-10-02 (#364
blocked, its PR #379's review dropped on every scan, all logged at INFO).
The blind run's answer graded only the block. It must be refused, and fail
the grade; an answer that keys the refused review is accepted and passes.
The live half runs the real model on the same fixture
(``test_improver_exam_live.py``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from issue_orchestrator.contracts.improver_findings import ImproverFindings
from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.domain.improver_findings_validation import (
    ImproverFindingsRejected,
    Rule,
    StagedEvidence,
    validate_findings,
)
from issue_orchestrator.entrypoints.improver_staging import load_staged_evidence
from issue_orchestrator.infra.config import Config
from issue_orchestrator.testing.exam.improver_downstream_stall import (
    AUDITED_REPO,
    CUTOFF,
    DIAGNOSIS_364,
    ENGINE_ID,
    STARTED,
    build_case,
    grade,
)

COMMIT = "387f107334f528d78476cfba5457a7cd7421f3f1"
SOURCE_FILE = "src/issue_orchestrator/control/review_validity.py"


def _source(destination: Path) -> None:
    path = destination / SOURCE_FILE
    path.parent.mkdir(parents=True)
    path.write_text("# a PR's review is invalid while its issue is blocked\n", encoding="utf-8")


@pytest.fixture(scope="module")
def evidence(tmp_path_factory: pytest.TempPathFactory) -> StagedEvidence:
    data = build_case(
        tmp_path_factory.mktemp("im2"), engine_commit=COMMIT,
        charter=TechLeadCharterPolicy.from_config(Config()).effective_charter(),
        export_source=_source,
    )
    return load_staged_evidence(data)


def _index(evidence: StagedEvidence, kind: str) -> int:
    return next(i for i, a in enumerate(evidence.audit.anomalies) if a.kind.value == kind)


def _snapshot(index: int) -> dict:
    return {"at": CUTOFF.isoformat(), "kind": "snapshot", "source": f"audit.json#/anomalies/{index}",
            "supports": "present_after_start"}


_ENGINE = {"id": ENGINE_ID, "repo": AUDITED_REPO}
_NEEDS_HUMAN = {"kind": "attention_label", "subject": "#364", "signature": "needs-human"}
_VETO = {"kind": "refused_work", "subject": "PR #379", "signature": "review:issue_blocked"}
_SINCE = {"at": "2026-10-02T05:24:57+00:00", "kind": "occurrence",
          "source": "blocked-items.json#/items/0/blocking_labels/0/since_at", "supports": "origin"}


def blind(evidence: StagedEvidence) -> dict:
    """What run 20261002T120316Z-1b2115c1 answered for #364: the block, graded
    as a question the tech lead diagnosed and never handed over. Nothing
    about the PR whose review the block vetoes."""
    finding = {
        "id": "blocked-364-operator-decision-left-as-advice",
        "engine": _ENGINE,
        "anomaly_keys": [_NEEDS_HUMAN],
        "present_after_start": "true", "recurs_after_start": "unknown", "origin": "before_start",
        "observed": [_snapshot(_index(evidence, "attention_label")), _SINCE],
        "grading_window": {"from": "unknown", "to": CUTOFF.isoformat()},
        "classification": "unknown",
        "stall_point": "noticed_not_acted",
        "stall_evidence": [DIAGNOSIS_364],
        "output": "needs_investigation",
        "missing_evidence": ["why the health review produced advice instead of a binding hand-over"],
    }
    return {
        "schema_version": 5, "engine_commit": COMMIT, "engine_started_at": STARTED.isoformat(), "design_findings": [],
        "findings": [finding],
        "blocked_items": [{"number": 364, "disposition": "finding", "finding_id": finding["id"], "why": "w"}],
        "trend": {"exam_scores": "unobserved", "operator_interventions": "unobserved", "notes": ""},
    }


def reference(evidence: StagedEvidence) -> dict:
    """The right answer: the block AND the review it vetoes, live and
    recurring after the start."""
    doc = blind(evidence)
    veto = evidence.audit.no_progress.refused_work[0]
    doc["findings"][0] = {
        "id": "published-work-review-vetoed-by-its-issues-block",
        "engine": _ENGINE,
        "anomaly_keys": [_NEEDS_HUMAN, _VETO],
        "present_after_start": "true", "recurs_after_start": "true", "origin": "before_start",
        "observed": [
            _snapshot(_index(evidence, "attention_label")),
            _snapshot(_index(evidence, "refused_work")),
            _SINCE,
            {"at": veto.last_seen, "kind": "occurrence",
             "source": "audit.json#/no_progress/refused_work/0/last_seen", "supports": "recurs_after_start"},
        ],
        "grading_window": {"from": _SINCE["at"], "to": CUTOFF.isoformat()},
        "classification": "new_defect",
        "stall_point": "noticed_not_acted",
        "stall_evidence": [DIAGNOSIS_364],
        "output": "capability_issue",
        "root_cause": {
            "owner": "issue_orchestrator.control.review_validity:evaluate_review_validity",
            "why": "an issue's needs-human block makes its PR's review invalid, so published work waits on it",
            "same_shape_sites": ["issue_orchestrator.control.pr_scanner:PRScanner.scan_for_reviews"],
        },
        "reproduction": {
            "kind": "unit_test", "harness": "tests/unit/control/test_review_validity.py",
            "planted_state": "an issue with needs-human and its PR with needs-code-review and published work",
            "assertions": ["the PR's review is queued and launched"], "fails_on": COMMIT,
        },
        "proposal": "review published work whatever its issue's block; the block gates new coding only",
    }
    doc["blocked_items"][0]["finding_id"] = doc["findings"][0]["id"]
    doc["blocked_items"][0]["downstream"] = [{
        "anomaly_key": _VETO, "finding_id": doc["findings"][0]["id"], "refused_action": "review",
        "pipeline_event": "blocked-items.json#/items/0/open_prs/0/pipeline_events/2",
        "impact": "the validated work published on PR #379 can never be reviewed while #364 is blocked",
    }]
    return doc


def test_the_inputs_show_the_veto_downstream_of_the_block(evidence: StagedEvidence) -> None:
    """Built by the real assembly: the INFO refusals are an anomaly, and the
    blocked item names its PR, the PR's pipeline and the refused work."""
    assert ("refused_work", "PR #379", "review:issue_blocked") in {a.key for a in evidence.audit.anomalies}
    assert evidence.blocked_items is not None
    [item] = evidence.blocked_items.items
    assert [p.number for p in item.open_prs] == [379]
    assert [e.event for e in item.open_prs[0].pipeline_events][-1] == "review.skipped"
    assert [(w.subject, w.signature) for w in item.stalled_work] == [("PR #379", "review:issue_blocked")]


def test_the_blind_runs_answer_is_refused_for_not_examining_the_vetoed_review(evidence: StagedEvidence) -> None:
    with pytest.raises(ImproverFindingsRejected) as rejected:
        validate_findings(json.dumps(blind(evidence)), evidence)

    assert rejected.value.rules == {Rule.BLOCKED_ITEM_STALLED_WORK_EXAMINED}
    assert "PR #379 [review:issue_blocked]" in str(rejected.value)


def test_the_blind_runs_answer_fails_the_grade(evidence: StagedEvidence) -> None:
    """The grader catches the miss on its own, whatever the validator lets through."""
    result = grade(ImproverFindings.model_validate_json(json.dumps(blind(evidence))))

    assert not result.passed
    assert result.failures == (
        "#364's account does not name PR #379's refused review with its pipeline event",
        "PR #379's review, refused on every scan while #364 is blocked, has no finding",
    )


def test_the_right_answer_is_accepted_and_passes(evidence: StagedEvidence) -> None:
    result = grade(validate_findings(json.dumps(reference(evidence)), evidence))

    assert result.passed, result.failures


def test_the_veto_keyed_onto_the_blind_finding_without_its_evidence_is_refused(evidence: StagedEvidence) -> None:
    """Review r1 F3: the blind finding with the refusal's key and a dated
    occurrence of it, but still only diagnosing the agent's question, cites
    no snapshot of the vetoed review: it examines nothing."""
    doc = blind(evidence)
    veto = evidence.audit.no_progress.refused_work[0]
    doc["findings"][0]["anomaly_keys"].append(_VETO)
    doc["findings"][0]["recurs_after_start"] = "true"
    doc["findings"][0]["observed"].append(
        {"at": veto.last_seen, "kind": "occurrence",
         "source": "audit.json#/no_progress/refused_work/0/last_seen", "supports": "recurs_after_start"}
    )

    with pytest.raises(ImproverFindingsRejected) as rejected:
        validate_findings(json.dumps(doc), evidence)

    assert rejected.value.rules == {Rule.BLOCKED_ITEM_STALLED_WORK_EXAMINED}


@pytest.mark.parametrize(
    "change",
    [
        {"pipeline_event": None},  # PR #379 has retained pipeline events: one must be cited
        {"pipeline_event": "blocked-items.json#/items/0/open_prs/0/pipeline_events/9"},  # no such event
        {"pipeline_event": "blocked-items.json#/items/0/block_events/1"},  # not the PR's pipeline
        # r5 F1: its label change is not the refusal; the retained skip is.
        {"pipeline_event": "blocked-items.json#/items/0/open_prs/0/pipeline_events/0"},
        # r5 F1: the retained skip refuses the review, not a rework.
        {"refused_action": "rework"},
    ],
)
def test_the_vetoed_review_is_accounted_for_with_its_own_skip(evidence: StagedEvidence, change: dict) -> None:
    doc = reference(evidence)
    doc["blocked_items"][0]["downstream"][0].update(change)

    with pytest.raises(ImproverFindingsRejected) as rejected:
        validate_findings(json.dumps(doc), evidence)

    assert rejected.value.rules == {Rule.BLOCKED_ITEM_STALLED_WORK_EXAMINED}


def test_a_skip_for_another_reason_does_not_show_the_veto(tmp_path: Path) -> None:
    """Review r7: with a recovery wait's review.skipped retained beside the
    veto's, only the veto's skip shows the refusal."""
    data = build_case(
        tmp_path, engine_commit=COMMIT,
        charter=TechLeadCharterPolicy.from_config(Config()).effective_charter(), export_source=_source,
    )
    staged = json.loads((data / "blocked-items.json").read_text())
    staged["items"][0]["open_prs"][0]["pipeline_events"].append({
        "at": "2026-10-02T11:45:00Z", "event": "review.skipped", "reason": "held_by_recovery",
        "detail": "reason: held_by_recovery",
    })
    (data / "blocked-items.json").write_text(json.dumps(staged))
    evidence = load_staged_evidence(data)
    doc = reference(evidence)
    doc["blocked_items"][0]["downstream"][0]["pipeline_event"] = "blocked-items.json#/items/0/open_prs/0/pipeline_events/3"

    with pytest.raises(ImproverFindingsRejected) as rejected:
        validate_findings(json.dumps(doc), evidence)

    assert rejected.value.rules == {Rule.BLOCKED_ITEM_STALLED_WORK_EXAMINED}
    assert grade(validate_findings(json.dumps(reference(evidence)), evidence)).passed


def test_the_refused_review_recurs_after_the_start(evidence: StagedEvidence) -> None:
    """The scanner refused it again after the restart: "unknown" is refused."""
    doc = copy.deepcopy(reference(evidence))
    doc["findings"][0]["recurs_after_start"] = "unknown"
    doc["findings"][0]["observed"] = doc["findings"][0]["observed"][:3]

    with pytest.raises(ImproverFindingsRejected) as rejected:
        validate_findings(json.dumps(doc), evidence)

    assert Rule.RECURRENCE_NEEDS_POST_START_OCCURRENCE in rejected.value.rules
