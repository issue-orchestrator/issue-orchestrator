"""The CI-failure triage owner (#8692): read a failed check, then answer it.

When a reviewer-approved PR's check fails, the awaiting-merge reconciler (or
the merge queue coordinator) discovers a post-publish rework flagged
``failed_check``. Agents cannot read Actions logs or re-run jobs, so before
that rework is queued this owner, with the engine's own credential:

1. reads the head commit's failed checks (one GraphQL call) and, for the
   failed required ones (every failed one when none is required), the tail of
   each job's log - once per job, bounded in size;
2. classifies every job through :func:`~..domain.ci_failure.classify_job_log`;
3. when every failure is transient and this head has never been re-run,
   replaces the rework with a :class:`DiscoveredCiRerun` - which spends no
   rework cycle - and otherwise keeps the rework, with the log excerpts and
   the classification appended to its brief.

The once-per-head bound is durable: io's PR record is written before its one
request, so io never asks twice for a head, and GitHub's own run attempts
say whether a head was re-run (by io or a person). A second transient
failure on the same head - after a restart too - goes to rework like any
other failure; a request GitHub never acted on goes to a person. Nothing
here loops.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..domain.ci_failure import (
    CI_RERUN_MARKER_STEM,
    CiFailureKind,
    CiFailureSignatures,
    CiJobAssessment,
    CiRerunRecord,
    classify_job_log,
    log_excerpt,
    normalize_log,
    overall_kind,
    escalated_heads,
    parse_rerun_records,
    rerun_escalated_marker,
    rerun_marker,
)
from ..domain.models import DiscoveredAwaitingMergeEscalation, DiscoveredCiRerun, DiscoveredRework
from ..ports.repository_host import RepositoryHostError

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config_models import CiFailureTriageConfig
    from ..ports.pull_request_tracker import FailedCheck
    from ..ports.repository_host import RepositoryHost
    from .action_results import ActionResult
    from .actions import RerunFailedChecksAction

logger = logging.getLogger(__name__)

#: New job logs read per scan; the memo carries a larger set across scans.
MAX_LOG_READS_PER_SCAN = 4
#: Failed jobs considered per head. Past this a failure is plainly not one
#: flaky job, and the rest are listed unread (unknown, so never re-run).
MAX_JOBS_PER_HEAD = 12
#: Scans a failed triage read (checks, a log, the re-run record) is retried
#: before the rework goes ahead with what could be read.
MAX_READ_DEFERRALS = 3
#: Job logs a rework brief carries in full; the rest are listed by name.
MAX_EXCERPTS_IN_BRIEF = 4
_PENDING = "pending"
#: Assessments remembered across ticks, so one job's log is read once.
ASSESSMENT_MEMO_LIMIT = 256
#: After requesting a re-run, the same failed jobs are still visible until
#: GitHub restarts them. Within this window that is "re-run starting", after
#: it the re-run is taken as spent (it never started).
RERUN_START_GRACE = timedelta(minutes=15)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CiTriageResult:
    """What the triage made of one tick's discovered reworks."""

    reworks: tuple[DiscoveredRework, ...]
    reruns: tuple[DiscoveredCiRerun, ...]
    escalations: tuple[DiscoveredAwaitingMergeEscalation, ...]


