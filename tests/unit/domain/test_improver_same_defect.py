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
_HANDLER_MODULE = ("issue_orchestrator", "control", "completion_handler")
UPDATE = CodeSite(_HANDLER_MODULE, ("CompletionHandler", "_update_issue_machine"))
OPERATOR_DECISION = CodeSite(("issue_orchestrator", "control", "tech_lead_operator_decision"), ("OperatorDecisionExecutor",))
REFUSAL = CodeSite(("issue_orchestrator", "control", "partial_delivery_guard"), ("PartialDeliveryGuard", "refusal"))
#: What the run's staged engine source answers for every engine source line
#: its design findings cite, and for the owners its findings name (read
#: from it with RunDirEngineSource).
SOURCE = FakeEngineSource(
    functions={
        (_HANDLER, 819): UPDATE,
        (_HANDLER, 799): UPDATE,
        (_HANDLER, 600): CodeSite(_HANDLER_MODULE, ("CompletionHandler", "finalize_terminal_outcome")),
        (
            "improver-data/engine-source/src/issue_orchestrator/domain/state_machines/issue_machine.py",
            114,
        ): CodeSite(("issue_orchestrator", "domain", "state_machines", "issue_machine"), ("IssueStateMachine", "__init__")),
    },
    defined=frozenset({UPDATE, OPERATOR_DECISION, REFUSAL}),
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

    assert stall_profile(route, SOURCE).sites == (OPERATOR_DECISION,)
    assert not same_defect(stall_profile(route, SOURCE), stall_profile(twin, SOURCE))


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


def test_an_owner_relates_only_the_one_definition_it_names() -> None:
    """r1 F1: a stall owner naming a method two classes define is no site;
    one naming another class's method is another site."""
    run_one = CodeSite(OPERATOR_DECISION.module, ("Executor", "run"))
    run_two = CodeSite(OPERATOR_DECISION.module, ("Planner", "run"))
    source = FakeEngineSource(defined=frozenset({run_one, run_two}))
    module = "issue_orchestrator.control.tech_lead_operator_decision"

    def folded(stall_owner: str) -> bool:
        first = _heat(1, **{HAND_OFF: {"owner": f"{module}:Planner.run"}})
        second = _heat(2)
        next(f for f in second["findings"] if f["id"] == ROUTE_PR)["root_cause"]["owner"] = stall_owner
        merged = merge_heats(
            [AcceptedHeat(n, ImproverFindings.model_validate_json(json.dumps(d))) for n, d in ((1, first), (2, second))],
            EffectIdentity(ENGINE, source),
        )
        return any(s.design.id == HAND_OFF for s in merged.same_defects)

    assert not folded(f"{module}:run")
    assert not folded(f"{module}:Executor.run")
    assert folded(f"{module}:Planner.run")


def _log(line: int, quote: str) -> dict:
    return {"kind": "file", "path": "toolbox/logs/orchestrator.log", "line": line, "quote": quote}


INCIDENT_1 = _log(10, "the tick crashed on issue #1 here")
INCIDENT_2 = _log(900, "the tick crashed on issue #2 here")


def _chain_design(id: str, *evidence: dict) -> dict:
    return {
        "id": id, "engine": {"id": ENGINE.engine_id, "repo": ENGINE.repo}, "kind": "silent_assumption",
        "summary": f"{id} summary.", "impact": "Ticks abort.", "proposed_change": "Guard it.",
        "owner": "control/completion_handler.py:_update_issue_machine",
        "evidence": list(evidence),
    }


def _chain_stall(id: str, item: int) -> dict:
    stall = json.loads(json.dumps(next(f for f in _heat(2)["findings"] if f["id"] == ROUTE_PR)))
    stall["id"] = id
    stall["anomaly_keys"] = [{"kind": "attention_label", "subject": f"#{item}", "signature": "needs-human"}]
    stall["stall_evidence"] = []
    stall["root_cause"]["owner"] = "issue_orchestrator.control.completion_handler:CompletionHandler._update_issue_machine"
    return stall


CHAIN = [
    _chain_design("crash-on-1", INCIDENT_1),
    _chain_design("crash-on-1-and-2", INCIDENT_1, INCIDENT_2),
    _chain_design("crash-on-2", INCIDENT_2),
]


def test_only_a_direct_relation_folds_never_one_through_a_third_finding() -> None:
    """r1 F2: designs about #1 and #2 are related only through a design that
    names both. Neither folds into the other; the one naming both, related
    to two stall findings, folds into neither."""
    alone = _merge({**_heat(1), "findings": [], "design_findings": CHAIN})
    with_stalls = _merge({**_heat(1), "findings": [_chain_stall("fix-1", 1), _chain_stall("fix-2", 2)],
                          "design_findings": CHAIN})

    assert [(s.design.id, s.finding_id) for s in alone.same_defects] == [("crash-on-1-and-2", "crash-on-1")]
    assert {"crash-on-1", "crash-on-2"} <= set(_filed(alone))
    assert [(s.design.id, s.finding_id) for s in with_stalls.same_defects] == [
        ("crash-on-1", "fix-1"), ("crash-on-2", "fix-2"),
    ]
    assert "crash-on-1-and-2" in _filed(with_stalls)


def test_no_order_of_the_findings_joins_two_that_are_not_related() -> None:
    """r3 F1: with the design finding naming both incidents first, the two
    that each name one may not both fold into it: they are not related."""
    broad_first = _merge({**_heat(1), "findings": [], "design_findings": [CHAIN[1], CHAIN[0], CHAIN[2]]})

    assert [(s.design.id, s.finding_id) for s in broad_first.same_defects] == [("crash-on-1", "crash-on-1-and-2")]
    assert {"crash-on-1-and-2", "crash-on-2"} <= set(_filed(broad_first))


def test_two_design_findings_about_one_item_but_no_one_incident_are_two_defects() -> None:
    """r2 F2: two defects of one function can show on one item. Two design
    findings must cite one thing to be one defect."""
    one = _chain_design("crash-on-327-at-start", _log(10, "issue #327 crashed the tick at start"))
    two = _chain_design("crash-on-327-at-merge", _log(900, "issue #327 crashed the tick at merge"))

    merged = _merge({**_heat(1), "findings": [], "design_findings": [one, two]})

    assert merged.same_defects == ()


def test_a_finding_both_are_related_to_does_not_join_two_that_are_not() -> None:
    """r4 F1: two design findings about two incidents on #327, each related
    to a stall finding on #327 in their function, are not both its defect."""
    start = _chain_design("crash-on-327-at-start", _log(10, "issue #327 crashed the tick at start"))
    merge = _chain_design("crash-on-327-at-merge", _log(900, "issue #327 crashed the tick at merge"))

    merged = _merge({**_heat(1), "findings": [_chain_stall("fix-327", 327)], "design_findings": [start, merge]})

    assert [(s.design.id, s.finding_id) for s in merged.same_defects] == [("crash-on-327-at-start", "fix-327")]
    assert "crash-on-327-at-merge" in _filed(merged)


def test_a_finding_it_cannot_join_does_not_keep_it_from_one_it_can() -> None:
    """r5 F1: two design findings citing one crash at a merge are one
    defect, though each is related to a #327 stall finding that already
    carries a crash at the start."""
    start = _chain_design("crash-on-327-at-start", _log(10, "issue #327 crashed the tick at start"))
    at_merge = _log(900, "issue #327 crashed the tick at merge")
    merge = _chain_design("crash-on-327-at-merge", at_merge)
    again = _chain_design("crash-on-327-at-merge-again", at_merge)

    merged = _merge({**_heat(1), "findings": [_chain_stall("fix-327", 327)], "design_findings": [start, merge, again]})

    assert [(s.design.id, s.finding_id) for s in merged.same_defects] == [
        ("crash-on-327-at-start", "fix-327"), ("crash-on-327-at-merge-again", "crash-on-327-at-merge"),
    ]


def test_a_record_a_quote_names_is_named_whole() -> None:
    """r6 F1: a stall finding citing decision D1 and a design finding
    quoting D10 do not share a record."""
    stall = {**_chain_stall("fix-555", 555), "stall_evidence": ["D1"]}

    for quote, folded in (("the tech lead applied D10 to it", False), ("the tech lead applied D1 to it", True)):
        design = _chain_design("crash-after-decision", _log(10, quote))
        merged = _merge({**_heat(1), "findings": [stall], "design_findings": [design]})

        assert bool(merged.same_defects) is folded, quote


def _answer(call: int, quote: str) -> dict:
    return {"kind": "tool", "call": call, "quote": quote}


def test_one_toolbox_answer_is_one_call() -> None:
    """r4 F3: two answers can hold the same phrase; one call is one answer."""
    phrase = "Can't trigger event needs_human"
    first = _chain_design("crash-seen-in-call-1", _answer(1, phrase))

    for call, folded in ((2, False), (1, True)):
        second = _chain_design("crash-seen-again", _answer(call, phrase))
        merged = _merge({**_heat(1), "findings": [], "design_findings": [first, second]})

        assert bool(merged.same_defects) is folded, call


def test_an_owner_is_read_as_the_prompt_asks_it_written() -> None:
    assert code_site("control/completion_handler.py:CompletionHandler._update_issue_machine") == CodeSite(
        ("control", "completion_handler"), ("CompletionHandler", "_update_issue_machine")
    )
    assert code_site("issue_orchestrator.control.x:run()") == CodeSite(("issue_orchestrator", "control", "x"), ("run",))
    for unanchored in ("issue_orchestrator.control.x.run", "control/x.py:run (and its callers)", "control/x.txt:run", ":run"):
        assert code_site(unanchored) is None, unanchored
