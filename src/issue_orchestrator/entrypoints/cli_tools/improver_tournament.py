#!/usr/bin/env python3
"""The improver tournament: frozen snapshots, answer keys, arms and graders (#8001).

    improver_tournament snapshot import --id 20261004 --improver-data D --taken-at ISO \\
        [--state-dir S --clone C] --origin TEXT
    improver_tournament snapshot list
    improver_tournament key seed --snapshot ID --sealed-key KEY.md --sealed-at ISO --by NAME
    improver_tournament key add --snapshot ID --id H-7999 --weight 2 --category stall \\
        --title T --description D --link owner/repo#N --filed-at YYYY-MM-DD \\
        --observable-since ISO --observable-source TEXT --by NAME [--confirmed]
    improver_tournament key confirm --snapshot ID --id H-7999 --by NAME
    improver_tournament key observe --snapshot ID --id H-7999 --since ISO --source TEXT --by NAME
    improver_tournament key move --snapshot ID --id H-8137 --to LATER-ID --by NAME
    improver_tournament key show --snapshot ID
    improver_tournament run --snapshot ID --arm C=claude:opus:empowered --arm A=codex:gpt-5.6-sol:scripted \\
        [--heats 3 --parallel-heats 3 --budget-minutes 60 --agent-timeout-minutes 80] [--passes 3] [--seed N]
    improver_tournament grade-recorded --snapshot ID --recorded DIR [--seed N]
    improver_tournament regrade --tournament ID    # a tournament whose grading failed

An arm may set its own prompt, heats and minutes after its mode:
``--arm 'X=claude:opus:empowered,prompt=challenger.md,heats=2,parallel=1,budget=45,timeout=70'``
(the rest default to the flags): a challenger beside its champion.

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
import dataclasses
import random
import re
import secrets
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from ...contracts.improver_run import (
    DEFAULT_IMPROVER_AGENT,
    ImproverAgentChoice,
    ImproverProvider,
)
from ...contracts.improver_toolbox import DEFAULT_IMPROVER_MODE, ImproverMode
from ...contracts.improver_tournament import (
    AnswerKey,
    AnswerKeyItem,
    Observation,
    TournamentArm,
    TournamentResult,
)
from ...contracts.improver_variant import ImproverVariant
from ...domain.improver_champion import prompt_digest
from ...execution.command_runner import LocalCommandRunner
from ...execution.improver_agents import improver_agent
from ...execution.improver_answer_keys import AnswerKeyError, FileAnswerKeyStore
from ...execution.improver_challenge import (
    DEFAULT_WHOLE_RUNS,
    ChallengeRefused,
    ImproverChallenges,
)
from ...execution.improver_champion_store import FileChampionStore
from ...execution.improver_investigation import EMPOWERED_ADDENDUM
from ...execution.improver_run_store import FileImproverRunStore, improver_root
from ...execution.improver_snapshots import FrozenSnapshotStore, SnapshotUnavailable
from ...execution.improver_tournament import (
    DEFAULT_GRADERS,
    DEFAULT_PASSES,
    ArmOutput,
    ArmSpec,
    Grader,
    TournamentHarness,
    require_cross_model,
)
from ...execution.process_group_command_runner import ProcessGroupCommandRunner
from ...execution.providers import create_repository_host
from ...ports.improver_challenger import ChallengerIssueEvidence
from ..improver_run import HeatPlan

PROMPT = Path("examples/prompts/tech-lead-improver.md")
GRADER_PROMPT = Path("examples/prompts/improver-grader.md")
_ARM = re.compile(
    r"^(?P<name>[A-Za-z0-9_-]+)=(?P<provider>claude|codex):(?P<model>[^:,]+):(?P<mode>scripted|empowered)"
    r"(?:,(?P<options>.+))?$"
)
_ARM_OPTIONS = {"prompt": Path, "heats": int, "parallel": int, "budget": int, "timeout": int}
_RECORDED = re.compile(r"^(?P<arm>[A-Za-z]+)(?P<heat>\d+)\.txt$")


@dataclasses.dataclass(frozen=True)
class ArmRequest:
    """An arm as asked on the command line: what it runs, and any setting of
    its own (the rest are the command's flags)."""

    arm: TournamentArm
    prompt: Path | None = None
    heats: int | None = None
    parallel: int | None = None
    budget: int | None = None
    timeout: int | None = None

    def spec(self, args: argparse.Namespace, *, prompt: Path, addendum: str) -> ArmSpec:
        heats = self.heats if self.heats is not None else args.heats
        parallel = self.parallel if self.parallel is not None else min(args.parallel_heats, heats)
        return ArmSpec(
            arm=self.arm,
            prompt=(self.prompt or prompt).read_text(encoding="utf-8"),
            empowered_addendum=addendum,
            heats=HeatPlan(count=heats, parallel=parallel),
            budget_minutes=self.budget if self.budget is not None else args.budget_minutes,
            agent_timeout_minutes=self.timeout if self.timeout is not None else args.agent_timeout_minutes,
        )


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
    add.add_argument("--observable-since", required=True, type=_aware,
                     help="When its evidence first existed (ISO, with a zone): it scores only on snapshots frozen since")
    add.add_argument("--observable-source", required=True,
                     help="Where that time is read: an event, a log line, a PR's or issue's created_at")
    add.add_argument("--by", required=True)
    add.add_argument("--confirmed", action="store_true", help="Score it now (else a candidate)")
    confirm = key.add_parser("confirm")
    confirm.add_argument("--snapshot", required=True)
    confirm.add_argument("--id", required=True)
    confirm.add_argument("--by", required=True)
    observe = key.add_parser("observe", help="Record when a hindsight item's evidence first existed")
    observe.add_argument("--snapshot", required=True)
    observe.add_argument("--id", required=True)
    observe.add_argument("--since", required=True, type=_aware)
    observe.add_argument("--source", required=True)
    observe.add_argument("--by", required=True)
    move = key.add_parser("move", help="Attach a hindsight item to a snapshot frozen once it was observable")
    move.add_argument("--snapshot", required=True)
    move.add_argument("--id", required=True)
    move.add_argument("--to", required=True)
    move.add_argument("--by", required=True)
    show = key.add_parser("show")
    show.add_argument("--snapshot", required=True)
    run = sub.add_parser("run")
    run.add_argument("--snapshot", required=True)
    run.add_argument(
        "--arm", action="append", required=True, type=parse_arm,
        help="NAME=provider:model:scripted|empowered[,prompt=P,heats=N,parallel=N,budget=MIN,timeout=MIN]",
    )
    # Three heats: the fewest with which an arm can be told apart (two heats
    # an arm can never pass the heats' exact test: its smallest p is 1/6).
    run.add_argument("--heats", type=int, default=3, help="Heats per arm (default: %(default)s; fewer can never separate arms)")
    run.add_argument("--parallel-heats", type=int, default=3)
    run.add_argument("--budget-minutes", type=int, default=60)
    run.add_argument("--agent-timeout-minutes", type=int, default=80)
    run.add_argument("--grader", action="append", type=parse_grader, help="NAME=provider:model (default: Claude and Codex)")
    run.add_argument("--grader-timeout-minutes", type=int, default=40)
    run.add_argument("--passes", type=int, default=DEFAULT_PASSES, help="Gradings per grader, pooled (default: %(default)s)")
    run.add_argument("--seed", type=int)
    graded = sub.add_parser("grade-recorded")
    graded.add_argument("--snapshot", required=True)
    graded.add_argument("--recorded", required=True, type=Path, help="DIR of <arm><heat>.txt answers")
    graded.add_argument("--grader", action="append", type=parse_grader)
    graded.add_argument("--seed", type=int)
    graded.add_argument("--grader-timeout-minutes", type=int, default=40)
    graded.add_argument("--passes", type=int, default=DEFAULT_PASSES, help="Gradings per grader, pooled (default: %(default)s)")
    regrade = sub.add_parser("regrade", help="Grade a tournament whose grading failed again (same outputs and seed)")
    regrade.add_argument("--tournament", required=True)
    regrade.add_argument("--grader", action="append", type=parse_grader)
    regrade.add_argument("--grader-timeout-minutes", type=int, default=40)
    regrade.add_argument("--passes", type=int, default=DEFAULT_PASSES)
    _champion_commands(sub)
    return parser


def _champion_commands(sub: argparse._SubParsersAction) -> None:
    champion = sub.add_parser("champion", help="The improver configuration that runs (#8001)").add_subparsers(
        dest="action", required=True
    )
    seed = champion.add_parser("seed", help="The first champion: today's improver defaults unless set")
    seed.add_argument("--by", required=True)
    seed.add_argument("--prompt", type=Path, default=PROMPT)
    seed.add_argument("--provider", choices=("claude", "codex"), default=DEFAULT_IMPROVER_AGENT.provider.value)
    seed.add_argument("--model")
    seed.add_argument("--mode", choices=("scripted", "empowered"), default=DEFAULT_IMPROVER_MODE.value)
    seed.add_argument("--heats", type=int, default=2)
    seed.add_argument("--budget-minutes", type=int, default=60)
    champion.add_parser("show")
    challenge = sub.add_parser("challenge", help="Try an invited run's change against the champion")
    challenge.add_argument("--run", required=True, help="The improver run that proposed the change")
    challenge.add_argument("--snapshot", action="append", required=True, help="A frozen snapshot (repeatable)")
    challenge.add_argument("--whole-runs", type=int, default=DEFAULT_WHOLE_RUNS,
                           help="Whole improver runs per arm per snapshot (default: %(default)s)")
    challenge.add_argument("--passes", type=int, default=DEFAULT_PASSES)
    challenge.add_argument("--grader", action="append", type=parse_grader)
    challenge.add_argument("--grader-timeout-minutes", type=int, default=40)
    challenge.add_argument("--seed", type=int)
    promote = sub.add_parser("promote", help="Make a winning, maintainer-approved challenger the champion")
    promote.add_argument("--challenge", required=True)


def _aware(text: str) -> datetime:
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise argparse.ArgumentTypeError(f"a time with its zone (e.g. 2026-10-04T09:02:00+00:00), not {text!r}")
    return when


def parse_arm(text: str) -> ArmRequest:
    match = _ARM.match(text)
    if match is None:
        raise argparse.ArgumentTypeError(f"an arm is NAME=claude|codex:MODEL:scripted|empowered[,k=v...], not {text!r}")
    options: dict[str, object] = {}
    for option in (match["options"] or "").split(",") if match["options"] else ():
        name, _, value = option.partition("=")
        if name not in _ARM_OPTIONS or not value or name in options:
            raise argparse.ArgumentTypeError(f"an arm's option is one of {sorted(_ARM_OPTIONS)} once, =value; not {option!r}")
        try:
            options[name] = _ARM_OPTIONS[name](value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"{name}={value!r}: {error}") from error
    fields = {k: match[k] for k in ("name", "provider", "model", "mode")}
    return ArmRequest(TournamentArm.model_validate(fields), **options)  # type: ignore[arg-type]


def parse_grader(text: str) -> Grader:
    name, _, rest = text.partition("=")
    provider, _, model = rest.partition(":")
    if not name or provider not in ("claude", "codex") or not model:
        raise argparse.ArgumentTypeError(f"a grader is NAME=claude|codex:MODEL, not {text!r}")
    try:
        return Grader(name, ImproverAgentChoice(provider=ImproverProvider(provider), model=model))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def new_tournament_id(snapshot_id: str, now: datetime) -> str:
    """When and on what, and unique even for two commands started in the same second."""
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{snapshot_id}-{secrets.token_hex(3)}"


def _now() -> datetime:
    return datetime.now(UTC)


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    root = improver_root(Path.cwd(), LocalCommandRunner())
    snapshots = FrozenSnapshotStore(root, LocalCommandRunner())
    keys = FileAnswerKeyStore(root, snapshots)
    if args.command == "snapshot":
        return _snapshot(args, snapshots)
    if args.command == "key":
        return _key(args, keys)
    if args.command == "champion":
        return _champion(args, FileChampionStore(root))
    harness = TournamentHarness(
        root=root, snapshots=snapshots, keys=keys,
        agent_for=lambda choice, minutes: improver_agent(
            choice, runner=ProcessGroupCommandRunner(), timeout_seconds=minutes * 60
        ),
        grader_prompt=GRADER_PROMPT.read_text(encoding="utf-8"),
        clock=_now,
    )
    challenges = ImproverChallenges(
        harness=harness, champions=FileChampionStore(root), runs=FileImproverRunStore(root),
        issues_for=lambda repo: cast("ChallengerIssueEvidence", create_repository_host(repo)),
        empowered_addendum=EMPOWERED_ADDENDUM.read_text(encoding="utf-8"), clock=_now,
    )
    if args.command == "promote":
        return _promote(challenges, args.challenge)
    graders = tuple(
        dataclasses.replace(g, timeout_minutes=args.grader_timeout_minutes) for g in (args.grader or DEFAULT_GRADERS)
    )
    require_cross_model(graders)
    if args.command == "challenge":
        return _challenge(challenges, args, graders)
    if args.command == "regrade":
        tournament_id = args.tournament
        result = _graded(lambda: harness.regrade(tournament_id, graders=graders, passes=args.passes), tournament_id)
        print(render_result(result, harness.directory(tournament_id)))
        return 0
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1 << 30)
    tournament_id = new_tournament_id(args.snapshot, _now())
    if args.command == "run":
        addendum = EMPOWERED_ADDENDUM.read_text(encoding="utf-8")
        # Every arm's settings are checked before any arm runs.
        specs = [request.spec(args, prompt=PROMPT, addendum=addendum) for request in args.arm]
        outputs = harness.run_arms(tournament_id, args.snapshot, specs)
    else:
        outputs = read_recorded(args.recorded)
    result = _graded(
        lambda: harness.grade(tournament_id, args.snapshot, outputs, graders=graders, passes=args.passes, seed=seed), tournament_id
    )
    print(render_result(result, harness.directory(tournament_id)))
    return 0


