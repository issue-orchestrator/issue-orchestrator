#!/usr/bin/env python3
"""The improver tournament: frozen snapshots, answer keys, arms and graders (#8001).

    improver_tournament snapshot import --id 20261004 --improver-data D --taken-at ISO \\
        [--state-dir S --clone C] --origin TEXT
    improver_tournament snapshot list
    improver_tournament key seed --snapshot ID --sealed-key KEY.md --sealed-at ISO --by NAME
    improver_tournament key add --snapshot ID --id H-7999 --weight 2 --category stall \\
        --title T --description D --link owner/repo#N --filed-at YYYY-MM-DD --by NAME [--confirmed]
    improver_tournament key confirm --snapshot ID --id H-7999 --by NAME
    improver_tournament key show --snapshot ID
    improver_tournament run --snapshot ID --arm C=claude:opus:empowered --arm A=codex:gpt-5.6-sol:scripted \\
        [--heats 3 --parallel-heats 3] [--seed N]
    improver_tournament grade-recorded --snapshot ID --recorded DIR [--seed N]

(each ``python -m issue_orchestrator.entrypoints.cli_tools.improver_tournament ...``)

Everything lives under ``<git common dir>/io-improver/``: ``snapshots/``,
``keys/`` (written only by the ``key`` commands, never by an improver run),
``tournaments/<id>/``. ``grade-recorded`` grades answers recorded by an
earlier tournament (``DIR/<arm><heat>.txt``, e.g. the 2026-10-04 one's
``results/raw``): the same anonymizer, graders and ranking, without
re-running its arms.

This is the composition root of the tournament.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from ...contracts.improver_run import ImproverAgentChoice, ImproverProvider
from ...contracts.improver_tournament import AnswerKeyItem, TournamentArm, TournamentResult
from ...execution.command_runner import LocalCommandRunner
from ...execution.improver_agents import improver_agent
from ...execution.improver_answer_keys import FileAnswerKeyStore
from ...execution.improver_investigation import EMPOWERED_ADDENDUM
from ...execution.improver_run_store import improver_root
from ...execution.improver_snapshots import FrozenSnapshotStore
from ...execution.improver_tournament import DEFAULT_GRADERS, ArmOutput, ArmSpec, Grader, TournamentHarness
from ...execution.process_group_command_runner import ProcessGroupCommandRunner
from ..improver_run import HeatPlan

PROMPT = Path("examples/prompts/tech-lead-improver.md")
GRADER_PROMPT = Path("examples/prompts/improver-grader.md")
_ARM = re.compile(r"^(?P<name>[A-Za-z0-9_-]+)=(?P<provider>claude|codex):(?P<model>[^:]+):(?P<mode>scripted|empowered)$")
_RECORDED = re.compile(r"^(?P<arm>[A-Za-z]+)(?P<heat>\d+)\.txt$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="improver_tournament", description="The improver tournament (#8001)")
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot").add_subparsers(dest="action", required=True)
    imp = snap.add_parser("import")
    imp.add_argument("--id", required=True)
    imp.add_argument("--improver-data", required=True, type=Path)
    imp.add_argument("--taken-at", required=True, type=datetime.fromisoformat)
    imp.add_argument("--state-dir", type=Path)
    imp.add_argument("--clone", type=Path)
    imp.add_argument("--origin", required=True)
    snap.add_parser("list")
    key = sub.add_parser("key").add_subparsers(dest="action", required=True)
    seed = key.add_parser("seed")
    seed.add_argument("--snapshot", required=True)
    seed.add_argument("--sealed-key", required=True, type=Path)
    seed.add_argument("--sealed-at", required=True, type=datetime.fromisoformat)
    seed.add_argument("--by", required=True)
    add = key.add_parser("add")
    add.add_argument("--snapshot", required=True)
    add.add_argument("--id", required=True)
    add.add_argument("--weight", required=True, type=int, choices=(1, 2, 3))
    add.add_argument("--category", required=True, choices=("stall", "design"))
    add.add_argument("--title", required=True)
    add.add_argument("--description", required=True)
    add.add_argument("--link", action="append", default=[])
    add.add_argument("--filed-at", type=date.fromisoformat)
    add.add_argument("--by", required=True)
    add.add_argument("--confirmed", action="store_true", help="Score it now (else a candidate)")
    confirm = key.add_parser("confirm")
    confirm.add_argument("--snapshot", required=True)
    confirm.add_argument("--id", required=True)
    confirm.add_argument("--by", required=True)
    show = key.add_parser("show")
    show.add_argument("--snapshot", required=True)
    run = sub.add_parser("run")
    run.add_argument("--snapshot", required=True)
    run.add_argument("--arm", action="append", required=True, type=_arm, help="NAME=provider:model:scripted|empowered")
    run.add_argument("--heats", type=int, default=3)
    run.add_argument("--parallel-heats", type=int, default=3)
    run.add_argument("--budget-minutes", type=int, default=60)
    run.add_argument("--agent-timeout-minutes", type=int, default=80)
    run.add_argument("--grader", action="append", type=_grader, help="NAME=provider:model (default: Claude and Codex)")
    run.add_argument("--seed", type=int)
    graded = sub.add_parser("grade-recorded")
    graded.add_argument("--snapshot", required=True)
    graded.add_argument("--recorded", required=True, type=Path, help="DIR of <arm><heat>.txt answers")
    graded.add_argument("--grader", action="append", type=_grader)
    graded.add_argument("--seed", type=int)
    graded.add_argument("--agent-timeout-minutes", type=int, default=30)
    return parser


def _arm(text: str) -> TournamentArm:
    match = _ARM.match(text)
    if match is None:
        raise argparse.ArgumentTypeError(f"an arm is NAME=claude|codex:MODEL:scripted|empowered, not {text!r}")
    return TournamentArm(**match.groupdict())  # type: ignore[arg-type]


def _grader(text: str) -> Grader:
    name, _, rest = text.partition("=")
    provider, _, model = rest.partition(":")
    if not name or provider not in ("claude", "codex") or not model:
        raise argparse.ArgumentTypeError(f"a grader is NAME=claude|codex:MODEL, not {text!r}")
    return Grader(name, ImproverAgentChoice(provider=ImproverProvider(provider), model=model))


def _now() -> datetime:
    return datetime.now(UTC)


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    root = improver_root(Path.cwd(), LocalCommandRunner())
    snapshots = FrozenSnapshotStore(root, LocalCommandRunner())
    keys = FileAnswerKeyStore(root)
    if args.command == "snapshot":
        return _snapshot(args, snapshots)
    if args.command == "key":
        return _key(args, keys)
    harness = TournamentHarness(
        root=root, snapshots=snapshots, keys=keys,
        agent_for=lambda choice: improver_agent(
            choice, runner=ProcessGroupCommandRunner(), timeout_seconds=args.agent_timeout_minutes * 60
        ),
        grader_prompt=GRADER_PROMPT.read_text(encoding="utf-8"),
        clock=_now,
    )
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1 << 30)
    tournament_id = f"{_now().strftime('%Y%m%dT%H%M%SZ')}-{args.snapshot}"
    graders = tuple(args.grader or DEFAULT_GRADERS)
    if args.command == "run":
        plan = HeatPlan(count=args.heats, parallel=args.parallel_heats)
        prompt, addendum = PROMPT.read_text(encoding="utf-8"), EMPOWERED_ADDENDUM.read_text(encoding="utf-8")
        specs = [ArmSpec(arm, prompt, addendum, plan, args.budget_minutes) for arm in args.arm]
        outputs = harness.run_arms(tournament_id, args.snapshot, specs)
    else:
        outputs = _recorded(args.recorded)
    result = harness.grade(tournament_id, args.snapshot, outputs, graders=graders, seed=seed)
    print(render_result(result, harness.directory(tournament_id)))
    return 0


def _snapshot(args: argparse.Namespace, snapshots: FrozenSnapshotStore) -> int:
    if args.action == "list":
        for snapshot_id in snapshots.ids():
            s = snapshots.get(snapshot_id)
            print(f"{s.id}: {s.audited_repo} at {s.taken_at.isoformat()} (toolbox: {s.has_toolbox}) {s.origin}")
        return 0
    s = snapshots.import_(
        args.id, improver_data=args.improver_data, taken_at=args.taken_at, origin=args.origin,
        state_dir=args.state_dir, clone=args.clone,
    )
    print(f"froze {s.id}: {s.audited_repo} engine {s.engine_commit[:10]}; upgrades: {list(s.upgrades) or 'none'}")
    return 0


def _key(args: argparse.Namespace, keys: FileAnswerKeyStore) -> int:
    if args.action == "seed":
        key = keys.seed_sealed(args.snapshot, args.sealed_key.read_text(encoding="utf-8"), sealed_at=args.sealed_at, added_by=args.by)
    elif args.action == "add":
        key = keys.add(AnswerKeyItem(
            id=args.id, weight=args.weight, title=args.title, description=args.description,
            category=args.category, source="hindsight", status="confirmed" if args.confirmed else "candidate",
            links=tuple(args.link), filed_at=args.filed_at, added_at=_now(), added_by=args.by,
        ), snapshot_id=args.snapshot)
    elif args.action == "confirm":
        key = keys.confirm(args.snapshot, args.id, by=args.by)
    else:
        key = keys.get(args.snapshot)
    for item in key.items:
        links = f" [{', '.join(item.links)}]" if item.links else ""
        print(f"{item.id} ({item.weight}, {item.category}, {item.source}, {item.status}) {item.title}{links}")
    print(f"scored: {len(key.scored)} item(s), max {key.max_score}")
    return 0


def _recorded(directory: Path) -> list[ArmOutput]:
    outputs: list[ArmOutput] = []
    for path in sorted(directory.glob("*.txt")):
        match = _RECORDED.match(path.name)
        if match is None:
            raise SystemExit(f"improver_tournament: {path.name} is not <arm><heat>.txt")
        text = path.read_text(encoding="utf-8")
        outputs.append(ArmOutput(match["arm"], int(match["heat"]), text if text.strip() else None))
    if not outputs:
        raise SystemExit(f"improver_tournament: no <arm><heat>.txt answer in {directory}")
    return outputs


def render_result(result: TournamentResult, directory: Path) -> str:
    lines = [f"tournament {result.tournament_id} on {result.snapshot_id}: key {result.key_items} item(s), max {result.max_score}"]
    lines += [f"  grader {g.name} ({g.provider}:{g.model}): {'accepted' if g.accepted else 'NOT accepted'}: {g.detail}"
              for g in result.graders]
    lines += [f"  arm {a.arm}: mean {a.mean:.2f} (range {a.low:.1f}-{a.high:.1f}) "
              + " ".join(f"{grader}={list(v)}" for grader, v in a.scores.items()) for a in result.arms]
    lines.append(f"  ranking: {result.ranking_text()} (ties within {result.tie_margin})")
    lines.append(f"  in {directory}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