@dataclass
class CiFailureTriage:
    """Owns reading, classifying and answering a failed required check."""

    host: "RepositoryHost"
    config: "CiFailureTriageConfig"
    clock: Callable[[], datetime] = _utc_now

    def screen(
        self, state: "OrchestratorState", reworks: Sequence[DiscoveredRework]
    ) -> CiTriageResult:
        """Triage each failed-check rework; every other rework passes through."""
        kept: list[DiscoveredRework] = []
        reruns: list[DiscoveredCiRerun] = []
        escalations: list[DiscoveredAwaitingMergeEscalation] = []
        for rework in reworks:
            answer = self._triage(state, rework) if rework.failed_check else rework
            if isinstance(answer, DiscoveredCiRerun):
                reruns.append(answer)
            elif isinstance(answer, DiscoveredAwaitingMergeEscalation):
                escalations.append(answer)
            elif answer is not None:
                kept.append(answer)
        return CiTriageResult(reworks=tuple(kept), reruns=tuple(reruns), escalations=tuple(escalations))

    # ------------------------------------------------------------------ #

    def _triage(
        self, state: "OrchestratorState", rework: DiscoveredRework
    ) -> DiscoveredRework | DiscoveredCiRerun | DiscoveredAwaitingMergeEscalation | None:
        """The rework (with its brief extended), a re-run, an escalation, or ``None`` (not yet)."""
        if not self.config.enabled:
            return _with_report(rework, "CI failure triage is disabled in this repository's config; no job log was read.")
        try:
            failed = self.host.read_failed_checks(rework.pr_number)
        except RepositoryHostError as error:
            logger.warning("CI triage: failed checks of PR #%d unreadable: %s", rework.pr_number, error)
            return self._defer(state, rework, f"CI failure triage could not read the PR's failed checks: {error}")
        targets = [c for c in failed.checks if c.required] or list(failed.checks)
        if not targets:
            # A new commit (or a restarted check) since the rollup was read: the
            # next reconciliation classifies the head as it now is.
            return self._defer(state, rework, (
                f"CI failure triage found no failed check on head {failed.head_sha[:12]}; "
                "check the PR's checks before changing code."
            ))
        assessments, unread = self._assess_all(state, targets)
        kind = overall_kind(assessments)
        if unread and kind is not CiFailureKind.GENUINE:
            # Only a genuine failure decides before every log is read.
            if all(why == _PENDING for why in unread.values()):
                return None  # this scan's read budget ran out; the next continues
            return self._defer(state, rework, _report(
                assessments, kind, failed.head_sha, (),
                note="Not every failed job's log could be read: " + "; ".join(unread.values()),
            ))
        if kind is CiFailureKind.TRANSIENT:
            return self._answer_transient(state, rework, failed.head_sha, assessments)
        not_read = ", ".join(f"`{name}`" for name in unread)
        return self._decided(state, rework, _report(
            assessments, kind, failed.head_sha, (),
            note=f"Logs not read (a genuine failure already decides this): {not_read}" if unread else None,
        ))

    def _answer_transient(
        self,
        state: "OrchestratorState",
        rework: DiscoveredRework,
        head_sha: str,
        assessments: Sequence[CiJobAssessment],
    ) -> DiscoveredRework | DiscoveredCiRerun | DiscoveredAwaitingMergeEscalation | None:
        """Re-run a head's transient failure at most once.

        GitHub's runs are the truth for what already happened: per failed run,
        its current attempt and that attempt's jobs. io's PR record (written
        before its one request) is the truth for what io asked. So:

        * a failed job that belongs to a run's re-run attempt: spent -> rework;
        * a run re-run since, whose new attempt has not failed yet: wait;
        * io already asked for this head and GitHub shows no re-run: never ask
          twice - wait out the start grace, then hand it to a person;
        * otherwise ask, once, for the runs not yet re-run.
        """
        runs = sorted({a.run_id for a in assessments if a.run_id is not None})
        try:
            bodies = self.host.issue_comment_bodies_containing(rework.pr_number, CI_RERUN_MARKER_STEM)
            records = tuple(r for r in parse_rerun_records(bodies) if r.head_sha == head_sha)
            attempts = {run: self.host.read_check_run_latest_attempt(run) for run in runs}
        except (RepositoryHostError, ValueError) as error:
            logger.warning("CI triage: re-run state of PR #%d unreadable: %s", rework.pr_number, error)
            return self._defer(state, rework, _report(
                assessments, CiFailureKind.TRANSIENT, head_sha, (),
                note=f"The re-run state could not be read ({error}), so no re-run was requested.",
            ))
        failed_in = {a.job_id: a.run_id for a in assessments if a.job_id is not None and a.run_id is not None}
        if any(attempts[run].attempt > 1 and job in attempts[run].job_ids for job, run in failed_in.items()):
            return self._decided(state, rework, _report(
                assessments, CiFailureKind.TRANSIENT, head_sha, records, reran=True,
                note="These jobs already failed on a re-run of this head.",
            ))
        if head_sha in escalated_heads(bodies):
            return None  # already handed to a person; they re-run it or it waits
        remaining = tuple(run for run in runs if attempts[run].attempt == 1)
        latest = max(records, key=lambda record: record.requested_at) if records else None
        if latest is None and remaining:
            state.ci_triage_deferrals.pop(rework.pr_number, None)
            return _rerun(rework, head_sha, assessments, self.clock(), run_ids=remaining)
        # Waiting on GitHub: since io asked (its record), or since GitHub started
        # a re-run nobody recorded (the run's own start time). Both clocks are
        # durable, so the wait is bounded across restarts; then a person.
        since = latest.requested_at if latest is not None else max(
            attempts[run].started_at for run in runs if attempts[run].attempt > 1
        )
        if self.clock() - since < RERUN_START_GRACE:
            return None
        return self._unconfirmed(state, rework, head_sha, None if latest is None else latest.requested_at)

    def _unconfirmed(
        self, state: "OrchestratorState", rework: DiscoveredRework, head_sha: str, asked_at: datetime | None
    ) -> DiscoveredAwaitingMergeEscalation:
        """A re-run that never restarted the failed jobs: a person decides, not a coding agent."""
        state.ci_triage_deferrals.pop(rework.pr_number, None)
        asked = (
            f"io asked GitHub at {asked_at.isoformat()} to re-run this PR's transient CI failure"
            if asked_at is not None else "GitHub shows a re-run of this PR's transient CI failure"
        )
        return DiscoveredAwaitingMergeEscalation(
            issue_number=rework.issue_number, pr_number=rework.pr_number,
            pr_url="", issue_key=str(rework.issue_number),
            rework_cycle=rework.rework_cycle, kind="ci_rerun_unconfirmed",
            reason=(
                f"{asked} on head {head_sha[:12]} (its one re-run for this head), and the failed "
                "jobs have not run again. Check that the engine's GitHub credential can write "
                "Actions and that runners are available, then re-run the failed jobs; io follows "
                f"the new attempt from there. {rerun_escalated_marker(head_sha)}"
            ),
        )

    def _defer(
        self, state: "OrchestratorState", rework: DiscoveredRework, report: str
    ) -> DiscoveredRework | None:
        """A read the decision needs failed: try again next scan, a bounded number
        of times, then let the rework go with what could be read."""
        deferrals = state.ci_triage_deferrals.get(rework.pr_number, 0)
        if deferrals < MAX_READ_DEFERRALS:
            state.ci_triage_deferrals[rework.pr_number] = deferrals + 1
            return None
        return self._decided(state, rework, f"{report}\n- The engine retried for {MAX_READ_DEFERRALS} scans before sending this to rework.")

    def _decided(
        self, state: "OrchestratorState", rework: DiscoveredRework, report: str
    ) -> DiscoveredRework:
        state.ci_triage_deferrals.pop(rework.pr_number, None)
        return _with_report(rework, report)

    def _assess_all(
        self, state: "OrchestratorState", targets: Sequence["FailedCheck"]
    ) -> tuple[tuple[CiJobAssessment, ...], dict[str, str]]:
        """Every target's assessment so far, and why each unassessed one is not.

        Reads at most ``MAX_LOG_READS_PER_SCAN`` new logs per scan (the memo
        carries the rest forward) and stops reading at the first genuine
        failure, which decides the answer whatever the remaining logs say.
        """
        signatures = CiFailureSignatures.compile(
            self.config.transient_signatures, self.config.genuine_signatures
        )
        assessments: list[CiJobAssessment] = []
        unread: dict[str, str] = {}
        reads = 0
        for index, check in enumerate(targets):
            if check.job_id is None:
                assessments.append(_unreadable(check, "not a GitHub Actions job: the engine can neither read its log nor re-run it"))
                continue
            if index >= MAX_JOBS_PER_HEAD:
                assessments.append(_unreadable(check, f"log not read: at most {MAX_JOBS_PER_HEAD} jobs are read per head"))
                continue
            known = state.ci_job_assessments.get(check.job_id)
            if known is None and any(a.kind is CiFailureKind.GENUINE for a in assessments):
                unread[check.name] = _PENDING
                continue
            if known is None and reads >= MAX_LOG_READS_PER_SCAN:
                unread[check.name] = _PENDING
                continue
            if known is None:
                reads += 1
            assessment = self._assess(state, check, signatures)
            if assessment.unreadable is not None:
                unread[check.name] = f"`{check.name}`: {assessment.unreadable}"
            assessments.append(assessment)
        return tuple(assessments), unread

    def _assess(
        self, state: "OrchestratorState", check: "FailedCheck", signatures: CiFailureSignatures
    ) -> CiJobAssessment:
        """One job's assessment, its log read once (memoized by job id)."""
        assert check.job_id is not None
        memo = state.ci_job_assessments
        assessment = memo.get(check.job_id)
        if assessment is None:
            try:
                raw = self.host.read_check_job_log_tail(check.job_id, max_bytes=self.config.log_tail_bytes)
            except RepositoryHostError as error:
                logger.warning("CI triage: log of job %d unreadable: %s", check.job_id, error)
                return _unreadable(check, f"log unreadable: {error}")
            log = normalize_log(raw)
            kind, signature = classify_job_log(check.conclusion, log, signatures)
            assessment = CiJobAssessment(
                name=check.name, conclusion=check.conclusion, job_id=check.job_id,
                run_id=check.run_id, kind=kind, signature=signature, excerpt=log_excerpt(log),
            )
            self._remember(state, assessment)
        return assessment

    @staticmethod
    def _remember(state: "OrchestratorState", assessment: CiJobAssessment) -> None:
        assert assessment.job_id is not None
        memo = state.ci_job_assessments
        memo[assessment.job_id] = assessment
        while len(memo) > ASSESSMENT_MEMO_LIMIT:
            memo.popitem(last=False)


