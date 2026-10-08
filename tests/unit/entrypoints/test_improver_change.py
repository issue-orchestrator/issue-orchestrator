"""An invited run's one proposed change to the improver (#8001)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from issue_orchestrator.contracts.improver_run import EffectStatus, ImproverAgentChoice, ImproverProvider, RunOutcome
from issue_orchestrator.contracts.improver_toolbox import ImproverMode
from issue_orchestrator.contracts.improver_variant import ImproverVariant
from issue_orchestrator.domain.improver_champion import CHANGE_ID, ChangeInvitation, prompt_digest
from issue_orchestrator.domain.improver_findings_validation import ImproverFindingsRejected, Rule, validate_findings
from issue_orchestrator.domain.improver_heats import AcceptedHeat, merge_heats
from issue_orchestrator.entrypoints.improver_run import ChangePolicy, HeatPlan, ImproverRun
from issue_orchestrator.execution.improver_effect_applier import ImproverEffects
from issue_orchestrator.execution.improver_investigation import ScriptedInvestigation
from issue_orchestrator.entrypoints.improver_staging import load_staged_evidence
from tests.unit.entrypoints.test_improver_run import NOW, FakeAgent, FakeStager, _request
from tests.unit.improver_support import FakeIssueHost, MemoryRunStore, build_improver_data, example

PROMPT = "THE PROMPT: cite the staged evidence for every finding."
CHAMPION = ImproverVariant(
    agent=ImproverAgentChoice(provider=ImproverProvider.CLAUDE, model="opus"), mode=ImproverMode.SCRIPTED,
    heats=1, budget_minutes=60, prompt_sha256=prompt_digest(PROMPT),
)
INVITED = ChangeInvitation(CHAMPION, PROMPT)


def _with_change(edit: dict[str, object], motivated_by: list[str] | None = None) -> dict:
    doc = example("exam_case")
    doc["improver_change"] = {
        "edit": edit, "why": "the improver never quotes the evidence it cites",
        "expected_effect": "fewer unsupported findings",
        "motivated_by": motivated_by or [doc["findings"][0]["id"]],
    }
    return doc


QUOTE = {"kind": "prompt", "find": "cite the staged evidence", "replace": "quote the staged evidence"}


def _rules(doc: dict, tmp_path: Path, invitation: ChangeInvitation | None) -> frozenset[Rule]:
    evidence = load_staged_evidence(build_improver_data(tmp_path))
    with pytest.raises(ImproverFindingsRejected) as rejected:
        validate_findings(json.dumps(doc), evidence, invitation=invitation)
    return rejected.value.rules


def test_an_invited_runs_change_is_accepted(tmp_path: Path) -> None:
    evidence = load_staged_evidence(build_improver_data(tmp_path))

    findings = validate_findings(json.dumps(_with_change(QUOTE)), evidence, invitation=INVITED)

    assert findings.improver_change is not None and findings.improver_change.edit.kind == "prompt"


def test_a_change_without_an_invitation_rejects_the_file(tmp_path: Path) -> None:
    assert _rules(_with_change(QUOTE), tmp_path, invitation=None) == {Rule.IMPROVER_CHANGE_INVITED}


def test_a_change_must_be_motivated_by_the_files_own_findings(tmp_path: Path) -> None:
    assert _rules(_with_change(QUOTE, ["no-such-finding"]), tmp_path, INVITED) == {Rule.IMPROVER_CHANGE_MOTIVATED}


@pytest.mark.parametrize(
    "edit",
    [
        {"kind": "prompt", "find": "a passage the prompt does not have", "replace": "x"},
        {"kind": "mode", "mode": "scripted"},  # the champion's mode already
        {"kind": "agent", "provider": "claude", "model": "opus"},  # the champion's agent already
    ],
)
def test_a_change_must_apply_to_the_invited_champion(edit: dict[str, object], tmp_path: Path) -> None:
    assert _rules(_with_change(edit), tmp_path, INVITED) == {Rule.IMPROVER_CHANGE_APPLIES}


def test_a_change_may_only_touch_the_improvers_own_settings(tmp_path: Path) -> None:
    edits = ({"kind": "answer_key", "item": "1"}, {"kind": "graders", "graders": []},
             {"kind": "heats", "heats": 9}, {"kind": "budget_minutes", "minutes": 5})
    for n, edit in enumerate(edits):
        assert Rule.SCHEMA in _rules(_with_change(edit), tmp_path / str(n), INVITED)


def test_only_the_primary_heats_change_is_carried_and_another_is_shown(tmp_path: Path) -> None:
    evidence = load_staged_evidence(build_improver_data(tmp_path))
    first = validate_findings(json.dumps(_with_change(QUOTE)), evidence, invitation=INVITED)
    other = validate_findings(
        json.dumps(_with_change({"kind": "budget_minutes", "minutes": 90})), evidence, invitation=INVITED
    )

    merged = merge_heats([AcceptedHeat(1, first), AcceptedHeat(2, other)], lambda f: f.id, lambda d: d.id)

    assert merged.findings.improver_change == first.improver_change
    [conflict] = [c for c in merged.conflicts if c.finding_id == CHANGE_ID]
    assert conflict.heat == 2 and "budget_minutes" in conflict.claim


# -- the run ---------------------------------------------------------------------


def _champion_run(store: MemoryRunStore, host: FakeIssueHost, agent: FakeAgent, rate: float) -> ImproverRun:
    clock = iter(NOW + timedelta(minutes=n) for n in range(1000))
    return ImproverRun(
        store=store, stager=FakeStager(), agent=agent, investigation=ScriptedInvestigation(),
        effects=ImproverEffects(store=store, host=host, outputs_repo="issue-orchestrator/issue-orchestrator",
                                clock=lambda: NOW),
        prompt=PROMPT, heats=HeatPlan(1, 1), clock=lambda: next(clock),
        change_policy=ChangePolicy(CHAMPION, PROMPT, addendum="INVITED to change <<CHAMPION>>", rate=rate),
    )


def test_an_invited_run_is_asked_and_its_change_files_a_challenger_issue(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = FakeAgent(json.dumps(_with_change(QUOTE)))

    record = _champion_run(store, host, agent, rate=1.0).run(_request())

    assert record.outcome is RunOutcome.ACCEPTED and record.change_invitation == CHAMPION.id
    assert agent.prompts[0].endswith(f"INVITED to change `{CHAMPION.id}` ({CHAMPION.describe()})")
    [receipt] = [e for e in record.effects if e.finding_id == CHANGE_ID]
    assert receipt.status is EffectStatus.FILED
    [issue] = [c for c in host.created if c["number"] == receipt.issue_number]
    assert "Improver challenger (prompt)" in issue["title"] and "improver:challenger" in issue["labels"]
    assert "challenge --run " + record.run_id in issue["body"] and "`approved`" in issue["body"]


def test_an_uninvited_run_is_not_asked_and_its_change_rejects_its_answer(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = FakeAgent(json.dumps(_with_change(QUOTE)))

    record = _champion_run(store, host, agent, rate=0.0).run(_request())

    assert record.change_invitation is None and "INVITED" not in agent.prompts[0]
    assert record.outcome is RunOutcome.REJECTED
    assert any("improver_change_invited" in r for h in record.heats for r in h.rejections)
    assert host.created == []


def test_a_run_that_does_not_run_the_champion_cannot_be_invited(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    with pytest.raises(ValueError, match=r"\['prompt'\] differ"):
        ImproverRun(
            store=store, stager=FakeStager(), agent=FakeAgent(None), investigation=ScriptedInvestigation(),
            effects=ImproverEffects(store=store, host=host, outputs_repo="o/r", clock=lambda: NOW),
            prompt="ANOTHER PROMPT", heats=HeatPlan(1, 1), clock=lambda: NOW,
            change_policy=ChangePolicy(CHAMPION, PROMPT, addendum="x"),
        )


def test_a_blind_run_is_never_invited(tmp_path: Path) -> None:
    store, host = MemoryRunStore(tmp_path), FakeIssueHost()
    agent = FakeAgent(json.dumps(example("exam_case")))
    request = _request()
    blind = type(request)(**{**request.__dict__, "excluded_open_issues": frozenset({1})})

    record = _champion_run(store, host, agent, rate=1.0).run(blind, apply=False)

    assert record.change_invitation is None and "INVITED" not in agent.prompts[0]
