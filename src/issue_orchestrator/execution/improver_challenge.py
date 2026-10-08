"""Trying a proposed change to the improver, and promoting a winner (#8001).

:class:`ImproverChallenges` owns the champion/challenger cycle:

* :meth:`challenge` takes an accepted, invited improver run's change, builds
  the challenger from the CURRENT champion, and tries both on frozen
  snapshots through the tournament harness: whole improver runs per arm
  (each its heats merged), graded blind and pooled. One tournament per
  snapshot, named for the challenge, so a challenge interrupted mid-way is
  resumed (a graded tournament is reused, an ungraded one regraded), never
  re-run at the cost of its arms again.
* :meth:`promote` replaces the champion with a challenger that won on every
  snapshot against the champion that is current now, AND whose issue a
  maintainer approved (#7906's positive ``approved``), read fresh.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from ..contracts.improver_run import EffectStatus, ImproverRunRecord, RunOutcome
from ..contracts.improver_tournament import TournamentArm, TournamentResult
from ..contracts.improver_variant import (
    ChallengeRecord,
    ChallengeRequest,
    ChampionState,
    GraderSpec,
    ImproverVariant,
    SnapshotTrial,
)
from ..domain.improver_champion import (
    CHALLENGER_ARM,
    CHAMPION_ARM,
    CHANGE_ID,
    ChallengerIssueFacts,
    ChangeNotApplicable,
    challenger_of,
    challenger_won,
    judge_challenger_issue,
    live_limits,
    outcome_of,
    promotion_refusals,
)
from ..domain.tech_lead_approval import APPROVED_LABEL, ApprovalVerdict
from ..entrypoints.improver_run import HeatPlan
from ..ports.improver_challenger import ChallengerIssueEvidence
from .improver_champion_store import ChampionUnavailable, FileChampionStore
from .improver_run_store import FileImproverRunStore
from .improver_tournament import DEFAULT_GRADERS, DEFAULT_PASSES, ArmSpec, Grader, TournamentHarness

#: Whole improver runs per arm per snapshot: the fewest the heats' exact
#: test can separate (two an arm never can).
DEFAULT_WHOLE_RUNS = 3
_ISSUE = re.compile(r"^(?P<repo>[^#\s]+/[^#\s]+)#(?P<number>\d+)$")


class ChallengeRefused(RuntimeError):
    """A run whose change cannot be tried (not invited, not accepted, its
    issue not filed, or proposed against a champion that has changed)."""


@dataclass(frozen=True)
class Promoted:
    state: ChampionState | None
    verdict: ApprovalVerdict
    refusals: tuple[str, ...]


class ImproverChallenges:
    def __init__(
        self,
        *,
        harness: TournamentHarness,
        champions: FileChampionStore,
        runs: FileImproverRunStore,
        issues_for: Callable[[str], ChallengerIssueEvidence],
        empowered_addendum: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._harness = harness
        self._champions = champions
        self._runs = runs
        self._issues_for = issues_for
        self._addendum = empowered_addendum
        self._clock = clock

    def challenge(
        self,
        run_id: str,
        snapshots: Sequence[str],
        *,
        whole_runs: int = DEFAULT_WHOLE_RUNS,
        passes: int = DEFAULT_PASSES,
        graders: Sequence[Grader] = DEFAULT_GRADERS,
        seed: int | None = None,
    ) -> ChallengeRecord:
        """Try the run's change. ``seed`` None: a retry's fixed one, else a new one."""
        if not snapshots or len(set(snapshots)) != len(snapshots):
            raise ChallengeRefused(f"a challenge is tried on one or more distinct snapshots, not {list(snapshots)}")
        run = self._invited_run(run_id)
        change = self._runs.accepted_findings(run).improver_change
        if change is None:
            raise ChallengeRefused(f"run {run_id} proposed no change")
        issue = _change_issue(run)
        champion = self._champions.state().champion
        if run.change_invitation != champion.id:
            raise ChallengeRefused(
                f"run {run_id} proposed a change to champion {run.change_invitation}; the champion is now"
                f" {champion.id}"
            )
        champion_prompt = self._champions.prompt(champion.prompt_sha256)
        try:
            challenger, challenger_prompt = challenger_of(champion, champion_prompt, change)
        except ChangeNotApplicable as error:
            raise ChallengeRefused(f"run {run_id}'s change does not apply to the champion: {error}") from error
        self._champions.store_prompt(challenger_prompt)
        challenge_id = f"{run_id}-vs-{champion.id}"
        if seed is None:
            fixed = self._champions.challenge_request(challenge_id)
            seed = fixed.seed if fixed is not None else random.SystemRandom().randrange(1 << 30)
        request = ChallengeRequest(
            challenge_id=challenge_id, run_id=run_id, issue=issue, champion=champion, challenger=challenger,
            snapshots=tuple(snapshots), whole_runs=whole_runs, passes=passes,
            graders=tuple(GraderSpec(name=g.name, provider=g.choice.provider.value, model=g.choice.model)
                          for g in graders),
            seed=seed,
        )
        try:
            self._champions.fix_challenge_request(request)
        except ChampionUnavailable as error:
            raise ChallengeRefused(str(error)) from error
        if self._champions.has_challenge(challenge_id):
            return self._champions.challenge(challenge_id)  # tried already, exactly so
        specs = [
            self._spec(CHAMPION_ARM, champion, champion_prompt, whole_runs),
            self._spec(CHALLENGER_ARM, challenger, challenger_prompt, whole_runs),
        ]
        trials = []
        for index, snapshot_id in enumerate(request.snapshots, 1):
            tournament_id = f"{challenge_id}-s{index}"
            result = self._tournament(tournament_id, snapshot_id, specs, request, graders)
            comparison = next(
                c for c in result.comparisons if {c.higher, c.lower} == {CHAMPION_ARM, CHALLENGER_ARM}
            )
            trials.append(SnapshotTrial(
                snapshot_id=snapshot_id, tournament_id=tournament_id, comparison=comparison,
                challenger_won=challenger_won(result),
            ))
        record = ChallengeRecord(
            challenge_id=challenge_id, at=self._clock(), run_id=run_id, change=change, issue=issue,
            champion=champion, challenger=challenger, trials=tuple(trials),
            outcome=outcome_of([t.challenger_won for t in trials]),  # type: ignore[arg-type]
        )
        self._champions.save_challenge(record)
        return record

    def promote(self, challenge_id: str) -> Promoted:
        challenge = self._champions.challenge(challenge_id)
        verdict = self._approval(challenge.issue)
        refusals = promotion_refusals(challenge, current=self._champions.state().champion, approval=verdict)
        if refusals:
            return Promoted(None, verdict, tuple(refusals))
        state = self._champions.promote(challenge, at=self._clock(), approved_by=verdict.actor)
        return Promoted(state, verdict, ())

    # -- parts ---------------------------------------------------------------

    def _invited_run(self, run_id: str) -> ImproverRunRecord:
        run = next((r for r in self._runs.runs() if r.run_id == run_id), None)
        if run is None:
            raise ChallengeRefused(f"no improver run {run_id!r}")
        if run.outcome is not RunOutcome.ACCEPTED or run.change_invitation is None:
            raise ChallengeRefused(f"run {run_id} is not an accepted run invited to propose a change")
        return run

    def _spec(self, name: str, variant: ImproverVariant, prompt: str, whole_runs: int) -> ArmSpec:
        return ArmSpec(
            arm=TournamentArm(
                name=name, provider=variant.agent.provider.value, model=variant.agent.model, mode=variant.mode.value,
            ),
            prompt=prompt,
            empowered_addendum=self._addendum,
            heats=HeatPlan(count=variant.heats, parallel=variant.heats),
            budget_minutes=variant.budget_minutes,
            # As it runs live.
            agent_timeout_minutes=live_limits(variant).agent_timeout_minutes,
            whole_runs=whole_runs,
        )

    def _tournament(
        self,
        tournament_id: str,
        snapshot_id: str,
        specs: Sequence[ArmSpec],
        request: ChallengeRequest,
        graders: Sequence[Grader],
    ) -> TournamentResult:
        """The snapshot's tournament: reused when graded (exactly as the
        challenge asked), regraded when its arms ran but its grading did not
        finish, run otherwise."""
        done = self._harness.result_of(tournament_id)
        if done is None and self._harness.arms_ran(tournament_id):
            done = self._harness.regrade(tournament_id, graders=graders, passes=request.passes)
        if done is None:
            outputs = self._harness.run_arms(tournament_id, snapshot_id, specs)
            done = self._harness.grade(
                tournament_id, snapshot_id, outputs, graders=graders, passes=request.passes, seed=request.seed
            )
        graded_by = {(g.name, g.provider, g.model) for g in done.graders}
        asked = {(g.name, g.provider, g.model) for g in request.graders}
        if done.snapshot_id != snapshot_id or done.passes != request.passes or graded_by != asked:
            raise ChallengeRefused(
                f"tournament {tournament_id} was graded otherwise than challenge {request.challenge_id} asks"
                f" (snapshot {done.snapshot_id}, {done.passes} pass(es), graders {sorted(graded_by)})"
            )
        return done

    def _approval(self, issue: str) -> ApprovalVerdict:
        match = _ISSUE.match(issue)
        if match is None:
            raise ChallengeRefused(f"{issue!r} names no issue")
        number, evidence = int(match["number"]), self._issues_for(match["repo"])
        found = evidence.get_issue(number)
        added = evidence.latest_label_event(number, APPROVED_LABEL)
        removed = evidence.latest_label_event(number, APPROVED_LABEL, removed=True)
        person = added is not None and not added.actor_is_bot
        return judge_challenger_issue(ChallengerIssueFacts(
            number=number,
            state=None if found is None else found.state,
            labels=frozenset(found.labels) if found is not None else frozenset(),
            approved_added=added,
            approved_removed=removed,
            approver_role=evidence.repository_role(added.actor_login) if person and added is not None else None,
            closed_since_approval=added is not None and evidence.issue_closed_on_or_after(number, added.created_at),
        ))


def _change_issue(run: ImproverRunRecord) -> str:
    receipt = next((e for e in run.effects if e.finding_id == CHANGE_ID), None)
    if receipt is None or receipt.status is EffectStatus.PENDING or receipt.issue_number is None:
        raise ChallengeRefused(
            f"run {run.run_id}'s change has no issue yet (improver apply files it): a maintainer approves it there"
        )
    return f"{run.outputs_repo}#{receipt.issue_number}"


__all__ = ["DEFAULT_WHOLE_RUNS", "ChallengeRefused", "ImproverChallenges", "Promoted"]
