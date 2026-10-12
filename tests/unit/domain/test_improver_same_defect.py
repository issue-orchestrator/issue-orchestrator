"""One defect is filed once, whatever kinds of finding report it (#8700),
and whichever run reports it (#8986).

The fixtures are the two accepted heat answers of the first apply-mode run
(``20261008T061946Z-a858fd07``), which filed one defect twice in two ways:
two design findings about one code line (#8693/#8696), and a design finding
and a stall finding about one owner (#8694/#8691). Applied as two runs, the
same answers are a later run rewording an earlier run's finding.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from issue_orchestrator.contracts.improver_findings import FINDINGS_FILE, ImproverFindings
from issue_orchestrator.contracts.improver_run import EffectStatus, ImproverRunRecord, RunOutcome
from issue_orchestrator.control.improver_effects import (
    DEFECT_MARKER,
    EffectIdentity,
    filed_defect,
    planned_effects,
)
from issue_orchestrator.domain.engine_activity import EngineRef
from issue_orchestrator.domain.improver_defects import CodeSite, code_site, same_defect, stall_profile
from issue_orchestrator.domain.improver_heats import AcceptedHeat, MergedHeats, merge_heats
from issue_orchestrator.execution.improver_effect_applier import ImproverEffects
from issue_orchestrator.ports.engine_audit import OpenIssueLabels
from tests.unit.improver_support import FakeEngineSource, FakeIssueHost, MemoryRunStore

RUN = Path(__file__).resolve().parents[2] / "fixtures" / "improver" / "run-20261008T061946Z-a858fd07"
ENGINE = EngineRef(
    engine_id="repo-89afeb99eff0542e787f28211c93f58cd90aa3e0fe9d2bf3acd6fcdc63e2fb73",
    repo="porchpin/porchpin",
    state_dir=Path("/engine/state"),
)
_HANDLER = "improver-data/engine-source/src/issue_orchestrator/control/completion_handler.py"
_HANDLER_MODULE = ("issue_orchestrator", "control", "completion_handler")
UPDATE = CodeSite(_HANDLER_MODULE, ("CompletionHandler", "_update_issue_machine"))
FINALIZE = CodeSite(_HANDLER_MODULE, ("CompletionHandler", "finalize_terminal_outcome"))
OPERATOR_DECISION = CodeSite(("issue_orchestrator", "control", "tech_lead_operator_decision"), ("OperatorDecisionExecutor",))
REFUSAL = CodeSite(("issue_orchestrator", "control", "partial_delivery_guard"), ("PartialDeliveryGuard", "refusal"))
#: What the run's staged engine source answers for every engine source line
#: its design findings cite, and for the owners its findings name (read
#: from it with RunDirEngineSource).
SOURCE = FakeEngineSource(
    functions={
        (_HANDLER, 819): UPDATE,
        (_HANDLER, 799): UPDATE,
        (_HANDLER, 600): FINALIZE,
        (
            "improver-data/engine-source/src/issue_orchestrator/domain/state_machines/issue_machine.py",
            114,
        ): CodeSite(("issue_orchestrator", "domain", "state_machines", "issue_machine"), ("IssueStateMachine", "__init__")),
    },
    defined=frozenset({UPDATE, FINALIZE, OPERATOR_DECISION, REFUSAL}),
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


def _merge(*docs: dict, identity: EffectIdentity = IDENTITY) -> MergedHeats:
    return merge_heats(
        [AcceptedHeat(n, ImproverFindings.model_validate_json(json.dumps(d))) for n, d in enumerate(docs, 1)],
        identity,
    )


def _filed(merged: MergedHeats) -> list[str]:
    """The finding ids the run files (or comments) an effect for."""
    return [e.finding_id for e in planned_effects(merged.findings, IDENTITY, merged.original_ids)]


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


def test_one_file_cited_by_two_spellings_of_its_path_is_one_citation() -> None:
    """r7 F1: `logs/./x.log` is `logs/x.log`, for a log line and for a line
    of engine source alike."""
    crash = "Can't trigger event needs_human from state pr_pending!"
    first = _chain_design("crash-plain-path", _log(10, crash))
    second = _chain_design("crash-dotted-path", {**_log(10, crash), "path": "toolbox/logs/./orchestrator.log"})
    second["owner"] = None
    second["evidence"].append(
        {"kind": "file", "path": _HANDLER.replace("control/", "control/./"), "line": 819, "quote": "issue_machine.needs_human()"}
    )

    merged = _merge({**_heat(1), "findings": [], "design_findings": [first, second]})

    assert [(s.design.id, s.finding_id) for s in merged.same_defects] == [("crash-dotted-path", "crash-plain-path")]


def test_a_declared_owner_is_where_the_defect_lives_and_cited_source_is_context() -> None:
    """r8 F1: the crash finding names `_update_issue_machine` and cites
    `finalize_terminal_outcome` as context: it is not the defect of a stall
    finding owned by `finalize_terminal_outcome`."""
    finalize = {**_chain_stall("fix-finalize", 450), "stall_evidence": []}
    finalize["root_cause"]["owner"] = "control/completion_handler.py:CompletionHandler.finalize_terminal_outcome"
    crash = _chain_design(
        "crash-in-update", _log(10, "rework of #450 ended needs_human"),
        {"kind": "file", "path": _HANDLER, "line": 600, "quote": "from .scoped_rework import note_scoped_rework_finished"},
    )

    for owner, folded in (("control/completion_handler.py:_update_issue_machine", False), (None, True)):
        merged = _merge({**_heat(1), "findings": [finalize], "design_findings": [{**crash, "owner": owner}]})

        assert bool(merged.same_defects) is folded, owner


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


# -- across runs (#8986) --------------------------------------------------------

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
OUTPUTS = "io/io"
#: Heat 2's tracked finding comments on its tracked issue, which is open.
TRACKED = OpenIssueLabels(number=8137, title="Parked records keyed on the original branch", labels=())


def _host() -> FakeIssueHost:
    return FakeIssueHost([TRACKED])


def _record(
    store: MemoryRunStore, run_id: str, *docs: dict, identity: EffectIdentity = IDENTITY, profiles: bool = True
) -> None:
    """One accepted run of ``docs`` (its heats), its effects owed; without
    ``profiles``, as a run recorded before #8986 owes them."""
    merged = _merge(*docs, identity=identity)
    run_dir = store.new_run_dir(run_id)
    (run_dir / FINDINGS_FILE).write_text(merged.findings.model_dump_json(by_alias=True), encoding="utf-8")
    effects = planned_effects(merged.findings, identity, merged.original_ids)
    store.record(ImproverRunRecord(
        run_id=run_id, started_at=NOW, finished_at=NOW, outcome=RunOutcome.ACCEPTED, detail="",
        engine_id=identity.engine.engine_id, audited_repo=identity.engine.repo, outputs_repo=OUTPUTS,
        run_dir=str(run_dir), engine_commit=merged.findings.engine_commit,
        effects=effects if profiles else tuple(e.model_copy(update={"profile": None}) for e in effects),
    ))