def _champion(args: argparse.Namespace, champions: FileChampionStore) -> int:
    if args.action == "show":
        state = champions.state()
        print(f"champion {state.champion.id}: {state.champion.describe()}"
              f" (seeded {state.seeded_at.isoformat()} by {state.seeded_by})")
        for p in state.promotions:
            print(f"  {p.at.isoformat()}: {p.previous.id} -> {p.champion.id} by challenge {p.challenge_id}"
                  f" ({p.issue}, approved by @{p.approved_by})")
        return 0
    prompt = args.prompt.read_text(encoding="utf-8")
    variant = ImproverVariant(
        agent=ImproverAgentChoice.for_provider(ImproverProvider(args.provider), args.model),
        mode=ImproverMode(args.mode), heats=args.heats, budget_minutes=args.budget_minutes,
        prompt_sha256=prompt_digest(prompt),
    )
    state = champions.seed(variant, prompt, at=_now(), by=args.by)
    print(f"seeded champion {state.champion.id}: {state.champion.describe()}")
    return 0


def _challenge(challenges: ImproverChallenges, args: argparse.Namespace, graders: tuple[Grader, ...]) -> int:
    try:
        # No --seed: a retry reuses the challenge's own; a new one draws one.
        record = challenges.challenge(
            args.run, args.snapshot, whole_runs=args.whole_runs, passes=args.passes, graders=graders, seed=args.seed
        )
    except ChallengeRefused as refused:
        raise SystemExit(f"improver_tournament challenge: {refused}") from refused
    print(f"challenge {record.challenge_id}: {record.outcome}"
          f" ({record.challenger.describe()} against champion {record.champion.id})")
    for t in record.trials:
        c = t.comparison
        print(f"  {t.snapshot_id} ({t.tournament_id}): {c.higher} above {c.lower} by {c.gap:.2f}, band {c.band:.2f},"
              f" heat p {c.heat_p:.3f}: {'challenger won' if t.challenger_won else 'not a win'}")
    print(f"  approval: a maintainer labels {record.issue} `approved`; then promote --challenge {record.challenge_id}")
    return 0 if record.outcome == "won" else 1


