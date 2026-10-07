"""The productized improver tournament: snapshots, keys, arms, graders (#8001)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from issue_orchestrator.contracts.improver_run import ImproverAgentChoice, ImproverProvider
from issue_orchestrator.contracts.improver_tournament import AnswerKeyItem, TournamentArm
from issue_orchestrator.entrypoints.improver_run import HeatPlan
from issue_orchestrator.entrypoints.improver_staging import load_staged_evidence
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.improver_answer_keys import AnswerKeyError, FileAnswerKeyStore
from issue_orchestrator.execution.improver_snapshots import FrozenSnapshotStore, SnapshotUnavailable
from issue_orchestrator.execution.improver_tournament import ArmOutput, ArmSpec, Grader, TournamentHarness
from issue_orchestrator.ports.improver import HeatSpace, ImproverAgentResult
from tests.unit.domain.test_improver_tournament import SEALED
from tests.unit.improver_support import build_improver_data, example

T0 = datetime(2026, 10, 4, 7, 36, tzinfo=UTC)


def _legacy_inputs(root: Path) -> Path:
    """A staged bundle as io wrote it before #8001's operator hand actions."""
    data = build_improver_data(root)
    (data / "interventions.json").write_text(json.dumps({
        "window_from": "2026-09-27T18:00:00Z", "window_to": "2026-09-28T18:00:00Z", "complete": False,
        "derived_from": ["charter ledger: proposal approvals and declines"],
        "not_derivable": ["needs-human or other labels removed by a human on GitHub"],
        "interventions": [
            {"at": "2026-09-28T16:00:00Z", "kind": "proposal_approved", "subject": "#900", "detail": "kill (d1)"},
            {"at": "2026-09-28T16:30:00Z", "kind": "operator_pause", "subject": "engine", "detail": "cli: operator"},
        ],
    }))
    return data


def _engine_files(root: Path) -> tuple[Path, Path]:
    state = root / "state"
    (state / "logs").mkdir(parents=True)
    with sqlite3.connect(state / "timeline.sqlite") as conn:
        conn.execute("CREATE TABLE timeline (event TEXT)")
        conn.execute("INSERT INTO timeline VALUES ('review.skipped')")
    (state / "logs" / "orchestrator.log").write_text("2026-10-04 02:00:01 INFO auth_expired\n")
    clone = root / "clone"
    clone.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    return state, clone


@pytest.fixture
def stores(tmp_path: Path) -> tuple[FrozenSnapshotStore, FileAnswerKeyStore, Path]:
    root = tmp_path / "io-improver"
    snapshots = FrozenSnapshotStore(root, LocalCommandRunner())
    state, clone = _engine_files(tmp_path)
    snapshots.import_("20261004", improver_data=_legacy_inputs(tmp_path / "src"), taken_at=T0,
                      origin="test", state_dir=state, clone=clone)
    keys = FileAnswerKeyStore(root)
    keys.seed_sealed("20261004", SEALED, sealed_at=T0, added_by="coordinator")
    return snapshots, keys, root


# -- frozen snapshots ----------------------------------------------------------


