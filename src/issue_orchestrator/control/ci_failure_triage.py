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

The once-per-head bound is durable: the applier posts the re-run marker on the
PR before it asks GitHub to re-run, so a second transient failure on the same
head - after a restart too - goes to rework like any other failure. Nothing
here loops.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..domain.ci_failure import (
    RERUN_MARKER_PREFIX,
    CiFailureKind,
    CiFailureSignatures,
    CiJobAssessment,
    CiRerunRecord,
    classify_job_log,
    log_excerpt,
    normalize_log,
    overall_kind,
    parse_rerun_records,
    rerun_marker,
)
from ..domain.models import DiscoveredCiRerun, DiscoveredRework
from ..ports.repository_host import RepositoryHostError

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config_models import CiFailureTriageConfig
    from ..ports.pull_request_tracker import FailedCheck
    from ..ports.repository_host import RepositoryHost
    from .action_results import ActionResult
    from .actions import RerunFailedChecksAction

logger = logging.getLogger(__name__)

#: At most this many failed jobs' logs are read for one PR.
MAX_JOBS_READ = 3
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
        for rework in reworks:
            if not rework.failed_check:
                kept.append(rework)
                continue
            answer = self._triage(state, rework)
            if isinstance(answer, DiscoveredCiRerun):
                reruns.append(answer)
            elif answer is not None:
                kept.append(answer)
        return CiTriageResult(reworks=tuple(kept), reruns=tuple(reruns))

    # ------------------------------------------------------------------ #

    def _triage(
        self, state: "OrchestratorState", rework: DiscoveredRework
    ) -> DiscoveredRework | DiscoveredCiRerun | None:
        """The rework (with its brief extended), a re-run, or ``None`` (wait)."""
        if not self.config.enabled:
            return _with_report(rework, "CI failure triage is disabled in this repository's config; no job log was read.")
        try:
            failed = self.host.read_failed_checks(rework.pr_number)
        except RepositoryHostError as error:
            logger.warning("CI triage: failed checks of PR #%d unreadable: %s", rework.pr_number, error)
            return _with_report(rework, f"CI failure triage could not read the PR's failed checks: {error}")
        targets = [c for c in failed.checks if c.required] or list(failed.checks)
        if not targets:
            return _with_report(
                rework,
                f"CI failure triage found no failed check on head {failed.head_sha[:12]}; "
                "check the PR's checks before changing code.",
            )
        assessments = self._assess_all(state, targets)
        kind = overall_kind(assessments)
        records: tuple[CiRerunRecord, ...] = ()
        if kind is CiFailureKind.TRANSIENT:
            try:
                records = self._head_records(rework.pr_number, failed.head_sha)
            except (RepositoryHostError, ValueError) as error:
                logger.warning("CI triage: re-run record of PR #%d unreadable: %s", rework.pr_number, error)
                return _with_report(rework, _report(assessments, kind, failed.head_sha, (),
                                                    note=f"The re-run record could not be read ({error}), so no re-run was requested."))
            if not records:
                return _rerun(rework, failed.head_sha, assessments, self.clock())
            latest = max(records, key=lambda record: record.requested_at)
            job_ids = {a.job_id for a in assessments if a.job_id is not None}
            if job_ids <= latest.job_ids:
                if self.clock() - latest.requested_at < RERUN_START_GRACE:
                    logger.info("CI triage: PR #%d re-run requested %s; waiting for it to start",
                                rework.pr_number, latest.requested_at.isoformat())
                    return None
                return _with_report(rework, _report(
                    assessments, kind, failed.head_sha, records,
                    note=f"The re-run requested at {latest.requested_at.isoformat()} never started "
                    "(can the engine's GitHub credential write Actions?).",
                ))
        return _with_report(rework, _report(assessments, kind, failed.head_sha, records))

    def _assess_all(
        self, state: "OrchestratorState", targets: Sequence["FailedCheck"]
    ) -> tuple[CiJobAssessment, ...]:
        signatures = CiFailureSignatures.compile(
            self.config.transient_signatures, self.config.genuine_signatures
        )
        readable = [c for c in targets if c.job_id is not None]
        assessments: list[CiJobAssessment] = []
        for check in targets:
            if check.job_id is None:
                assessments.append(_unreadable(check, "not a GitHub Actions job: the engine can neither read its log nor re-run it"))
            elif check in readable[:MAX_JOBS_READ]:
                assessments.append(self._assess(state, check, signatures))
            else:
                assessments.append(_unreadable(check, f"log not read: at most {MAX_JOBS_READ} job logs are read per PR"))
        return tuple(assessments)

    def _assess(
        self, state: "OrchestratorState", check: "FailedCheck", signatures: CiFailureSignatures
    ) -> CiJobAssessment:
        assert check.job_id is not None
        memo = state.ci_job_assessments
        if check.job_id in memo:
            return memo[check.job_id]
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
        memo[check.job_id] = assessment
        while len(memo) > ASSESSMENT_MEMO_LIMIT:
            memo.popitem(last=False)
        return assessment

    def _head_records(self, pr_number: int, head_sha: str) -> tuple[CiRerunRecord, ...]:
        bodies = self.host.issue_comment_bodies_containing(pr_number, RERUN_MARKER_PREFIX)
        return tuple(r for r in parse_rerun_records(bodies) if r.head_sha == head_sha)