def _apply(
    store: MemoryRunStore, host: FakeIssueHost, run_id: str, *docs: dict, identity: EffectIdentity = IDENTITY,
    profiles: bool = True,
) -> ImproverRunRecord:
    """One accepted run of ``docs`` (its heats), recorded and applied."""
    _record(store, run_id, *docs, identity=identity, profiles=profiles)
    ImproverEffects(store=store, host=host, outputs_repo=OUTPUTS, clock=lambda: NOW).apply_pending()
    return next(r for r in store.runs() if r.run_id == run_id)


def _effect(run: ImproverRunRecord, finding_id: str) -> tuple[EffectStatus, int | None]:
    receipt = next(e for e in run.effects if e.finding_id == finding_id)
    return receipt.status, receipt.issue_number


def _filed_for(host: FakeIssueHost, finding_id: str) -> int:
    [number] = [c["number"] for c in host.created if f": {finding_id} (" in c["title"]]
    return number


def test_a_later_run_rewording_8693s_slug_comments_on_8693_instead_of_filing(tmp_path: Path) -> None:
    """#8693 was filed by heat 1; heat 2 wrote the same defect under another
    slug. Applied as a later run, heat 2's finding is evidence on #8693's
    issue: its title token differs, its defect does not."""
    store, host = MemoryRunStore(tmp_path), _host()
    _apply(store, host, "a", _heat(1))
    crash = _filed_for(host, CRASH_H1)

    later = _apply(store, host, "b", _heat(2))

    assert _effect(later, CRASH_H2) == (EffectStatus.COMMENTED, crash)
    assert not any(CRASH_H2 in c["title"] for c in host.created)
    [(_, body)] = [(n, b) for n, b in host.comments if n == crash]
    assert "found this issue's defect again" in body and f"`{CRASH_H2}`" in body
    assert '"id": "rework-needs-human-on-pr-pending-aborts-tick"' in body


def test_a_run_owed_with_an_earlier_one_comments_on_the_issue_that_one_just_filed(tmp_path: Path) -> None:
    """Both runs' effects are applied in one batch: the issue the first
    files carries its defect for the second, before any re-read."""
    store, host = MemoryRunStore(tmp_path), _host()
    for run_id, doc in (("a", _heat(1)), ("b", _heat(2))):
        _record(store, run_id, doc)

    applied = ImproverEffects(store=store, host=host, outputs_repo=OUTPUTS, clock=lambda: NOW).apply_pending()

    later = next(r for r in applied if r.run_id == "b")
    assert _effect(later, CRASH_H2) == (EffectStatus.COMMENTED, _filed_for(host, CRASH_H1))


