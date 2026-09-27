"""Live drivers for the exam cases: plant the fault, drive, observe, grade.

Each driver owns HOW its fault is planted; the right answer it is graded
against lives in :mod:`issue_orchestrator.testing.exam.cases`, where unit
tests pin it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.infra.config import Config
from issue_orchestrator.testing.exam import ExamCase, ExamObservation, RunEnd, Scorecard, grade
from issue_orchestrator.testing.exam.tech_lead import actions_resolved
from issue_orchestrator.testing.exam.cases import (
    SUBJECT,
    blocked_issue_green_pr_awaiting_review,
    halted_exchange_with_validated_work,
    stale_claim_paused_for_reconcile,
)

from tests.e2e.exam.agents import CODER_LABEL
from tests.e2e.exam.driving import drive, settle
from tests.e2e.exam.run_identity import RunIdentity
from tests.e2e.exam.engine import (
    EngineCheckout,
    ExamEngine,
    exam_config,
)
from tests.e2e.exam.observe import (
    TrackedItem,
    build_observation,
    linked_pull_requests,
    observe_item,
    observe_tech_lead_runs,
    owned_numbers,
    parked_screen,
    terminal_tech_lead_runs,
)
from tests.e2e.exam.seeding import E2E_DATA_LABEL, seed_pull_request, wait_for_checks
from tests.e2e.fixtures import fetch_gh_audit_report
from tests.e2e.flows import E2EFlow

logger = logging.getLogger(__name__)


#: Title ids (``[M0-760]``): e2e issues must carry one, and the engine keys
#: an item's snapshot and some of its events by it.
CASE_A_EXTERNAL_ID = "M0-760"
CASE_B_EXTERNAL_ID = "M0-761"
CASE_C_EXTERNAL_ID = "M0-762"

#: Production tech-lead authority for every act-level action (the operator's
#: io/porchpin configs), so a destructive remedy is really executed — and
#: really graded — as it would be. Filing is proposed instead: an executed
#: follow-up is worked by the exam's scripted coder (the first Case B run
#: grew a second investigation that way), and a case file is a real issue
#: other engines' case-file readers could pick up. A proposed escalation
#: still grades as a remedy.
EXAM_TECH_LEAD_AUTHORITY = {
    "reset_retry": "execute",
    "kill_hung_session": "execute",
    "request_rework": "execute",
    "recover_validated_work": "execute",
    "create_issue": "propose",
    "flag_pattern": "propose",
}


@dataclass(frozen=True)
class ExamResult:
    """The raw observation (so a scorecard can be re-graded) and its grade."""

    observation: ExamObservation
    scorecard: Scorecard


@dataclass
class ExamRun:
    """Everything one case run needs, and what it accumulated."""

    case: ExamCase
    repo: str
    harness_root: Path
    engine_ref: str
    base_config: Config
    identity: RunIdentity
    notes: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    """Remote branches the harness pushed, registered the moment they exist
    so cleanup can delete them even if the PR was never created."""

    @property
    def run_label(self) -> str:
        return self.identity.label


def goals_met_probe(
    run: ExamRun, engine: ExamEngine, item: TrackedItem, *, every_s: float = 60.0
) -> Callable[[], Awaitable[bool]]:
    """``done`` for :func:`drive`: the case's own goals, on GitHub's state.

    The goals ARE the definition of done, so the loop asks them rather than a
    second, drift-prone predicate over the watcher's partial view (which, for
    one, never learns a PR's draft flag). Throttled to one GitHub read per
    ``every_s``.
    """
    last = 0.0

    async def done() -> bool:
        nonlocal last
        now = time.monotonic()
        if now - last < every_s:
            return False
        last = now
        fact = observe_item(
            repo=run.repo,
            config=engine.config,
            item=item,
            watcher=engine.runtime.watcher,
            parked_screen="",
            read_checks=False,
        )
        return all(goal.check(fact).passed for goal in run.case.goals if goal.role == item.role)

    return done


async def _finish(
    run: ExamRun,
    engine: ExamEngine,
    *,
    items: list[TrackedItem],
    extra_prs: dict[str, list[int]],
    started: float,
    ended_by: RunEnd,
) -> ExamResult:
    watcher = engine.runtime.watcher
    alive = engine.is_running()
    # A dead engine has no control API: no live sessions to inspect and no
    # audit report to fetch. build_observation allows a missing report only
    # for ENGINE_EXITED, and every scorecard fails on it.
    active = engine.active_session_issues() if alive else ()
    report = fetch_gh_audit_report(engine.config.control_api_port) if alive else None
    observation = build_observation(
        case_id=run.case.case_id,
        engine_commit=engine.checkout.commit,
        items=tuple(
            observe_item(
                repo=run.repo,
                config=engine.config,
                item=item,
                watcher=watcher,
                parked_screen=parked_screen(
                    worktree_base=engine.config.worktree_base,
                    active_sessions=active,
                    issue_number=item.issue_number,
                ),
                extra_pr_numbers=extra_prs.get(item.role, ()),
            )
            for item in items
        ),
        tech_lead_runs=observe_tech_lead_runs(
            engine.checkout.state_dir, watcher, worktree_base=engine.config.worktree_base
        ),
        events=list(watcher.view.global_events),
        owned=owned_numbers(
            run.repo, run.run_label, (n for numbers in extra_prs.values() for n in numbers)
        ),
        gh_audit_report=report,
        elapsed_seconds=time.monotonic() - started,
        ended_by=ended_by,
        notes=tuple(run.notes),
    )
    return ExamResult(observation=observation, scorecard=grade(run.case, observation))


def _labels(config: Config) -> LabelManager:
    """The engine's own label vocabulary, never re-spelled here."""
    return LabelManager(config)