def _unreadable(check: "FailedCheck", why: str) -> CiJobAssessment:
    return CiJobAssessment(
        name=check.name, conclusion=check.conclusion, job_id=check.job_id,
        run_id=check.run_id, kind=CiFailureKind.UNKNOWN, signature=None, excerpt="",
        unreadable=why,
    )


def _rerun(
    rework: DiscoveredRework, head_sha: str, assessments: Sequence[CiJobAssessment], now: datetime
) -> DiscoveredCiRerun:
    job_ids = tuple(sorted(a.job_id for a in assessments if a.job_id is not None))
    run_ids = tuple(sorted({a.run_id for a in assessments if a.run_id is not None}))
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
) -> str:
    lines = [f"CI failure triage of head {head_sha[:12]} (the engine read these job logs for you):",
             f"- Classification: {kind.value}"]
    if records:
        lines.append(
            f"- CI re-runs already spent on this head: {len(records)} (re-runs are not rework "
            "cycles). The failure persisted, so it is treated as real: fix it, or if it is a "
            "known flake, say so in your completion."
        )
    if note:
        lines.append(f"- {note}")
    for a in assessments:
        why = f"signature `{a.signature}`" if a.signature else (a.unreadable or "no known signature")
        lines += ["", f"Job `{a.name}` (job {a.job_id}, conclusion {a.conclusion}): {a.kind.value}, {why}"]
        if a.excerpt:
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
    action: "RerunFailedChecksAction", host: "RepositoryHost"
) -> "ActionResult":
    """Record the re-run on the PR, THEN ask GitHub to re-run.

    The record comes first so the once-per-head bound holds even when the
    re-run request fails: the triage then waits out the start grace and sends
    the failure to rework instead of re-running again.
    """
    from .action_results import ActionResult

    try:
        host.add_comment(action.pr_number, action.comment)
    except RepositoryHostError as error:
        return ActionResult.fail_from(action, error, pr_number=action.pr_number)
    for run_id in action.run_ids:
        try:
            host.rerun_failed_check_jobs(run_id)
        except RepositoryHostError as error:
            logger.error("CI re-run of run %d (PR #%d) failed: %s", run_id, action.pr_number, error)
            return ActionResult.fail_from(action, error, pr_number=action.pr_number, run_id=run_id)
    logger.info("Re-ran failed CI jobs %s of PR #%d (head %s)", action.job_ids, action.pr_number, action.head_sha[:12])
    return ActionResult.ok(action, issue_number=action.issue_number, pr_number=action.pr_number)