def _promote(challenges: ImproverChallenges, challenge_id: str) -> int:
    promoted = challenges.promote(challenge_id)
    if promoted.state is None:
        print(f"challenge {challenge_id} not promoted ({promoted.verdict.describe()}):")
        for refusal in promoted.refusals:
            print(f"  - {refusal}")
        return 1
    print(f"promoted: champion {promoted.state.champion.id} ({promoted.state.champion.describe()}),"
          f" {promoted.verdict.describe()}")
    return 0


def _graded(grade: Callable[[], TournamentResult], tournament_id: str) -> TournamentResult:
    """The result, or (a grader failed) how to grade the same outputs again."""
    try:
        return grade()
    except RuntimeError as failed:
        raise SystemExit(
            f"improver_tournament: {failed}\n  retry the grading (the arms do not run again):"
            f" improver_tournament regrade --tournament {tournament_id}"
        ) from failed


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
    try:
        key = _key_write(args, keys)
        unseen = keys.unobservable(key.snapshot_id)
    except (AnswerKeyError, SnapshotUnavailable) as refused:
        raise SystemExit(f"improver_tournament key {args.action}: {refused}") from refused
    if args.action == "add" and args.id in unseen:
        print(f"warning: {unseen[args.id]}; it stays a candidate here (key move attaches it to a later snapshot)",
              file=sys.stderr)
    print(render_key(key, unseen))
    return 0