def _recovered(tmp_path: Path, *, closed_since: bool) -> tuple[dict[str, ImproverRunRecord], FakeIssueHost, int]:
    """Run A's POST filed #8693's issue, then its response was lost, and the
    open listing does not show it yet; run B (heat 2, the same defect under
    another key) is owed with it. The issue is closed (or not) before the
    batch is applied again."""
    store, host = MemoryRunStore(tmp_path), _host()
    host.stale_listing = True
    first = _heat(1)
    _record(store, "a", {**first, "findings": [], "design_findings": [
        d for d in first["design_findings"] if d["id"] == CRASH_H1
    ]})
    _record(store, "b", _heat(2))
    effects = ImproverEffects(store=store, host=host, outputs_repo=OUTPUTS, clock=lambda: NOW)
    host.lose_create_result = RuntimeError("connection reset")
    [lost] = effects.apply_pending()
    [crash] = [c["number"] for c in host.created]
    assert lost.run_id == "a" and lost.effects[0].create_attempted_at is not None
    host.lose_create_result = None
    if closed_since:
        host.open = [i for i in host.open if i.number != crash]
    return {r.run_id: r for r in effects.apply_pending()}, host, crash


def test_an_issue_recovered_after_a_lost_create_and_closed_since_takes_no_later_evidence(tmp_path: Path) -> None:
    """Recovery proves A filed the issue over every issue, open or closed;
    only one proven open is an open issue. B's evidence is never commented
    on the closed issue: B files the defect anew."""
    applied, host, crash = _recovered(tmp_path, closed_since=True)

    assert _effect(applied["a"], CRASH_H1) == (EffectStatus.FILED, crash)
    status, filed = _effect(applied["b"], CRASH_H2)
    assert status is EffectStatus.FILED and filed not in (None, crash)
    assert filed == _filed_for(host, CRASH_H2)
    assert not any(n == crash for n, _ in host.comments)


def test_an_issue_recovered_after_a_lost_create_and_still_open_carries_its_defect(tmp_path: Path) -> None:
    applied, host, crash = _recovered(tmp_path, closed_since=False)

    assert _effect(applied["a"], CRASH_H1) == (EffectStatus.FILED, crash)
    assert _effect(applied["b"], CRASH_H2) == (EffectStatus.COMMENTED, crash)
    assert not any(CRASH_H2 in c["title"] for c in host.created)


def test_a_later_design_finding_about_an_open_stall_findings_defect_comments_there(tmp_path: Path) -> None:
    """#8691 (a stall finding) was filed first; the hand-off design finding
    names its owner and is about its item, so a later run comments on it."""
    store, host = MemoryRunStore(tmp_path), _host()
    _apply(store, host, "a", _heat(2))
    route = next(c["number"] for c in host.created if ROUTE_PR in c["title"])

    later = _apply(store, host, "b", _heat(1, **{HAND_OFF: {"owner": HAND_OFF_OWNER}}))

    assert _effect(later, HAND_OFF) == (EffectStatus.COMMENTED, route)


def test_a_later_stall_finding_about_an_open_design_findings_defect_comments_there(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), _host()
    _apply(store, host, "a", _heat(1, **{HAND_OFF: {"owner": HAND_OFF_OWNER}}))
    hand_off = _filed_for(host, HAND_OFF)

    later = _apply(store, host, "b", _heat(2))

    assert _effect(later, ROUTE_PR) == (EffectStatus.COMMENTED, hand_off)
    assert not any(ROUTE_PR in c["title"] for c in host.created)


def test_a_receipt_planned_before_profiles_files_as_it_did(tmp_path: Path) -> None:
    """A run recorded before #8986 owes receipts without a profile: they
    file, carrying only their key, and match no open issue's defect."""
    store, host = MemoryRunStore(tmp_path), _host()
    _apply(store, host, "a", _heat(1))

    old = _apply(store, host, "old", _heat(2), profiles=False)

    assert _effect(old, CRASH_H2)[0] is EffectStatus.FILED
    crash_h2 = next(c for c in host.created if CRASH_H2 in c["title"])
    assert DEFECT_MARKER not in crash_h2["body"]


def test_every_filed_issue_records_its_defect_on_its_first_line(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), _host()
    run = _apply(store, host, "a", _heat(1))

    crash = next(c for c in host.created if c["number"] == _filed_for(host, CRASH_H1))
    first, rest = crash["body"].split("\n", 1)
    defect = filed_defect(crash["body"])
    assert defect is not None and DEFECT_MARKER in first and DEFECT_MARKER not in rest
    assert (defect.run_id, defect.engine_id, defect.audited_repo) == ("a", ENGINE.engine_id, ENGINE.repo)
    assert {(s.module, s.symbol) for s in defect.profile.sites} == {
        (UPDATE.module, UPDATE.symbol), (FINALIZE.module, FINALIZE.symbol),
    }
    assert defect.profile == next(e.profile for e in run.effects if e.finding_id == CRASH_H1)