def test_an_old_staged_bundle_is_frozen_upgraded_and_loads_with_todays_contracts(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    snapshots, _, root = stores

    snapshot = snapshots.get("20261004")

    assert snapshot.has_toolbox and snapshot.taken_at == T0 and snapshot.audited_repo == "porchpin/porchpin"
    assert snapshot.upgrades and "interventions.json" in snapshot.upgrades[0]
    data = root / "snapshots" / "20261004" / "improver-data"
    interventions = json.loads((data / "interventions.json").read_text())
    assert [(i["source"], i["attribution"]) for i in interventions["interventions"]] == [
        ("charter_ledger", "operator_surface"), ("pause_journal", "operator_surface"),
    ]
    assert interventions["github"]["read"] is False
    load_staged_evidence(data)
    toolbox = root / "snapshots" / "20261004" / "toolbox"
    assert (toolbox / "state" / "timeline.sqlite").is_file() and (toolbox / "logs" / "orchestrator.log").is_file()
    assert (toolbox / "repo" / ".git").is_dir()


def test_a_snapshot_is_never_changed_or_half_made(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    snapshots, _, root = stores
    with pytest.raises(SnapshotUnavailable, match="never changed"):
        snapshots.import_("20261004", improver_data=_legacy_inputs(tmp_path / "again"), taken_at=T0, origin="x")
    state, _ = _engine_files(tmp_path / "half")
    with pytest.raises(SnapshotUnavailable, match="needs both"):
        snapshots.import_("half", improver_data=_legacy_inputs(tmp_path / "h"), taken_at=T0, origin="x", state_dir=state)
    broken = _legacy_inputs(tmp_path / "broken")
    (broken / "audit.json").write_text("{}")
    with pytest.raises(SnapshotUnavailable, match="do not load"):
        snapshots.import_("broken", improver_data=broken, taken_at=T0, origin="x")
    assert snapshots.ids() == ("20261004",) and not (root / "snapshots" / "broken").exists()


def test_a_snapshot_never_links_outside_itself(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A link out of a snapshot would show later evidence: a clone whose
    .git is a live repository's, or a staged file pointing at a live one."""
    snapshots, _, root = stores
    live = tmp_path / "live"
    live.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=live, check=True)
    state, _ = _engine_files(tmp_path / "e")
    linked = tmp_path / "linked-clone"
    linked.mkdir()
    (linked / ".git").symlink_to(live / ".git")
    with pytest.raises(SnapshotUnavailable, match="not the clone's own directory|not a Git clone"):
        snapshots.import_("linked", improver_data=_legacy_inputs(tmp_path / "a"), taken_at=T0, origin="x",
                          state_dir=state, clone=linked)
    data = _legacy_inputs(tmp_path / "b")
    (data / "later.md").symlink_to(live / "README")
    with pytest.raises(SnapshotUnavailable, match="later.md links outside the snapshot"):
        snapshots.import_("leaky", improver_data=data, taken_at=T0, origin="x")
    escaping = _legacy_inputs(tmp_path / "c")
    (escaping / "up.md").symlink_to("../../outside.md")
    with pytest.raises(SnapshotUnavailable, match="up.md links outside"):
        snapshots.import_("escaping", improver_data=escaping, taken_at=T0, origin="x")
    assert snapshots.ids() == ("20261004",)
    assert not any((root / "snapshots" / name).exists() for name in ("linked", "leaky", "escaping"))
    # A link that stays inside (a bundle's CLAUDE.md -> AGENTS.md) is fine.
    inside = _legacy_inputs(tmp_path / "d")
    (inside / "AGENTS.md").write_text("agents")
    (inside / "CLAUDE.md").symlink_to("AGENTS.md")
    snapshots.import_("inside", improver_data=inside, taken_at=T0, origin="x")


def test_a_clone_that_borrows_objects_is_frozen_owning_them(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """``git clone --shared`` borrows the live repository's objects; a
    snapshot must own them, so it keeps working whatever the live one does."""
    snapshots, _, root = stores
    live = tmp_path / "live"
    live.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    before = {"GIT_AUTHOR_DATE": (T0 - timedelta(hours=1)).isoformat(),
              "GIT_COMMITTER_DATE": (T0 - timedelta(hours=1)).isoformat(), "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q"], cwd=live, check=True)
    (live / "f").write_text("one")
    subprocess.run([*git, "add", "f"], cwd=live, check=True)
    subprocess.run([*git, "commit", "-qm", "one"], cwd=live, check=True, env=before)
    shared = tmp_path / "shared"
    subprocess.run(["git", "clone", "-q", "--shared", str(live), str(shared)], check=True)
    assert (shared / ".git" / "objects" / "info" / "alternates").exists()
    state, _ = _engine_files(tmp_path / "e")

    snapshots.import_("shared", improver_data=_legacy_inputs(tmp_path / "a"), taken_at=T0, origin="x",
                      state_dir=state, clone=shared)

    frozen = root / "snapshots" / "shared" / "toolbox" / "repo"
    assert not (frozen / ".git" / "objects" / "info" / "alternates").exists()
    shutil.rmtree(live)
    shown = subprocess.run(["git", "-C", str(frozen), "show", "HEAD:f"], capture_output=True, text=True, check=True)
    assert shown.stdout == "one"


def test_a_clone_showing_commits_after_the_snapshot_is_refused(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A clone taken later than the staged inputs would show the arms what
    was done since; and what no ref reaches (a dropped commit) is not kept."""
    snapshots, _, root = stores
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    def commit(message: str, when: datetime, *, ref: str | None = None) -> str:
        env = {"GIT_AUTHOR_DATE": when.isoformat(), "GIT_COMMITTER_DATE": when.isoformat(), "PATH": "/usr/bin:/bin"}
        (repo / "f").write_text(message)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "add", "f"], cwd=repo, check=True, env=env)
        subprocess.run([*git, "commit", "-qm", message], cwd=repo, check=True, env=env)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    commit("before", T0 - timedelta(hours=1))
    dropped = commit("after, then dropped", T0 + timedelta(days=2))
    subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=repo, check=True)
    state, _ = _engine_files(tmp_path / "e")

    snapshots.import_("in-time", improver_data=_legacy_inputs(tmp_path / "a"), taken_at=T0, origin="x",
                      state_dir=state, clone=repo)
    frozen = root / "snapshots" / "in-time" / "toolbox" / "repo"
    shown = subprocess.run(["git", "-C", str(frozen), "cat-file", "-e", dropped], capture_output=True)
    assert shown.returncode != 0, "a commit no ref reaches was kept in the snapshot"

    later = commit("after", T0 + timedelta(days=3))
    with pytest.raises(SnapshotUnavailable, match=r"reaches 1 commit\(s\) made after the snapshot.*" + later[:12]):
        snapshots.import_("late", improver_data=_legacy_inputs(tmp_path / "b"), taken_at=T0, origin="x",
                          state_dir=state, clone=repo)
    assert "late" not in snapshots.ids()


def test_a_scripted_only_snapshot_has_no_toolbox_to_copy(stores, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    snapshots, _, _ = stores
    snapshots.import_("bundle-only", improver_data=_legacy_inputs(tmp_path / "b"), taken_at=T0, origin="x")
    run = tmp_path / "run"
    run.mkdir()

    snapshots.copy_inputs("bundle-only", run)
    with pytest.raises(SnapshotUnavailable, match="scripted arms only"):
        snapshots.copy_toolbox("bundle-only", run)


# -- answer keys ---------------------------------------------------------------


def _hindsight(item_id: str = "H-7999", *, by: str = "coordinator", confirmed: bool = True) -> AnswerKeyItem:
    return AnswerKeyItem(
        id=item_id, weight=2, title="Review admitted onto a PR whose rework is live", description="d",
        category="stall", source="hindsight", status="confirmed" if confirmed else "candidate",
        links=("issue-orchestrator/issue-orchestrator#7999",), filed_at=T0.date(), added_at=T0, added_by=by,
    )


def test_hindsight_items_join_the_sealed_key_and_only_confirmed_ones_score(stores) -> None:  # type: ignore[no-untyped-def]
    _, keys, _ = stores

    keys.add(_hindsight("H-7999"), snapshot_id="20261004")
    key = keys.add(_hindsight("H-8137", confirmed=False), snapshot_id="20261004")

    assert [i.id for i in key.scored] == ["1", "2", "9", "H-7999"] and key.max_score == 10
    assert keys.confirm("20261004", "H-8137", by="operator").max_score == 12


@pytest.mark.parametrize("by", ["improver", "Improver", " the improver "])
def test_the_improver_never_writes_its_own_key(stores, by: str) -> None:  # type: ignore[no-untyped-def]
    _, keys, _ = stores
    with pytest.raises(AnswerKeyError, match="never writes its own"):
        keys.add(_hindsight(by=by), snapshot_id="20261004")
    with pytest.raises(AnswerKeyError, match="never writes its own"):
        keys.confirm("20261004", "1", by=by)


def test_a_key_is_seeded_once_and_an_item_added_once(stores) -> None:  # type: ignore[no-untyped-def]
    _, keys, _ = stores
    with pytest.raises(AnswerKeyError, match="already has a key"):
        keys.seed_sealed("20261004", SEALED, sealed_at=T0, added_by="coordinator")
    keys.add(_hindsight(), snapshot_id="20261004")
    with pytest.raises(AnswerKeyError, match="exists"):
        keys.add(_hindsight(), snapshot_id="20261004")
    with pytest.raises(AnswerKeyError, match="only hindsight"):
        keys.add(_hindsight().model_copy(update={"id": "H-x", "source": "sealed_key"}), snapshot_id="20261004")


def test_no_improver_run_module_can_reach_the_answer_keys() -> None:
    """The improver never writes its own key: nothing an improver run is
    built from imports the key store (only the tournament's key commands do)."""
    src = Path(__file__).resolve().parents[3] / "src" / "issue_orchestrator"
    run_path = [
        "entrypoints/improver_run.py", "entrypoints/improver_staging.py", "entrypoints/cli_tools/improver.py",
        "execution/improver_agents.py", "execution/claude_improver_agent.py", "execution/codex_improver_agent.py",
        "execution/improver_investigation.py", "execution/improver_toolbox.py", "execution/improver_effect_applier.py",
    ]
    for module in run_path:
        assert "improver_answer_keys" not in (src / module).read_text(), module


# -- the harness -----------------------------------------------------------------


class Agents:
    """Fake agents: arms answer by arm, graders grade every output."""

    def __init__(self, arm_answers: dict[str, str | None], grader_answers: dict[str, object]) -> None:
        self.arm_answers = arm_answers
        self.grader_answers = grader_answers
        self.spaces: list[tuple[str, HeatSpace, object]] = []
        self.timeouts: dict[str, list[int]] = {}
        self._lock = threading.Lock()

    def agent_for(self, choice: ImproverAgentChoice, minutes: int):  # type: ignore[no-untyped-def]
        agents = self
        self.timeouts.setdefault(choice.model, []).append(minutes)

        class Agent:
            def __init__(self) -> None:
                self.choice = choice

            def run(self, *, prompt: str, space: HeatSpace, toolbox: object) -> ImproverAgentResult:
                with agents._lock:
                    agents.spaces.append((prompt, space, toolbox))
                if "Improver tournament grader" in prompt:
                    answer = agents.grader_answers[choice.provider.value]
                    labels = sorted(p.stem for p in (space.run_dir / "anon").glob("*.json"))
                    text = answer(labels) if callable(answer) else answer
                    return ImproverAgentResult(text, "graded")  # type: ignore[arg-type]
                arm = choice.model
                return ImproverAgentResult(agents.arm_answers[arm], "done" if agents.arm_answers[arm] else "timed out")

        return Agent()


ADDENDUM = "<<AUDITED_REPO>> budget <<BUDGET_MINUTES>> minutes <<STAGED_AT>>"


def _spec(arm: TournamentArm, *, prompt: str = "THE IMPROVER PROMPT", heats: HeatPlan = HeatPlan(2, 2),
          budget: int = 30, timeout: int = 40) -> ArmSpec:
    return ArmSpec(arm=arm, prompt=prompt, empowered_addendum=ADDENDUM, heats=heats, budget_minutes=budget,
                   agent_timeout_minutes=timeout)


def _harness(root: Path, stores, agents: Agents) -> TournamentHarness:  # type: ignore[no-untyped-def]
    snapshots, keys, _ = stores
    return TournamentHarness(
        root=root, snapshots=snapshots, keys=keys, agent_for=agents.agent_for,
        grader_prompt=(Path(__file__).resolve().parents[3] / "examples" / "prompts" / "improver-grader.md").read_text(),
        clock=lambda: T0 + timedelta(days=3),
    )


def test_arms_run_on_the_frozen_snapshot_and_are_graded_blind_by_every_grader(stores) -> None:  # type: ignore[no-untyped-def]
    _, _, root = stores
    good, poor = json.dumps(example("exam_case")), "not even json"
    agents = Agents({"good-model": good, "poor-model": poor, "dead-model": None}, {})

    def grade_from_text(labels: list[str]) -> str:
        out = {}
        for label in labels:
            text = (root / "tournaments").glob(f"*/anon/{label}.json")
            body = next(text).read_text()
            grade = "full" if "refused-validation" in body else "miss"
            out[label] = {"items": {i: {"grade": grade, "why": "q"} for i in ("1", "2", "9")},
                          "unsupported": 0, "unsupported_ids": [], "extras": []}
        return json.dumps(out)

    agents.grader_answers = {"claude": grade_from_text, "codex": grade_from_text}
    harness = _harness(root, stores, agents)
    arms = [
        TournamentArm(name="G", provider="claude", model="good-model", mode="empowered"),
        TournamentArm(name="P", provider="codex", model="poor-model", mode="scripted"),
        TournamentArm(name="X", provider="codex", model="dead-model", mode="scripted"),
    ]

    outputs = harness.run_arms("t1", "20261004", [_spec(arm) for arm in arms])
    result = harness.grade("t1", "20261004", outputs, seed=11)

    assert {(o.arm, o.heat, o.text is not None) for o in outputs} == {
        ("G", 1, True), ("G", 2, True), ("P", 1, True), ("P", 2, True), ("X", 1, False), ("X", 2, False),
    }
    assert [g.accepted for g in result.graders] == [True, True]
    assert {a.arm: a.mean for a in result.arms} == {"G": 8.0, "P": 0.0, "X": 0.0}
    assert result.ranking == (("G",), ("P", "X"))
    directory = harness.directory("t1").resolve()
    # Graders read only the anonymized outputs and the key; never the
    # sealed mapping, the arms' run dirs or the key store.
    grader_spaces = [s for p, s, _ in agents.spaces if "Improver tournament grader" in p]
    assert {s.evidence for s in grader_spaces} == {(directory / "anon", directory / "key")}
    mapping = json.loads((directory / "sealed" / "mapping.json").read_text())
    assert mapping["seed"] == 11 and {v["arm"] for v in mapping["labels"].values()} == {"G", "P"}
    assert not any(str(directory) in p.read_text() for p in (directory / "anon").glob("*.json"))
    # Arms read their frozen inputs (and the empowered one its toolbox), never the key.
    arm_spaces = [(s, t) for p, s, t in agents.spaces if "THE IMPROVER PROMPT" in p]
    assert all(not any("keys" in str(e) or "sealed" in str(e) for e in s.evidence) for s, _ in arm_spaces)
    assert any(any(e.name == "toolbox" for e in s.evidence) and t is not None for s, t in arm_spaces)
    assert json.loads((directory / "result.json").read_text())["ranking"] == [["G"], ["P", "X"]]


def _complete(labels: list[str]) -> str:
    return json.dumps({label: {"items": {i: {"grade": "half", "why": "q"} for i in ("1", "2", "9")},
                               "unsupported": 0} for label in labels})


def test_a_tournament_one_grader_could_not_grade_has_no_result(stores) -> None:  # type: ignore[no-untyped-def]
    """Cross-model means every grader: one model's judgment never ranks the arms alone."""
    _, _, root = stores

    def partial(labels: list[str]) -> str:
        return json.dumps({labels[0]: {}})

    harness = _harness(root, stores, Agents({}, {"claude": _complete, "codex": partial}))

    with pytest.raises(RuntimeError, match=r"not every grader.*codex: rejected"):
        harness.grade("t2", "20261004", [ArmOutput("A", 1, "{}"), ArmOutput("B", 1, "{}")], seed=1)
    assert not (harness.directory("t2") / "result.json").exists()
    # What each grader answered stays for a person to read.
    assert (harness.directory("t2") / "graders" / "codex" / "grades.json").is_file()


def test_arms_are_ranked_on_their_exact_means_not_the_rounded_ones(stores) -> None:  # type: ignore[no-untyped-def]
    """1.0004 and 0.4996 differ by more than the 0.5 margin; rounded to
    1.0 and 0.5, they would tie."""
    from issue_orchestrator.execution.improver_tournament import _result

    key = stores[1].get("20261004")
    per_grader = {"g": {"S10": 1.0004, "S11": 0.4996}}
    result = _result("t", "20261004", key, [], per_grader, {"S10": "A", "S11": "B"},
                     [ArmOutput("A", 1, "x"), ArmOutput("B", 1, "y")])

    assert result.ranking == (("A",), ("B",))
    assert [a.mean for a in result.arms] == [1.0, 0.5]


def test_a_challenger_and_its_champion_run_together_each_as_specified(stores) -> None:  # type: ignore[no-untyped-def]
    _, _, root = stores
    agents = Agents({"m": json.dumps(example("exam_case"))}, {})
    harness = _harness(root, stores, agents)
    arm = TournamentArm(name="champion", provider="claude", model="m", mode="empowered")

    outputs = harness.run_arms("t5", "20261004", [
        _spec(arm, prompt="CHAMPION PROMPT", heats=HeatPlan(1, 1), budget=30, timeout=40),
        _spec(arm.model_copy(update={"name": "challenger"}), prompt="CHALLENGER PROMPT", heats=HeatPlan(3, 2),
              budget=45, timeout=55),
    ])

    seen = sorted(
        ("CHALLENGER" if "CHALLENGER PROMPT" in p else "CHAMPION" if "CHAMPION PROMPT" in p else "?", space.heat,
         "budget 45 minutes" in p)
        for p, space, _ in agents.spaces
    )
    assert seen == [("CHALLENGER", 1, True), ("CHALLENGER", 2, True), ("CHALLENGER", 3, True), ("CHAMPION", 1, False)]
    assert [(o.arm, o.heat) for o in outputs] == [("champion", 1), ("challenger", 1), ("challenger", 2), ("challenger", 3)]
    assert agents.timeouts["m"] == [40, 55]
    with pytest.raises(ValueError, match="arm names repeat"):
        harness.run_arms("t6", "20261004", [_spec(arm), _spec(arm)])


def test_an_empowered_arm_is_stopped_only_after_its_budget() -> None:
    arm = TournamentArm(name="e", provider="claude", model="m", mode="empowered")
    with pytest.raises(ValueError, match="60-minute budget must be below its 60-minute agent timeout"):
        _spec(arm, budget=60, timeout=60)
    _spec(arm.model_copy(update={"mode": "scripted"}), budget=60, timeout=60)


@pytest.mark.parametrize(
    ("graders", "why"),
    [
        ([Grader("only", ImproverAgentChoice.for_provider(ImproverProvider.CLAUDE))], "at least two providers"),
        ([Grader("a", ImproverAgentChoice.for_provider(ImproverProvider.CLAUDE)),
          Grader("b", ImproverAgentChoice(provider=ImproverProvider.CLAUDE, model="sonnet"))], "at least two providers"),
        ([Grader("g", ImproverAgentChoice.for_provider(ImproverProvider.CLAUDE)),
          Grader("g", ImproverAgentChoice.for_provider(ImproverProvider.CODEX))], "grader names repeat"),
    ],
)
def test_a_tournament_is_graded_by_at_least_two_providers_under_distinct_names(stores, graders, why) -> None:  # type: ignore[no-untyped-def]
    _, _, root = stores
    harness = _harness(root, stores, Agents({}, {"claude": _complete, "codex": _complete}))

    with pytest.raises(ValueError, match=why):
        harness.grade("t7", "20261004", [ArmOutput("A", 1, "{}")], graders=graders, seed=1)
    assert not harness.directory("t7").exists()


@pytest.mark.parametrize("name", ["../../../snapshots/20261004/improver-data", "a/b", "..", ""])
def test_no_name_reaches_outside_its_directory(stores, name: str) -> None:  # type: ignore[no-untyped-def]
    """A grader, snapshot or tournament name is one path component: a name
    like ../../snapshots/x would write into a frozen snapshot."""
    snapshots, keys, root = stores
    before = sorted(str(p) for p in (root / "snapshots").rglob("*"))
    harness = _harness(root, stores, Agents({}, {}))

    with pytest.raises(ValueError, match="one path component"):
        Grader(name, ImproverAgentChoice.for_provider(ImproverProvider.CLAUDE))
    with pytest.raises(ValueError, match="one path component"):
        harness.directory(name)
    with pytest.raises(ValueError, match="one path component"):
        keys.path(name)
    with pytest.raises(ValueError, match="one path component"):
        snapshots.get(name)
    with pytest.raises(ValueError, match="one path component"):
        snapshots.import_(name, improver_data=root, taken_at=T0, origin="x")
    assert sorted(str(p) for p in (root / "snapshots").rglob("*")) == before


def test_a_tournament_never_touches_github(stores) -> None:  # type: ignore[no-untyped-def]
    from issue_orchestrator.execution.improver_tournament import _NoGitHub

    with pytest.raises(RuntimeError, match="never touches GitHub"):
        _NoGitHub().list_open_issue_labels_complete()


def test_the_graders_default_to_one_claude_and_one_codex() -> None:
    from issue_orchestrator.execution.improver_tournament import DEFAULT_GRADERS

    assert [(g.name, g.choice.provider.value) for g in DEFAULT_GRADERS] == [("claude", "claude"), ("codex", "codex")]
    assert isinstance(DEFAULT_GRADERS[0], Grader)