def _key_write(args: argparse.Namespace, keys: FileAnswerKeyStore) -> AnswerKey:
    if args.action == "seed":
        return keys.seed_sealed(args.snapshot, args.sealed_key.read_text(encoding="utf-8"), sealed_at=args.sealed_at, added_by=args.by)
    if args.action == "add":
        return keys.add(AnswerKeyItem(
            id=args.id, weight=args.weight, title=args.title, description=args.description,
            category=args.category, source="hindsight", status="confirmed" if args.confirmed else "candidate",
            links=tuple(args.link), filed_at=args.filed_at, added_at=_now(), added_by=args.by,
            observable_since=Observation(at=args.observable_since, source=args.observable_source),
        ), snapshot_id=args.snapshot)
    if args.action == "confirm":
        return keys.confirm(args.snapshot, args.id, by=args.by)
    if args.action == "observe":
        return keys.observe(args.snapshot, args.id, Observation(at=args.since, source=args.source), by=args.by)
    if args.action == "move":
        return keys.move(args.snapshot, args.id, to=args.to, by=args.by)
    return keys.get(args.snapshot)


def render_key(key: AnswerKey, unseen: dict[str, str]) -> str:
    """Each item, with when it was observable: an item the snapshot could not show says why."""
    lines = [f"key of snapshot {key.snapshot_id}:"]
    for item in key.items:
        links = f" [{', '.join(item.links)}]" if item.links else ""
        seen = item.observable_since
        observable = "sealed with the snapshot" if item.source == "sealed_key" else (
            "not recorded" if seen is None else f"{seen.at.isoformat()} ({seen.source})"
        )
        line = f"{item.id} ({item.weight}, {item.category}, {item.source}, {item.status}) {item.title}{links}"
        lines.append(f"{line}; observable since: {observable}")
        if item.id in unseen:
            lines.append(f"  NOT observable at the snapshot's time: {unseen[item.id]}")
    lines.append(f"scored: {len(key.scored)} item(s), max {key.max_score}")
    return "\n".join(lines)