# ---------------------------------------------------------------------------
# Case A
# ---------------------------------------------------------------------------


async def run_case_a(run: ExamRun, flow_cleanup: list[E2EFlow]) -> ExamResult:
    checkout = EngineCheckout.create(
        harness_root=run.harness_root, ref=run.engine_ref, identity=run.identity, repo=run.repo
    )
    try:
        config = exam_config(
            run.base_config,
            checkout=checkout,
            run_label=run.run_label,
            reviewer_exchange_fault="exit-silently",
        )
        overlay = {
            "review": {
                "exchange": {
                    "mode": "via-local-loop",
                    "loop": {"max_rounds": 3, "max_no_progress": 2, "require_validation": True},
                },
                "max_consecutive_review_exchange_failures": 3,
            }
        }
        engine = ExamEngine(config, checkout, overlay=overlay)
        runtime = await engine.start()
        try:
            flow = E2EFlow(repo=run.repo, watcher=runtime.watcher, filter_label=run.run_label)
            flow_cleanup.append(flow)
            started = time.monotonic()
            _, number = flow.create_issue(
                f"[{CASE_A_EXTERNAL_ID}] [EXAM-A] Halted review exchange with validated work",
                [CODER_LABEL, E2E_DATA_LABEL],
                body="Tech-lead exam case A: the exchange reviewer never answers.",
            )
            subject = TrackedItem(SUBJECT, number, external_id=CASE_A_EXTERNAL_ID)
            ended_by = await drive(
                engine,
                done=goals_met_probe(run, engine, subject),
                quiet_s=420,
                timeout_s=45 * 60,
            )
            return await _finish(
                run,
                engine,
                items=[subject],
                extra_prs={},
                started=started,
                ended_by=ended_by,
            )
        finally:
            await engine.close()
    finally:
        checkout.remove()


# ---------------------------------------------------------------------------
# Case B
# ---------------------------------------------------------------------------


async def run_case_b(
    run: ExamRun, flow_cleanup: list[E2EFlow], *, tech_lead_model: str
) -> ExamResult:
    checkout = EngineCheckout.create(
        harness_root=run.harness_root, ref=run.engine_ref, identity=run.identity, repo=run.repo
    )
    try:
        config = exam_config(
            run.base_config,
            checkout=checkout,
            run_label=run.run_label,
            reviewer_exchange_fault="none",
            tech_lead_model=tech_lead_model,
        )
        overlay: dict[str, Any] = {
            "review": {
                "tech_lead_follow_up_agent": CODER_LABEL,
                "tech_lead_review_on_failure": True,
            },
            "tech_lead": {
                "max_concurrent": 1,
                "explicit_labels": [E2E_DATA_LABEL],
                "inherit_labels": [E2E_DATA_LABEL],
                "authority": dict(EXAM_TECH_LEAD_AUTHORITY),
                "findings": {"promote": "off"},
                "health_review": {"interval_minutes": 0},
                # The path porchpin took (#7293): the sweep finds the blocked
                # issue and sends it to a tech-lead investigation. Production
                # runs it every 240 minutes; the exam cannot wait that long.
                # Its scan is scoped by filtering.label, so it only ever sees
                # this run's issues.
                "stuck_sweep": {"enabled": True, "interval_minutes": 1, "max_recovery_attempts": 3},
            },
        }
        labels = _labels(config)
        blocked_failed = labels.blocked_failed
        flow = E2EFlow(repo=run.repo, watcher=None, filter_label=run.run_label)
        flow_cleanup.append(flow)
        flow.ensure_labels([blocked_failed, labels.pr_pending, labels.code_review])
        issue, number = flow.create_issue(
            f"[{CASE_B_EXTERNAL_ID}] [EXAM-B] Blocked issue whose green PR waits on review",
            [CODER_LABEL, E2E_DATA_LABEL, blocked_failed, labels.pr_pending],
            body=(
                "Tech-lead exam case B: this issue carries blocked-failed while its"
                " open PR is green and waiting on code review."
            ),
        )
        seeded = seed_pull_request(
            repo=run.repo,
            repo_root=run.harness_root,
            issue_number=number,
            slug="exam-b-green-pr-awaiting-review",
            labels=[labels.code_review, run.run_label, E2E_DATA_LABEL],
            draft=True,
            register_branch=run.branches.append,
        )
        checks = wait_for_checks(run.repo, seeded.number, timeout_s=15 * 60)
        if checks != "SUCCESS":
            # The case is "a GREEN PR waits on review"; without green checks
            # the fault was not planted, and any grade would be meaningless.
            raise RuntimeError(
                f"seeded PR #{seeded.number} checks are {checks}, not SUCCESS;"
                " case B's premise was not planted"
            )

        engine = ExamEngine(config, checkout, overlay=overlay)
        runtime = await engine.start()
        try:
            started = time.monotonic()
            flow.watcher = runtime.watcher
            try:
                await flow.issue_seen(issue, timeout_s=180)
            except TimeoutError:
                # Not a harness failure: an item the engine cannot see is a
                # finding the scorecard must carry.
                run.notes.append(
                    f"subject #{number} never appeared in the engine's snapshot within 180s"
                )

            async def concluded() -> bool:
                return bool(terminal_tech_lead_runs(checkout.state_dir))

            ended_by = await drive(
                engine,
                done=concluded,
                quiet_s=600,
                timeout_s=60 * 60,
                reached=RunEnd.TECH_LEAD_CONCLUDED,
            )
            if ended_by is RunEnd.TECH_LEAD_CONCLUDED:
                # Let the engine apply the decision it just accepted: wait,
                # bounded, until every concluded run's actions have a fate.
                resolved = await settle(
                    lambda: actions_resolved(
                        observe_tech_lead_runs(
                            checkout.state_dir,
                            runtime.watcher,
                            worktree_base=engine.config.worktree_base,
                        )
                    ),
                    timeout_s=5 * 60,
                )
                if not resolved:
                    run.notes.append("tech-lead actions still unresolved 5 min after the run concluded")
            return await _finish(
                run,
                engine,
                items=[TrackedItem(SUBJECT, number, external_id=CASE_B_EXTERNAL_ID)],
                extra_prs={SUBJECT: [seeded.number]},
                started=started,
                ended_by=ended_by,
            )
        finally:
            await engine.close()
    finally:
        checkout.remove()


