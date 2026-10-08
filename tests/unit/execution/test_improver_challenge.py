"""The improver's champion and challengers, end to end with fakes (#8001)."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest

from issue_orchestrator.contracts.improver_run import EffectStatus, ImproverAgentChoice, ImproverProvider, RunOutcome
from issue_orchestrator.contracts.improver_toolbox import ImproverMode
from issue_orchestrator.contracts.improver_variant import ImproverVariant
from issue_orchestrator.domain.improver_champion import (
    CHANGE_ID,
    ChallengerIssueFacts,
    judge_challenger_issue,
    prompt_digest,
)
from issue_orchestrator.domain.tech_lead_approval import ApprovalVerdictKind, LabelEvent
from issue_orchestrator.entrypoints.improver_run import ChangePolicy, HeatPlan, ImproverRun
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.improver_answer_keys import FileAnswerKeyStore
from issue_orchestrator.execution.improver_challenge import ChallengeRefused, ImproverChallenges
from issue_orchestrator.execution.improver_champion_store import ChampionUnavailable, FileChampionStore
from issue_orchestrator.execution.improver_effect_applier import ImproverEffects
from issue_orchestrator.execution.improver_investigation import ScriptedInvestigation
from issue_orchestrator.execution.improver_run_store import FileImproverRunStore
from issue_orchestrator.execution.improver_snapshots import FrozenSnapshotStore
from issue_orchestrator.execution.improver_tournament import TournamentHarness
from issue_orchestrator.ports.improver import HeatSpace, ImproverAgentResult
from tests.unit.domain.test_improver_tournament import SEALED
from tests.unit.entrypoints.test_improver_run import FakeStager, _request
from tests.unit.execution.test_improver_tournament_harness import T0, _legacy_inputs
from tests.unit.improver_support import FakeIssueHost, example

PROMPT = "You audit the tech lead. Cite the staged evidence for every finding."
QUOTE = {"kind": "prompt", "find": "Cite the staged evidence", "replace": "Quote the staged evidence"}
GRADER_PROMPT = (Path(__file__).resolve().parents[3] / "examples" / "prompts" / "improver-grader.md").read_text()


def _variant(prompt: str = PROMPT) -> ImproverVariant:
    return ImproverVariant(
        agent=ImproverAgentChoice(provider=ImproverProvider.CLAUDE, model="opus"), mode=ImproverMode.SCRIPTED,
        heats=1, budget_minutes=60, prompt_sha256=prompt_digest(prompt),
    )


def _answer(quoted: bool) -> str:
    doc = example("exam_case")
    if quoted:
        doc["findings"][0]["root_cause"]["why"] += " QUOTED"
    return json.dumps(doc)


class Agents:
    """Improver agents answer by the prompt they ran with (the challenger's
    says "Quote"); graders give full credit to answers that quote."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def agent_for(self, choice: ImproverAgentChoice, minutes: int):  # type: ignore[no-untyped-def]
        agents = self

        class Agent:
            def __init__(self) -> None:
                self.choice = choice

            def run(self, *, prompt: str, space: HeatSpace, toolbox: object) -> ImproverAgentResult:
                with agents._lock:
                    agents.calls.append("grader" if "Improver tournament grader" in prompt else "arm")
                if "Improver tournament grader" not in prompt:
                    return ImproverAgentResult(_answer("Quote the staged evidence" in prompt), "done")
                anon = space.run_dir / "anon"
                grades = {}
                for path in sorted(anon.glob("*.json")):
                    grade = "full" if "QUOTED" in path.read_text() else "miss"
                    grades[path.stem] = {"items": {i: {"grade": grade, "why": "q"} for i in ("1", "2", "9")},
                                         "unsupported": 0}
                return ImproverAgentResult(json.dumps(grades), "graded")

        return Agent()