def test_one_runs_own_issues_are_left_to_its_merge(tmp_path: Path) -> None:
    """#8700: a design finding related to two stall findings of its own run
    is folded into neither. Applying the run does not undo that: its own
    issues are never matched."""
    first = _heat(1, **{HAND_OFF: {"owner": HAND_OFF_OWNER}})
    non_partial = next(f for f in first["findings"] if f["id"] == NON_PARTIAL)
    non_partial["root_cause"]["owner"] = HAND_OFF_OWNER
    store, host = MemoryRunStore(tmp_path), _host()

    run = _apply(store, host, "a", first, _heat(2))

    assert _effect(run, HAND_OFF)[0] is EffectStatus.FILED


def test_two_stall_findings_about_one_owner_are_two_issues_in_any_runs(tmp_path: Path) -> None:
    """A stall finding's identity is its key, across runs too: an exam case
    and a fix about one owner are two deliverables."""
    second = _heat(2)
    route = next(f for f in second["findings"] if f["id"] == ROUTE_PR)
    exam_case = {
        **json.loads(json.dumps(route)), "id": "route-pr-exam-case", "output": "exam_case",
        "reproduction": {**route["reproduction"], "kind": "exam_case", "case_id": "E-route-the-pr"},
    }
    store, host = MemoryRunStore(tmp_path), _host()
    _apply(store, host, "a", second)

    later = _apply(store, host, "b", {**_heat(2), "findings": [exam_case], "design_findings": []})

    assert _effect(later, "route-pr-exam-case")[0] is EffectStatus.FILED


def test_another_engines_issue_about_the_same_code_is_not_its_defect(tmp_path: Path) -> None:
    """Two engines run io's code; their items share numbers and their logs
    share paths. One engine's finding is never evidence on another's issue."""
    other = EffectIdentity(EngineRef(engine_id="repo-other", repo="other/other", state_dir=Path("/other")), SOURCE)
    store, host = MemoryRunStore(tmp_path), _host()
    _apply(store, host, "a", _heat(1))

    later = _apply(store, host, "b", _heat(2), identity=other)

    assert _effect(later, CRASH_H2)[0] is EffectStatus.FILED


def test_a_defect_two_open_issues_carry_is_evidence_on_the_oldest(tmp_path: Path) -> None:
    """Two earlier runs filed one defect before this rule: a later finding
    about it comments on the oldest and names the other, filing nothing."""
    store, host = MemoryRunStore(tmp_path), _host()
    _apply(store, host, "a", _heat(1))
    crash = next(c for c in host.created if c["number"] == _filed_for(host, CRASH_H1))
    host.open.append(OpenIssueLabels(number=100, title="An older run's crash issue", labels=()))
    host.bodies[100] = crash["body"].replace('"run_id":"a"', '"run_id":"older"')

    later = _apply(store, host, "b", _heat(2))

    assert _effect(later, CRASH_H2) == (EffectStatus.COMMENTED, 100)
    [body] = [b for n, b in host.comments if n == 100]
    assert f"#{crash['number']}" in body


def test_quoted_text_cannot_end_or_plant_the_defect_marker(tmp_path: Path) -> None:
    """A quote is the improver's text: in the marker's JSON it cannot close
    the comment or open another, and it reads back as written."""
    quote = "the tick crashed --> <!-- io-improver-defect:{} --> on #450"
    design = _chain_design("crash-quoting-markers", _log(10, quote))
    store, host = MemoryRunStore(tmp_path), _host()

    _apply(store, host, "a", {**_heat(1), "findings": [], "design_findings": [design]})

    [created] = host.created
    first, rest = created["body"].split("\n", 1)
    assert first.count("-->") == 2 and first.count("<!--") == 2 and "<!--" not in rest
    defect = filed_defect(created["body"])
    assert defect is not None and defect.profile.quotes == (quote,)


def test_a_marker_that_is_not_a_defect_stops_the_batch_loudly(tmp_path: Path) -> None:
    """Ignoring it would file again a defect that issue already carries."""
    store, host = MemoryRunStore(tmp_path), _host()
    host.open.append(OpenIssueLabels(number=100, title="Edited by hand", labels=()))
    host.bodies[100] = f"{DEFECT_MARKER}{{not json}} -->\nThe operator's notes."

    run = _apply(store, host, "a", _heat(1))

    assert host.created == [] and len(run.pending_effects) == len(run.effects)
    assert "open issue #100's defect marker records no defect" in (run.effects[0].error or "")