def read_recorded(directory: Path) -> list[ArmOutput]:
    outputs: list[ArmOutput] = []
    for path in sorted(directory.glob("*.txt")):
        match = _RECORDED.match(path.name)
        if match is None:
            raise SystemExit(f"improver_tournament: {path.name} is not <arm><heat>.txt")
        text = path.read_text(encoding="utf-8")
        outputs.append(ArmOutput(
            match["arm"], int(match["heat"]), text if text.strip() else None, hide=(str(directory.resolve()),)
        ))
    if not outputs:
        raise SystemExit(f"improver_tournament: no <arm><heat>.txt answer in {directory}")
    return outputs


def render_result(result: TournamentResult, directory: Path) -> str:
    lines = [f"tournament {result.tournament_id} on {result.snapshot_id}: key {result.key_items} item(s), max {result.max_score}"]
    lines += [f"  grading {g.grading} ({g.provider}:{g.model}, {g.seconds:.0f}s): "
              f"{'accepted' if g.accepted else 'NOT accepted'}: {g.detail}" for g in result.graders]
    n = result.noise

    def var(v: float | None) -> str:
        return "unmeasured" if v is None else f"{v:.3f}"

    lines.append(f"  {len(result.graders)} grading(s) ({result.passes} pass(es) per grader); noise (variances):"
                 f" heat {var(n.heat)}, grader {var(n.grader)}, pass {var(n.pass_)}; resolution {n.resolution:g}")
    lines += [f"  arm {a.arm}: mean {a.mean:.2f} ± {a.se:.2f} se (outputs {list(a.output_means)}) "
              + " ".join(f"{grading}={list(v)}" for grading, v in a.scores.items()) for a in result.arms]
    lines.append(f"  ranking: {result.ranking_text()}")
    lines.append(f"  (told apart: gap > {result.band_ses:g} standard errors of the difference AND the heats'"
                 f" permutation p <= {result.heat_alpha:g})")
    lines += [f"    {c.higher} vs {c.lower}: gap {c.gap:.2f}, band {c.band:.2f}, heat p {c.heat_p:.3f}"
              f" -> {'told apart' if c.distinguishable else 'indistinguishable'}" for c in result.comparisons]
    cost = result.cost
    lines.append(f"  cost: arm heats {cost.arm_heats or 'none (recorded)'}; grader calls {cost.grader_calls};"
                 f" grader seconds {cost.grader_seconds} (Claude counts against the operator's subscription)")
    lines.append(f"  in {directory}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