@dataclass
class Issues:
    """A challenger issue on GitHub, as the approval reads it."""

    state: str = "open"
    labels: tuple[str, ...] = ("improver", "improver:challenger")
    added: LabelEvent | None = None
    removed: LabelEvent | None = None
    roles: dict[str, str] = field(default_factory=dict)
    closed_after: bool = False

    def get_issue(self, issue_number: int):  # type: ignore[no-untyped-def]
        return type("Issue", (), {"state": self.state, "labels": self.labels})()

    def latest_label_event(self, issue_number: int, label: str, *, removed: bool = False) -> LabelEvent | None:
        return self.removed if removed else self.added

    def repository_role(self, login: str) -> str | None:
        return self.roles.get(login)

    def issue_closed_on_or_after(self, issue_number: int, timestamp: str) -> bool:
        return self.closed_after

    def approve(self, login: str, *, role: str, event_id: int = 101, bot: bool = False) -> None:
        self.labels = (*self.labels, "approved")
        self.added = LabelEvent(event_id=event_id, actor_login=login, actor_is_bot=bot, created_at="2026-10-08T10:00:00Z")
        self.roles[login] = role


@pytest.fixture
def cycle(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = tmp_path / "io-improver"
    snapshots = FrozenSnapshotStore(root, LocalCommandRunner())
    snapshots.import_("20261004", improver_data=_legacy_inputs(tmp_path / "src"), taken_at=T0, origin="test")
    FileAnswerKeyStore(root).seed_sealed("20261004", SEALED, sealed_at=T0, added_by="coordinator")
    champions = FileChampionStore(root)
    champions.seed(_variant(), PROMPT, at=T0, by="operator")
    runs = FileImproverRunStore(root)
    agents, issues = Agents(), Issues()
    harness = TournamentHarness(root=root, snapshots=snapshots, keys=FileAnswerKeyStore(root),
                                agent_for=agents.agent_for, grader_prompt=GRADER_PROMPT, clock=lambda: T0)
    challenges = ImproverChallenges(harness=harness, champions=champions, runs=runs, issues_for=lambda repo: issues,
                                    empowered_addendum="", clock=lambda: T0 + timedelta(days=2))
    return root, champions, runs, agents, issues, challenges


def _invited_run(runs: FileImproverRunStore, change: dict | None = QUOTE, *, rate: float = 1.0) -> str:
    """An invited champion run proposing ``change``, its issue filed."""
    doc = json.loads(_answer(False))
    if change is not None:
        doc["improver_change"] = {"edit": change, "why": "the improver paraphrases evidence",
                                  "expected_effect": "graders find the quotes", "motivated_by": [doc["findings"][0]["id"]]}

    class Agent:
        choice = _variant().agent

        def run(self, *, prompt: str, space: HeatSpace, toolbox: object) -> ImproverAgentResult:
            return ImproverAgentResult(json.dumps(doc), "done")

    clock = iter(T0 + timedelta(minutes=n) for n in range(100))
    run = ImproverRun(
        store=runs, stager=FakeStager(), agent=Agent(), investigation=ScriptedInvestigation(),
        effects=ImproverEffects(store=runs, host=FakeIssueHost(), outputs_repo="issue-orchestrator/issue-orchestrator",
                                clock=lambda: T0),
        prompt=PROMPT, heats=HeatPlan(1, 1), clock=lambda: next(clock),
        change_policy=ChangePolicy(_variant(), PROMPT, addendum="<<CHAMPION>>", rate=rate),
    )
    record = run.run(_request())
    assert record.outcome is RunOutcome.ACCEPTED, record.rejections or [h.rejections for h in record.heats]
    return record.run_id


def test_a_challenger_that_wins_and_is_approved_becomes_the_champion(cycle) -> None:  # type: ignore[no-untyped-def]
    root, champions, runs, agents, issues, challenges = cycle
    run_id = _invited_run(runs)

    record = challenges.challenge(run_id, ["20261004"], whole_runs=3, passes=1, seed=5)

    assert record.outcome == "won" and record.issue.startswith("issue-orchestrator/issue-orchestrator#")
    [trial] = record.trials
    assert trial.challenger_won and trial.comparison.higher == "challenger" and trial.comparison.heat_p == 0.05
    assert record.challenger.prompt_sha256 == prompt_digest(PROMPT.replace("Cite the", "Quote the"))
    # Three whole runs an arm (one heat each), then each grader once.
    assert agents.calls.count("arm") == 6 and agents.calls.count("grader") == 2
    assert champions.challenge(record.challenge_id) == record

    refused = challenges.promote(record.challenge_id)
    assert refused.state is None and "is not approved" in refused.refusals[0]
    assert champions.state().champion == _variant()

    issues.approve("bruce", role="admin")
    promoted = challenges.promote(record.challenge_id)

    assert promoted.state is not None and promoted.state.champion == record.challenger
    [promotion] = promoted.state.promotions
    assert (promotion.previous, promotion.approved_by, promotion.issue) == (_variant(), "bruce", record.issue)
    assert champions.prompt(record.challenger.prompt_sha256) == PROMPT.replace("Cite the", "Quote the")


def test_a_losing_challenger_is_never_promoted_whoever_approves_it(cycle) -> None:  # type: ignore[no-untyped-def]
    _, champions, runs, _, issues, challenges = cycle
    # A change that does not make the improver quote: the challenger answers as the champion does.
    run_id = _invited_run(runs, {"kind": "prompt", "find": "You audit the tech lead.", "replace": "Audit the tech lead."})

    record = challenges.challenge(run_id, ["20261004"], whole_runs=3, passes=1, seed=5)
    issues.approve("bruce", role="admin")
    promoted = challenges.promote(record.challenge_id)

    assert record.outcome == "lost" and promoted.state is None
    assert any("did not win" in r for r in promoted.refusals) and champions.state().champion == _variant()


def test_a_challenge_tried_against_an_old_champion_is_not_promoted(cycle) -> None:  # type: ignore[no-untyped-def]
    _, champions, runs, _, issues, challenges = cycle
    first, second = _invited_run(runs), _invited_run(runs, {"kind": "budget_minutes", "minutes": 90})
    won = challenges.challenge(first, ["20261004"], whole_runs=3, passes=1, seed=5)
    issues.approve("bruce", role="maintain")
    assert challenges.promote(won.challenge_id).state is not None

    # The second run proposed against the champion that is no more.
    with pytest.raises(ChallengeRefused, match="the champion is now"):
        challenges.challenge(second, ["20261004"], whole_runs=3, passes=1, seed=5)


def test_a_change_that_cannot_be_tried_is_refused(cycle) -> None:  # type: ignore[no-untyped-def]
    _, _, runs, _, _, challenges = cycle
    uninvited = _invited_run(runs, None, rate=0.0)

    with pytest.raises(ChallengeRefused, match="not an accepted run invited"):
        challenges.challenge(uninvited, ["20261004"], seed=1)
    with pytest.raises(ChallengeRefused, match="no improver run"):
        challenges.challenge("nope", ["20261004"], seed=1)
    with pytest.raises(ChallengeRefused, match="distinct snapshots"):
        challenges.challenge(uninvited, [], seed=1)


def test_a_challenge_whose_issue_is_not_filed_yet_cannot_be_approved_so_is_not_tried(cycle) -> None:  # type: ignore[no-untyped-def]
    _, _, runs, _, _, challenges = cycle
    run_id = _invited_run(runs)
    record = next(r for r in runs.runs() if r.run_id == run_id)
    pending = tuple(e.model_copy(update={"status": EffectStatus.PENDING, "issue_number": None})
                    if e.finding_id == CHANGE_ID else e for e in record.effects)
    runs.record(record.model_copy(update={"effects": pending}))

    with pytest.raises(ChallengeRefused, match="has no issue yet"):
        challenges.challenge(run_id, ["20261004"], seed=1)


def test_an_interrupted_challenge_resumes_without_running_its_arms_again(cycle) -> None:  # type: ignore[no-untyped-def]
    root, champions, runs, agents, _, challenges = cycle
    run_id = _invited_run(runs)
    first = challenges.challenge(run_id, ["20261004"], whole_runs=3, passes=1, seed=5)
    # The trial was recorded once; pretend the record was lost after the tournament finished.
    (root / "champion" / "challenges" / f"{first.challenge_id}.json").unlink()
    arm_calls = agents.calls.count("arm")

    again = challenges.challenge(run_id, ["20261004"], whole_runs=3, passes=1, seed=5)

    assert agents.calls.count("arm") == arm_calls and again.trials == first.trials


def test_the_champion_changes_only_by_a_winning_promotion(tmp_path: Path) -> None:
    champions = FileChampionStore(tmp_path)
    with pytest.raises(ChampionUnavailable, match="no improver champion"):
        champions.state()
    champions.seed(_variant(), PROMPT, at=T0, by="operator")
    with pytest.raises(ChampionUnavailable, match="seeded already"):
        champions.seed(_variant(), PROMPT, at=T0, by="operator")
    with pytest.raises(ChampionUnavailable, match="does not match"):
        FileChampionStore(tmp_path / "other").seed(_variant(), "another prompt", at=T0, by="operator")


def test_the_store_promotes_only_against_the_champion_current_under_its_lock(tmp_path: Path) -> None:
    """Two promotions racing: the second was tried against a champion the
    first replaced, so the store refuses it however the caller checked."""
    from issue_orchestrator.contracts.improver_findings import ImproverChange
    from issue_orchestrator.contracts.improver_variant import ChallengeRecord, SnapshotTrial
    from tests.unit.domain.test_improver_champion import _result

    champions = FileChampionStore(tmp_path)
    champions.seed(_variant(), PROMPT, at=T0, by="operator")
    change = ImproverChange.model_validate_json(json.dumps(
        {"edit": {"kind": "heats", "heats": 3}, "why": "w", "expected_effect": "e", "motivated_by": ["f1"]}
    ))
    trial = SnapshotTrial(snapshot_id="s", tournament_id="t", comparison=_result(True, True).comparisons[0],
                          challenger_won=True)

    def won(challenge_id: str, challenger: ImproverVariant) -> ChallengeRecord:
        return ChallengeRecord(challenge_id=challenge_id, at=T0, run_id="r", change=change, issue="o/r#1",
                               champion=_variant(), challenger=challenger, trials=(trial,), outcome="won")

    champions.promote(won("c1", _variant().model_copy(update={"heats": 3})), at=T0, approved_by="bruce")
    with pytest.raises(ChampionUnavailable, match="the champion is now"):
        champions.promote(won("c2", _variant().model_copy(update={"heats": 4})), at=T0, approved_by="bruce")
    assert champions.state().champion.heats == 3 and len(champions.state().promotions) == 1


# -- the approval ------------------------------------------------------------------


def _facts(**over: object) -> ChallengerIssueFacts:
    added = LabelEvent(event_id=10, actor_login="bruce", actor_is_bot=False, created_at="2026-10-08T10:00:00Z")
    fields = {"number": 5, "state": "open", "labels": frozenset({"improver", "approved"}), "approved_added": added,
              "approved_removed": None, "approver_role": "admin", "closed_since_approval": False, **over}
    return ChallengerIssueFacts(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("over", "kind"),
    [
        ({}, ApprovalVerdictKind.MAINTAINER),
        ({"approver_role": "maintain"}, ApprovalVerdictKind.MAINTAINER),
        ({"approver_role": "write"}, ApprovalVerdictKind.NOT_A_MAINTAINER),
        ({"approver_role": None}, ApprovalVerdictKind.NOT_A_MAINTAINER),
        ({"approved_added": LabelEvent(event_id=10, actor_login="io[bot]", actor_is_bot=True, created_at="x")},
         ApprovalVerdictKind.BOT_ACTOR),
        ({"labels": frozenset({"improver"})}, ApprovalVerdictKind.NOT_CLAIMED),
        ({"approved_added": None}, ApprovalVerdictKind.NO_LABEL_EVENT),
        ({"approved_removed": LabelEvent(event_id=11, actor_login="bruce", actor_is_bot=False, created_at="y")},
         ApprovalVerdictKind.NO_LABEL_EVENT),
        ({"state": "closed"}, ApprovalVerdictKind.CLOSED),
        ({"state": None}, ApprovalVerdictKind.CLOSED),
        ({"closed_since_approval": True}, ApprovalVerdictKind.CLOSED),
    ],
)
def test_only_a_standing_maintainers_approved_label_on_an_open_issue_approves(over: dict, kind: ApprovalVerdictKind) -> None:
    verdict = judge_challenger_issue(_facts(**over))

    assert verdict.kind is kind and verdict.approved == (kind is ApprovalVerdictKind.MAINTAINER)
