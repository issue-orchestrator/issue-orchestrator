"""The improver's champion/challenger rules (#8001)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from issue_orchestrator.contracts.improver_findings import ImproverChange
from issue_orchestrator.contracts.improver_run import ImproverAgentChoice, ImproverProvider
from issue_orchestrator.contracts.improver_toolbox import ImproverMode
from issue_orchestrator.contracts.improver_tournament import TournamentResult
from issue_orchestrator.contracts.improver_variant import ChallengeRecord, ImproverVariant, SnapshotTrial
from issue_orchestrator.domain.improver_champion import (
    ChangeNotApplicable,
    challenger_of,
    challenger_won,
    invited,
    prompt_digest,
    promotion_refusals,
)
from issue_orchestrator.domain.tech_lead_approval import ApprovalVerdict, ApprovalVerdictKind

PROMPT = "You audit the tech lead.\n\nCite the staged evidence for every finding you make.\n"
T0 = datetime(2026, 10, 7, tzinfo=UTC)


def _champion(**over: object) -> ImproverVariant:
    fields = {"agent": ImproverAgentChoice.for_provider(ImproverProvider.CLAUDE), "mode": ImproverMode.EMPOWERED,
              "heats": 2, "budget_minutes": 60, "prompt_sha256": prompt_digest(PROMPT), **over}
    return ImproverVariant.model_validate(fields)


def _change(edit: dict[str, object]) -> ImproverChange:
    # As an agent's answer arrives: JSON.
    return ImproverChange.model_validate_json(
        json.dumps({"edit": edit, "why": "w", "expected_effect": "e", "motivated_by": ["f1"]})
    )


def test_about_one_run_in_ten_is_invited_and_always_the_same_ones() -> None:
    ids = [f"20261007T{n:06d}Z-run" for n in range(5000)]

    chosen = [i for i in ids if invited(i)]

    assert 400 < len(chosen) < 600
    assert chosen == [i for i in ids if invited(i)]
    assert not any(invited(i, rate=0.0) for i in ids) and all(invited(i, rate=1.0) for i in ids)
    with pytest.raises(ValueError):
        invited("x", rate=1.5)


def test_a_prompt_edit_replaces_one_exact_passage() -> None:
    change = _change({"kind": "prompt", "find": "Cite the staged evidence", "replace": "Quote the staged evidence"})

    challenger, prompt = challenger_of(_champion(), PROMPT, change)

    assert prompt == PROMPT.replace("Cite the staged evidence", "Quote the staged evidence")
    assert challenger.prompt_sha256 == prompt_digest(prompt) and challenger.agent == _champion().agent
    assert challenger.id != _champion().id


@pytest.mark.parametrize(
    ("edit", "why"),
    [
        ({"kind": "prompt", "find": "a passage the prompt lacks", "replace": "x"}, "occurs 0 time"),
        ({"kind": "prompt", "find": "the staged evidence", "replace": "x"}, "occurs 1"),
        ({"kind": "heats", "heats": 2}, "leaves the champion as it is"),
        ({"kind": "mode", "mode": "empowered"}, "leaves the champion as it is"),
    ],
)
def test_a_change_that_does_not_apply_to_this_champion_is_refused(edit: dict[str, object], why: str) -> None:
    prompt = PROMPT + "Never guess the staged evidence.\n" if why == "occurs 1" else PROMPT
    champion = _champion(prompt_sha256=prompt_digest(prompt))
    if why == "occurs 1":
        with pytest.raises(ChangeNotApplicable, match="occurs 2 time"):
            challenger_of(champion, prompt, _change(edit))
        return
    with pytest.raises(ChangeNotApplicable, match=why):
        challenger_of(champion, prompt, _change(edit))


def test_each_kind_of_change_changes_only_its_own_setting() -> None:
    base = _champion()
    for edit, field, value in (
        ({"kind": "agent", "provider": "codex", "model": "gpt-5.6-sol"}, "agent",
         ImproverAgentChoice(provider=ImproverProvider.CODEX, model="gpt-5.6-sol")),
        ({"kind": "mode", "mode": "scripted"}, "mode", ImproverMode.SCRIPTED),
        ({"kind": "heats", "heats": 3}, "heats", 3),
        ({"kind": "budget_minutes", "minutes": 90}, "budget_minutes", 90),
    ):
        challenger, prompt = challenger_of(base, PROMPT, _change(edit))
        assert prompt == PROMPT and getattr(challenger, field) == value
        assert challenger.model_copy(update={field: getattr(base, field)}) == base


def test_a_change_can_never_name_the_keys_the_graders_or_the_scoring() -> None:
    for kind in ("answer_key", "graders", "noise_band", "harness"):
        with pytest.raises(ValueError):
            _change({"kind": kind, "value": "x"})


def _result(challenger_above: bool, distinguishable: bool) -> TournamentResult:
    higher, lower = ("challenger", "champion") if challenger_above else ("champion", "challenger")
    return TournamentResult.model_validate({
        "tournament_id": "t", "snapshot_id": "s", "key_items": 1, "max_score": 3, "graders": [], "passes": 3,
        "noise": {"heat": 0.1, "grader": 0.0, "pass_": 0.0, "resolution": 0.5}, "band_ses": 2.0,
        "heat_alpha": 0.05, "arms": [],
        "comparisons": [{"higher": higher, "lower": lower, "gap": 2.0, "band": 1.0, "heat_p": 0.05,
                         "distinguishable": distinguishable}],
        "ranking": [[higher], [lower]] if distinguishable else [[higher, lower]],
        "cost": {"arm_heats": {}, "grader_calls": {}, "grader_seconds": {}},
    })


def test_a_challenger_wins_only_when_told_apart_above_the_champion() -> None:
    assert challenger_won(_result(challenger_above=True, distinguishable=True))
    assert not challenger_won(_result(challenger_above=True, distinguishable=False))
    assert not challenger_won(_result(challenger_above=False, distinguishable=True))


def _challenge(outcome: str, champion: ImproverVariant) -> ChallengeRecord:
    challenger, _ = challenger_of(champion, PROMPT, _change({"kind": "heats", "heats": 3}))
    trial = SnapshotTrial(snapshot_id="s", tournament_id="t", comparison=_result(True, True).comparisons[0],
                          challenger_won=outcome == "won")
    return ChallengeRecord(challenge_id="c1", at=T0, run_id="r", change=_change({"kind": "heats", "heats": 3}),
                           issue="o/r#5", champion=champion, challenger=challenger, trials=(trial,),
                           outcome=outcome)  # type: ignore[arg-type]


APPROVED = ApprovalVerdict(5, ApprovalVerdictKind.MAINTAINER, actor="bruce", event_id=9)


def test_a_challenge_is_promoted_only_if_it_won_against_the_current_champion_and_is_approved() -> None:
    champion = _champion()
    won = _challenge("won", champion)

    assert promotion_refusals(won, current=champion, approval=APPROVED) == []
    assert "did not win" in promotion_refusals(_challenge("lost", champion), current=champion, approval=APPROVED)[0]
    moved_on = _champion(budget_minutes=45)
    assert "the champion is now" in promotion_refusals(won, current=moved_on, approval=APPROVED)[0]
    for kind in (ApprovalVerdictKind.NOT_CLAIMED, ApprovalVerdictKind.BOT_ACTOR,
                 ApprovalVerdictKind.NOT_A_MAINTAINER, ApprovalVerdictKind.CLOSED):
        refusals = promotion_refusals(won, current=champion, approval=ApprovalVerdict(5, kind, actor="x", event_id=1))
        assert refusals and "is not approved" in refusals[0]
