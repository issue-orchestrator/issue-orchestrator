"""The tournament's command line: arms, graders and recorded answers (#8001)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.cli_tools import improver_tournament as cli


def test_an_arm_and_a_grader_are_named_by_provider_model_and_mode() -> None:
    args = cli.build_parser().parse_args([
        "run", "--snapshot", "20261004", "--arm", "C=claude:opus:empowered", "--arm", "A=codex:gpt-5.6-sol:scripted",
        "--grader", "g2=codex:gpt-5.6-sol",
    ])

    assert [(r.arm.name, r.arm.provider, r.arm.model, r.arm.mode) for r in args.arm] == [
        ("C", "claude", "opus", "empowered"), ("A", "codex", "gpt-5.6-sol", "scripted"),
    ]
    assert args.grader[0].name == "g2" and args.grader[0].choice.describe() == "codex:gpt-5.6-sol"
    # Modest defaults: Claude heats and gradings count against the operator's subscription.
    assert (args.heats, args.parallel_heats, args.passes) == (2, 2, 3)


@pytest.mark.parametrize("arm", [
    "C=claude:opus", "C=gemini:x:scripted", "=claude:opus:scripted", "C=claude:opus:wild",
    "C=claude:opus:empowered,colour=red", "C=claude:opus:empowered,heats=two", "C=claude:opus:empowered,heats=2,heats=3",
    "C=claude:opus:empowered,budget=",
])
def test_a_malformed_arm_is_refused(arm: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_arm(arm)


def test_recorded_answers_are_read_as_arm_and_heat(tmp_path: Path) -> None:
    for name, text in (("A1.txt", "{}"), ("C3.txt", '{"findings": []}'), ("D2.txt", "  ")):
        (tmp_path / name).write_text(text)

    outputs = cli.read_recorded(tmp_path)

    assert [(o.arm, o.heat, o.text) for o in outputs] == [("A", 1, "{}"), ("C", 3, '{"findings": []}'), ("D", 2, None)]


def test_a_recorded_file_that_names_no_arm_and_heat_is_refused(tmp_path: Path) -> None:
    (tmp_path / "MAPPING.txt").write_text("x")
    with pytest.raises(SystemExit, match="not <arm><heat>.txt"):
        cli.read_recorded(tmp_path)


def test_a_challenger_arm_sets_its_own_prompt_heats_and_minutes(tmp_path: Path) -> None:
    challenger = tmp_path / "challenger.md"
    challenger.write_text("CHALLENGER")
    champion = tmp_path / "champion.md"
    champion.write_text("CHAMPION")
    args = cli.build_parser().parse_args([
        "run", "--snapshot", "s", "--arm", "A=claude:opus:empowered",
        "--arm", f"B=claude:opus:empowered,prompt={challenger},heats=2,parallel=1,budget=45,timeout=55",
    ])

    a, b = (request.spec(args, prompt=champion, addendum="ADD") for request in args.arm)

    assert (a.prompt, a.heats.count, a.heats.parallel, a.budget_minutes, a.agent_timeout_minutes) == ("CHAMPION", 2, 2, 60, 80)
    assert (b.prompt, b.heats.count, b.heats.parallel, b.budget_minutes, b.agent_timeout_minutes) == ("CHALLENGER", 2, 1, 45, 55)
    over = cli.build_parser().parse_args(["run", "--snapshot", "s", "--arm", "C=claude:opus:empowered,budget=90"])
    with pytest.raises(ValueError, match="budget must be below"):
        over.arm[0].spec(over, prompt=champion, addendum="ADD")


@pytest.mark.parametrize("grader", ["../../snapshots/x=claude:opus", "a/b=codex:m"])
def test_a_grader_name_is_one_path_component(grader: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="one path component"):
        cli.parse_grader(grader)


def test_a_failed_grading_is_regraded_without_running_the_arms_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The arms are costly; a grader that failed is retried on the same
    tournament (same outputs, seed, anonymized files), not by a new run."""
    import json
    from datetime import UTC, datetime

    from issue_orchestrator.execution.improver_answer_keys import FileAnswerKeyStore
    from issue_orchestrator.ports.improver import ImproverAgentResult
    from tests.unit.domain.test_improver_tournament import SEALED
    from tests.unit.execution.test_improver_tournament_harness import _legacy_inputs

    store = tmp_path / "io-improver"
    monkeypatch.setattr(cli, "improver_root", lambda checkout, runner: store)
    monkeypatch.chdir(Path(__file__).resolve().parents[3])
    assert cli.main(["snapshot", "import", "--id", "s1", "--improver-data", str(_legacy_inputs(tmp_path / "d")),
                     "--taken-at", "2026-10-04T07:36:00+00:00", "--origin", "test"]) == 0
    FileAnswerKeyStore(store).seed_sealed("s1", SEALED, sealed_at=datetime(2026, 10, 4, tzinfo=UTC), added_by="coordinator")
    calls: list[str] = []
    codex_complete = {"now": False}

    class Agent:
        def __init__(self, choice):  # type: ignore[no-untyped-def]
            self.choice = choice

        def run(self, *, prompt, space, toolbox):  # type: ignore[no-untyped-def]
            labels = sorted(p.stem for p in (space.run_dir / "anon").glob("*.json"))
            calls.append(self.choice.provider.value)
            if self.choice.provider.value == "codex" and not codex_complete["now"]:
                return ImproverAgentResult("{}", "partial")
            return ImproverAgentResult(json.dumps({label: {
                "items": {i: {"grade": "half", "why": "q"} for i in ("1", "2", "9")}, "unsupported": 0,
            } for label in labels}), "graded")

    monkeypatch.setattr(cli, "improver_agent", lambda choice, runner, timeout_seconds: Agent(choice))
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    (recorded / "A1.txt").write_text("{}")
    (recorded / "B1.txt").write_text('{"b": 1}')

    with pytest.raises(SystemExit, match=r"regrade --tournament (\S+)") as failed:
        cli.main(["grade-recorded", "--snapshot", "s1", "--recorded", str(recorded), "--seed", "9"])
    tournament = str(failed.value).rsplit("--tournament ", 1)[1].strip()
    anon = {p.name: p.read_text() for p in (store / "tournaments" / tournament / "anon").iterdir()}
    codex_complete["now"] = True

    assert cli.main(["regrade", "--tournament", tournament]) == 0

    directory = store / "tournaments" / tournament
    assert sorted(calls) == ["claude"] * 6 + ["codex"] * 6  # 3 passes each, twice; the arms never ran
    assert {p.name: p.read_text() for p in (directory / "anon").iterdir()} == anon
    assert (directory / "graders-attempt-1" / "codex" / "p1" / "grades.json").read_text() == "{}"
    result = json.loads((directory / "result.json").read_text())
    assert len(result["graders"]) == 6 and all(g["accepted"] for g in result["graders"])
    # Both attempts' calls: the failed one cost as much as the one that counted.
    assert result["cost"] == {"arm_heats": {}, "grader_calls": {"claude": 6, "codex": 6},
                              "grader_seconds": result["cost"]["grader_seconds"]}


def test_two_tournaments_started_in_one_second_get_their_own_ids() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
    ids = {cli.new_tournament_id("20261004", now) for _ in range(20)}

    assert len(ids) == 20 and all(i.startswith("20261007T200000Z-20261004-") for i in ids)