def _unreadable(check: "FailedCheck", why: str) -> CiJobAssessment:
    return CiJobAssessment(
        name=check.name, conclusion=check.conclusion, job_id=check.job_id,
        run_id=check.run_id, kind=CiFailureKind.UNKNOWN, signature=None, excerpt="",
        unreadable=why,
    )


def _rerun(
    rework: DiscoveredRework,
    head_sha: str,
    assessments: Sequence[CiJobAssessment],
    now: datetime,
    *,
    run_ids: tuple[int, ...],
) -> DiscoveredCiRerun:
    job_ids = tuple(sorted(a.job_id for a in assessments if a.job_id is not None))
    lines = [
        rerun_marker(head_sha, job_ids, now),
        f"io re-ran the failed CI job(s) of head `{head_sha[:12]}` once: every failure "
        "matched a transient signature. This does not count as a rework cycle; a second "
        "failure on this head goes to rework with the job log attached.",
        "",
    ]
    lines += [f"- `{a.name}` (job {a.job_id}, {a.conclusion}): `{a.signature}`" for a in assessments]
    return DiscoveredCiRerun(
        issue_number=rework.issue_number, pr_number=rework.pr_number, head_sha=head_sha,
        run_ids=run_ids, job_ids=job_ids, comment="\n".join(lines),
    )


