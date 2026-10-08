"""One defect is filed once, whatever kinds of finding report it (#8700).

The fixtures are the two accepted heat answers of the first apply-mode run
(``20261008T061946Z-a858fd07``), which filed one defect twice in two ways:
two design findings about one code line (#8693/#8696), and a design finding
and a stall finding about one owner (#8694/#8691).
"""

from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.contracts.improver_findings import ImproverFindings
from issue_orchestrator.control.improver_effects import EffectIdentity, planned_effects
from issue_orchestrator.domain.engine_activity import EngineRef
from issue_orchestrator.domain.improver_defects import CodeSite, code_site, same_defect, stall_profile
from issue_orchestrator.domain.improver_heats import AcceptedHeat, MergedHeats, merge_heats
from tests.unit.improver_support import FakeEngineSource

RUN = Path(__file__).resolve().parents[2] / "fixtures" / "improver" / "run-20261008T061946Z-a858fd07"
ENGINE = EngineRef(
    engine_id="repo-89afeb99eff0542e787f28211c93f58cd90aa3e0fe9d2bf3acd6fcdc63e2fb73",
    repo="porchpin/porchpin",
    state_dir=Path("/engine/state"),
)
_HANDLER = "improver-data/engine-source/src/issue_orchestrator/control/completion_handler.py"
#: What the run's staged engine source answers for every engine source line
#: its design findings cite (read from it with RunDirEngineSource).
SOURCE = FakeEngineSource(
    functions={
        (_HANDLER, 819): ("CompletionHandler", "_update_issue_machine"),
        (_HANDLER, 799): ("CompletionHandler", "_update_issue_machine"),
        (_HANDLER, 600): ("CompletionHandler", "finalize_terminal_outcome"),
        (
            "improver-data/engine-source/src/issue_orchestrator/domain/state_machines/issue_machine.py",
            114,
        ): ("IssueStateMachine", "__init__"),
    }
)
IDENTITY = EffectIdentity(ENGINE, SOURCE)

CRASH_H1 = "rework-needs-human-crashes-issue-state-machine"  # #8693, heat 1 (design)
CRASH_H2 = "rework-needs-human-on-pr-pending-aborts-tick"  # #8696, heat 2 (design)
HAND_OFF = "operator-approval-does-not-carry-the-pr-hand-off"  # #8694, heat 1 (design)
ROUTE_PR = "decision-approval-cannot-route-the-pr-327"  # #8691, heat 2 (stall)
#: Heat 1's stall finding about #327 too, through another owner.
NON_PARTIAL = "non-partial-receipt-publishes-closing-pr-on-partially-delivered-issue"
#: Heat 2's stall findings with the effect key of one of heat 1's: merged by key.
KEY_MERGED = {"flaky-check-rerun-has-no-action-450", "provisioning-handover-179-undated-marker"}
#: As the prompt now asks (#8700): where the hand-off defect lives.
HAND_OFF_OWNER = "issue_orchestrator.control.tech_lead_operator_decision:OperatorDecisionExecutor"


def _heat(n: int, **design_updates: dict) -> dict:
    doc = json.loads((RUN / f"heat-{n}.json").read_text(encoding="utf-8"))
    for design in doc["design_findings"]:
        design.update(design_updates.get(design["id"], {}))
    return doc


def _merge(*docs: dict) -> MergedHeats:
    return merge_heats(
        [AcceptedHeat(n, ImproverFindings.model_validate_json(json.dumps(d))) for n, d in enumerate(docs, 1)],
        IDENTITY,
    )


def _filed(merged: MergedHeats) -> list[str]:
    """The finding ids the run files (or comments) an effect for."""
    return [e.finding_id for e in planned_effects(merged.findings, ENGINE, merged.original_ids)]


def _all_ids(*docs: dict) -> set[str]:
    return {x["id"] for d in docs for x in (*d["findings"], *d["design_findings"])}


def test_this_runs_four_findings_about_two_defects_file_two_issues() -> None:
    heats = (_heat(1, **{HAND_OFF: {"owner": HAND_OFF_OWNER}}), _heat(2))

    merged = _merge(*heats)

    filed = _filed(merged)
    assert [i for i in filed if i in {CRASH_H1, CRASH_H2, HAND_OFF, ROUTE_PR}] == [ROUTE_PR, CRASH_H1]
    assert {(s.design.id, s.finding_id, s.heats) for s in merged.same_defects} == {
        (CRASH_H2, CRASH_H1, (2,)),
        (HAND_OFF, ROUTE_PR, (1,)),
    }
    # Each defect was found by both heats.
    assert merged.support[CRASH_H1] == (1, 2) and merged.support[ROUTE_PR] == (1, 2)
    # Every other finding of the run is filed as before: nothing else is folded.
    assert sorted(filed) == sorted(_all_ids(*heats) - {CRASH_H2, HAND_OFF} - KEY_MERGED)