# ---------------------------------------------------------------------------
# Case C
# ---------------------------------------------------------------------------

#: How long Case C watches: ~25 engine ticks — far past the livelock
#: threshold if the engine loops, and nothing is expected to finish.
CASE_C_WINDOW_S = 6 * 60


async def run_case_c(run: ExamRun, flow_cleanup: list[E2EFlow]) -> ExamResult:
    checkout = EngineCheckout.create(
        harness_root=run.harness_root, ref=run.engine_ref, identity=run.identity, repo=run.repo
    )
    try:
        config = exam_config(
            run.base_config,
            checkout=checkout,
            run_label=run.run_label,
            reviewer_exchange_fault="none",
        )
        labels = _labels(config)
        flow = E2EFlow(repo=run.repo, watcher=None, filter_label=run.run_label)
        flow_cleanup.append(flow)
        flow.ensure_labels([labels.in_progress, labels.needs_reconcile])
        _, number = flow.create_issue(
            f"[{CASE_C_EXTERNAL_ID}] [EXAM-C] Stale claim paused for reconciliation",
            [CODER_LABEL, E2E_DATA_LABEL, labels.in_progress, labels.needs_reconcile],
            body=(
                "Tech-lead exam case C: in-progress with no session, paused with the"
                " engine's needs-reconcile label (porchpin#410's shape)."
            ),
        )
        engine = ExamEngine(config, checkout, overlay={})
        await engine.start()
        try:
            started = time.monotonic()

            async def window_elapsed() -> bool:
                return time.monotonic() - started >= CASE_C_WINDOW_S

            # Never quiescent by design: a livelocked engine is never quiet,
            # and a correct one has nothing to finish. Watch a fixed window.
            ended_by = await drive(
                engine,
                done=window_elapsed,
                quiet_s=CASE_C_WINDOW_S * 10,
                timeout_s=CASE_C_WINDOW_S * 2,
                reached=RunEnd.WINDOW_ELAPSED,
            )
            return await _finish(
                run,
                engine,
                items=[TrackedItem(SUBJECT, number, external_id=CASE_C_EXTERNAL_ID)],
                extra_prs={},
                started=started,
                ended_by=ended_by,
            )
        finally:
            await engine.close()
    finally:
        checkout.remove()


def case_c(config: Config) -> ExamCase:
    return stale_claim_paused_for_reconcile(needs_reconcile_label=_labels(config).needs_reconcile)


def case_a(config: Config) -> ExamCase:
    labels = _labels(config)
    return halted_exchange_with_validated_work(
        code_reviewed_label=labels.code_reviewed,
        blocked_failed_label=labels.blocked_failed,
        needs_human_label=labels.needs_human,
    )


def case_b(config: Config) -> ExamCase:
    return blocked_issue_green_pr_awaiting_review(
        blocked_failed_label=_labels(config).blocked_failed
    )