def _report(
    assessments: Sequence[CiJobAssessment],
    kind: CiFailureKind,
    head_sha: str,
    records: Sequence[CiRerunRecord],
    *,
    note: str | None = None,
    reran: bool = False,
) -> str:
    lines = [f"CI failure triage of head {head_sha[:12]} (the engine read these job logs for you):",
             f"- Classification: {kind.value}"]
    if records or reran:
        lines.append(
            f"- CI re-runs already spent on this head: {max(len(records), 1)} (re-runs are not rework "
            "cycles). The failure persisted, so it is treated as real: fix it, or if it is a "
            "known flake, say so in your completion."
        )
    if note:
        lines.append(f"- {note}")
    precedence = {CiFailureKind.GENUINE: 0, CiFailureKind.UNKNOWN: 1, CiFailureKind.TRANSIENT: 2}
    shown = sorted((a for a in assessments if a.excerpt), key=lambda a: precedence[a.kind])
    excerpted = {id(a) for a in shown[:MAX_EXCERPTS_IN_BRIEF]}
    for a in assessments:
        why = f"signature `{a.signature}`" if a.signature else (a.unreadable or "no known signature")
        lines += ["", f"Job `{a.name}` (job {a.job_id}, conclusion {a.conclusion}): {a.kind.value}, {why}"]
        if a.excerpt and id(a) not in excerpted:
            lines.append(f"(log excerpt omitted: a brief carries at most {MAX_EXCERPTS_IN_BRIEF})")
        elif a.excerpt:
            lines += ["```text", a.excerpt.replace("```", "'''"), "```"]
    return "\n".join(lines)


def _with_report(rework: DiscoveredRework, report: str) -> DiscoveredRework:
    return replace(rework, feedback=f"{rework.feedback or ''}\n\n{report}".lstrip())


def plan_ci_reruns(facts: Sequence[DiscoveredCiRerun]) -> list["RerunFailedChecksAction"]:
    """One re-run action per discovered fact; the planner adds nothing to it."""
    from .actions import RerunFailedChecksAction

    return [
        RerunFailedChecksAction(
            issue_number=fact.issue_number, pr_number=fact.pr_number, head_sha=fact.head_sha,
            run_ids=fact.run_ids, job_ids=fact.job_ids, comment=fact.comment,
            reason=f"PR #{fact.pr_number}: transient CI failure on {fact.head_sha[:12]}, re-run once",
        )
        for fact in facts
    ]


def apply_rerun_failed_checks(
    action: "RerunFailedChecksAction",
    *,
    rerun: Callable[[int], None],
    post_comment: Callable[[int, str], object],
) -> "ActionResult":
    """Record the re-run on the PR, THEN ask GitHub to re-run each run.

    The record is the intent, written first: once it is on the PR the triage
    never asks again for that head, whatever happened to the request (a
    restart, a lost response, a refusal). A request GitHub never acted on is
    handed to a person after the start grace - never to a coding agent. A
    failed record write asks GitHub nothing.
    """
    from .action_results import ActionResult

    try:
        post_comment(action.pr_number, action.comment)
    except RepositoryHostError as error:
        return ActionResult.fail_from(action, error, pr_number=action.pr_number)
    for run_id in action.run_ids:
        try:
            rerun(run_id)
        except RepositoryHostError as error:
            logger.error("CI re-run of run %d (PR #%d) failed: %s", run_id, action.pr_number, error)
            return ActionResult.fail_from(action, error, pr_number=action.pr_number, run_id=run_id)
    logger.info("Re-ran failed CI jobs %s of PR #%d (head %s)", action.job_ids, action.pr_number, action.head_sha[:12])
    return ActionResult.ok(action, issue_number=action.issue_number, pr_number=action.pr_number)
