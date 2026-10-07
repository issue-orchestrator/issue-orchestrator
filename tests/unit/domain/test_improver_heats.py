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
    return f"{finding.output}:{sorted(k.key for k in finding.anomaly_keys)}"


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
    merged = merge_heats(
        [AcceptedHeat(1, _findings("exam_case")), AcceptedHeat(2, _findings("exam_case", "capability_issue"))],
        _key,
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

    merged = merge_heats([AcceptedHeat(1, _findings("exam_case")), AcceptedHeat(2, second)], _key)

    ids = [f.id for f in merged.findings.findings]
    assert len(set(ids)) == 2 and ids[1].endswith("-h2")
    assert merged.support[ids[1]] == (2,)


def test_two_heats_proposing_the_same_new_exam_case_are_one_finding() -> None:
    """A case id names one case; two different findings may not both propose it."""
    other = example("exam_case")
    other["findings"][0]["id"] = "same-case-other-words"
    other["findings"][0]["output"] = "exam_case"
    other["findings"][0]["anomaly_keys"] = example("capability_issue")["findings"][0]["anomaly_keys"]
    second = ImproverFindings.model_validate_json(json.dumps(other))

    merged = merge_heats([AcceptedHeat(1, _findings("exam_case")), AcceptedHeat(2, second)], _key)

    assert len(merged.findings.findings) == 1
    assert merged.support[merged.findings.findings[0].id] == (1, 2)


def test_design_findings_merge_by_kind_and_a_shared_id_or_citation() -> None:
    first = _findings("exam_case", designs=[_design("approval-by-label"), _design("tool-cited", call=3)])
    second = _findings("exam_case", designs=[
        _design("label-approval"),                       # another id, the same cited line: the same finding
        _design("approval-by-label", kind="manual_operator_step", line=99),  # same id, another kind: distinct
        _design("tool-cited-again", call=3),             # the same toolbox answer: the same finding
        _design("a-new-one", line=7),                    # new
    ])

    merged = merge_heats([AcceptedHeat(1, first), AcceptedHeat(2, second)], _key)

    designs = {d.id: d.kind for d in merged.findings.design_findings}
    assert merged.primary == 2  # more findings in all
    # Heat 1's two designs are heat 2's first and third (same cited line,
    # same toolbox answer); the same id of another kind stays distinct.
    assert designs == {
        "label-approval": "operator_friction",
        "approval-by-label": "manual_operator_step",
        "tool-cited-again": "operator_friction",
        "a-new-one": "operator_friction",
    }
    assert merged.support["label-approval"] == (1, 2)
    assert merged.support["tool-cited-again"] == (1, 2)
    assert merged.support["approval-by-label"] == (2,)
    assert merged.support["a-new-one"] == (2,)


def test_one_heat_merges_to_itself() -> None:
    only = _findings("exam_case", "capability_issue")

    merged = merge_heats([AcceptedHeat(3, only)], _key)

    assert merged.findings == only and merged.primary == 3
    assert set(merged.support.values()) == {(3,)}
