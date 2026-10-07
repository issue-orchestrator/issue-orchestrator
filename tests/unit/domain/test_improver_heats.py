"""Merging an improver run's accepted heats, with each finding's support (#8001)."""

from __future__ import annotations

import json

from issue_orchestrator.contracts.improver_findings import ImproverFindings
from issue_orchestrator.domain.improver_heats import AcceptedHeat, merge_heats
from tests.unit.improver_support import ENGINE_ID, AUDITED_REPO, example


def _findings(*names: str, designs: list[dict] | None = None) -> ImproverFindings:
    docs = [example(n) for n in names]
    merged = docs[0]
    merged["findings"] = [f for d in docs for f in d["findings"]]
    merged["design_findings"] = designs or []
    return ImproverFindings.model_validate_json(json.dumps(merged))


def _key(finding) -> str:  # type: ignore[no-untyped-def]
    case = finding.reproduction.case_id if finding.reproduction else None
    return f"{finding.output}:{sorted(k.key for k in finding.anomaly_keys)}:{case}"


def _dkey(design) -> str:  # type: ignore[no-untyped-def]
    return f"{design.kind}:{design.id}"


def _merge(heats):  # type: ignore[no-untyped-def]
    return merge_heats(heats, _key, _dkey)


def _design(id: str, kind: str = "operator_friction", line: int = 41, call: int | None = None) -> dict:
    evidence = (
        {"kind": "tool", "call": call, "quote": "Removed proposed-tech-lead to approve"}
        if call is not None
        else {"kind": "file", "path": "toolbox/logs/orchestrator.log", "line": line, "quote": "auth_expired: parking all work"}
    )
    return {
        "id": id, "engine": {"id": ENGINE_ID, "repo": AUDITED_REPO}, "kind": kind,
        "summary": "Approval is a label removal.", "evidence": [evidence],
        "impact": "Anything that strips labels approves.", "proposed_change": "A positive approval act.",
    }


def test_a_finding_several_heats_found_is_kept_once_with_each_heat_as_support() -> None:
    merged = _merge(
        [AcceptedHeat(1, _findings("exam_case")), AcceptedHeat(2, _findings("exam_case", "capability_issue"))]
    )

    exam = example("exam_case")["findings"][0]["id"]
    capability = example("capability_issue")["findings"][0]["id"]
    assert [f.id for f in merged.findings.findings] == [exam, capability]
    assert merged.support == {exam: (1, 2), capability: (2,)}
    # The heat with the most findings is primary: its accounts and trend stand.
    assert merged.primary == 2
    assert merged.findings.blocked_items == _findings("exam_case", "capability_issue").blocked_items


def test_a_distinct_finding_with_a_taken_id_is_renamed_for_its_heat() -> None:
    clash = example("capability_issue")
    clash["findings"][0]["id"] = example("exam_case")["findings"][0]["id"]
    second = ImproverFindings.model_validate_json(json.dumps(clash))

    merged = _merge([AcceptedHeat(1, _findings("exam_case")), AcceptedHeat(2, second)])

    ids = [f.id for f in merged.findings.findings]
    assert len(set(ids)) == 2 and ids[1].endswith("-h2")
    assert merged.support[ids[1]] == (2,)


def test_a_different_finding_proposing_a_taken_exam_case_is_surfaced_not_merged() -> None:
    """r1 F2: a case id names one case. A different finding proposing it is
    no support for the first; it is a conflict, recorded for the operator."""
    other = example("exam_case")
    other["findings"][0]["id"] = "same-case-other-anomaly"
    other["findings"][0]["anomaly_keys"] = example("capability_issue")["findings"][0]["anomaly_keys"]
    other["findings"][0]["proposal"] = "Plant the other anomaly instead."
    second = ImproverFindings.model_validate_json(json.dumps(other))
    exam = example("exam_case")["findings"][0]["id"]

    merged = _merge([AcceptedHeat(1, _findings("exam_case")), AcceptedHeat(2, second)])

    assert [f.id for f in merged.findings.findings] == [exam]
    assert merged.support[exam] == (1,)
    [conflict] = merged.conflicts
    assert (conflict.finding_id, conflict.heat, conflict.claim) == (exam, 2, "Plant the other anomaly instead.")
    assert "same-case-other-anomaly" in conflict.reason


def test_a_design_finding_is_one_across_heats_only_by_its_effect_key_and_claim() -> None:
    """r1 F1: a shared citation is no identity. The same key with the same
    claim is support; with a different claim, a conflict to resolve."""
    first = _findings("exam_case", designs=[_design("approval-by-label"), _design("shared-line-a")])
    second = _findings("exam_case", designs=[
        _design("approval-by-label"),                                        # same key, same claim: support
        _design("shared-line-b"),                                            # same cited line, another id: distinct
        {**_design("shared-line-a"), "summary": "Something else entirely."},  # same key, another claim: conflict
    ])

    merged = _merge([AcceptedHeat(1, first), AcceptedHeat(2, second)])

    assert merged.primary == 2
    assert sorted(d.id for d in merged.findings.design_findings) == [
        "approval-by-label", "shared-line-a", "shared-line-b",
    ]
    assert merged.support["approval-by-label"] == (1, 2)
    assert merged.support["shared-line-b"] == (2,)
    assert merged.support["shared-line-a"] == (2,)
    [conflict] = merged.conflicts
    assert conflict.finding_id == "shared-line-a" and conflict.heat == 1
    assert "Approval is a label removal." in conflict.claim


def test_a_renamed_design_keeps_its_original_id_for_its_effect_key() -> None:
    """r1 F3: renamed for this run's merge (its id taken by another kind), a
    design finding keeps the id its heat wrote, so its issue dedups the same
    way in a run where nothing collides."""
    first = _findings("exam_case", designs=[_design("approval", kind="silent_assumption")])
    second = _findings("exam_case", "capability_issue", designs=[_design("approval")])

    merged = _merge([AcceptedHeat(1, first), AcceptedHeat(2, second)])

    assert merged.primary == 2
    renamed = next(d for d in merged.findings.design_findings if d.kind == "silent_assumption")
    assert renamed.id == "approval-h1"
    assert merged.original_ids == {"approval-h1": "approval"}


def test_one_heat_merges_to_itself() -> None:
    only = _findings("exam_case", "capability_issue")

    merged = _merge([AcceptedHeat(3, only)])

    assert merged.findings == only and merged.primary == 3
    assert set(merged.support.values()) == {(3,)}