def test_as_written_the_two_design_findings_citing_one_code_line_are_one_issue() -> None:
    """The run's answers carry no design owner: the code line both cite,
    with the log lines both cite, is enough. The hand-off finding names no
    code, so it stays its own issue: nothing guesses."""
    merged = _merge(_heat(1), _heat(2))

    assert [(s.design.id, s.finding_id) for s in merged.same_defects] == [(CRASH_H2, CRASH_H1)]
    assert HAND_OFF in _filed(merged) and CRASH_H2 not in _filed(merged)


def test_an_item_both_are_about_does_not_relate_findings_on_different_code() -> None:
    """The hand-off design finding and the non-partial-receipt stall finding
    are both about #327, but their owners differ."""
    merged = _merge(_heat(1, **{HAND_OFF: {"owner": HAND_OFF_OWNER}}), _heat(2))

    assert all(s.finding_id != NON_PARTIAL for s in merged.same_defects)


def test_one_code_site_without_shared_evidence_is_not_one_defect() -> None:
    """One function can hold two defects: the second crash finding keeps
    only its code citations, and is then its own issue."""
    second = _heat(2)
    crash = next(d for d in second["design_findings"] if d["id"] == CRASH_H2)
    crash["evidence"] = [e for e in crash["evidence"] if "engine-source" in e["path"]]

    merged = _merge(_heat(1), second)

    assert merged.same_defects == ()
    assert {CRASH_H1, CRASH_H2} <= set(_filed(merged))


def test_a_design_finding_related_to_two_stall_findings_is_folded_into_neither() -> None:
    first = _heat(1, **{HAND_OFF: {"owner": HAND_OFF_OWNER}})
    non_partial = next(f for f in first["findings"] if f["id"] == NON_PARTIAL)
    non_partial["root_cause"]["owner"] = HAND_OFF_OWNER

    merged = _merge(first, _heat(2))

    assert HAND_OFF in _filed(merged)
    assert all(s.design.id != HAND_OFF for s in merged.same_defects)


def test_stall_findings_are_never_related_by_this_rule() -> None:
    """A stall finding's identity is its effect key: an exam case and a fix
    about one owner are two deliverables."""
    route = next(f for f in ImproverFindings.model_validate_json(json.dumps(_heat(2))).findings if f.id == ROUTE_PR)
    twin = route.model_copy(update={"id": "route-pr-exam-case"})

    assert not same_defect(stall_profile(route), stall_profile(twin))


def test_a_folded_design_findings_conflict_and_change_motive_move_to_its_defect() -> None:
    """What another heat claimed against a folded design finding is shown on
    the issue that carries it; the run's change cites that issue's finding."""
    first = _heat(1, **{HAND_OFF: {"owner": HAND_OFF_OWNER}})
    first["improver_change"] = {
        "edit": {"kind": "heats", "heats": 3}, "why": "More heats.", "expected_effect": "Recall.",
        "motivated_by": [HAND_OFF, CRASH_H1],
    }
    other = {**_heat(1), "findings": [], "design_findings": [
        {**next(d for d in _heat(1)["design_findings"] if d["id"] == HAND_OFF), "summary": "Another claim."},
    ]}

    merged = _merge(first, _heat(2), other)

    [conflict] = [c for c in merged.conflicts if c.heat == 3]
    assert conflict.finding_id == ROUTE_PR and "Another claim." in conflict.claim
    assert merged.findings.improver_change is not None
    assert merged.findings.improver_change.motivated_by == (ROUTE_PR, CRASH_H1)


def test_an_owner_is_read_as_the_prompt_asks_it_written() -> None:
    assert code_site("control/completion_handler.py:CompletionHandler._update_issue_machine") == CodeSite(
        ("control", "completion_handler"), ("CompletionHandler", "_update_issue_machine")
    )
    assert code_site("issue_orchestrator.control.x:run()") == CodeSite(("issue_orchestrator", "control", "x"), ("run",))
    for unanchored in ("issue_orchestrator.control.x.run", "control/x.py:run (and its callers)", "control/x.txt:run", ":run"):
        assert code_site(unanchored) is None, unanchored
    # One module and function, written from different roots, is one site;
    # a class is not the same site as its method; another class's is not.
    method = CodeSite(("control", "completion_handler"), ("CompletionHandler", "_update_issue_machine"))
    assert method.matches(CodeSite(("issue_orchestrator", "control", "completion_handler"), ("_update_issue_machine",)))
    assert not method.matches(CodeSite(("control", "completion_handler"), ("CompletionHandler",)))
    assert not method.matches(CodeSite(("domain", "completion_handler"), ("CompletionHandler", "_update_issue_machine")))
    assert not CodeSite(("m",), ("A", "__init__")).matches(CodeSite(("m",), ("B", "__init__")))
