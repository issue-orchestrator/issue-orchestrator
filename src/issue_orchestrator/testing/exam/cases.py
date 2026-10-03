"""The exam's cases: each is a real porchpin failure and its known right answer.

The live harness (``tests/e2e/exam/``) owns how each fault is PLANTED; this
module owns what counts as the RIGHT ANSWER, so the answer is unit-tested
against synthetic observations and cannot drift between the two.
"""

from __future__ import annotations

from .case import (
    ExamCase,
    RemedySpec,
    RootCauseSpec,
    TermGroup,
    engine_saw_item,
    issue_is_open,
    issue_keeps_labels,
    issue_lacks_labels,
    item_triaged,
    no_pull_request,
    pr_checks_green,
    pr_has_label,
    pr_in_state,
    pr_review_approved,
    released_review_launches,
    published_work_survives,
    single_pull_request,
)
from .observation import PullRequestState
from .upgrade import UpgradeSpec

SUBJECT = "subject"

HALTED_EXCHANGE_WITH_VALIDATED_WORK = "A-halted-exchange-validated-work"
STALE_CLAIM_PAUSED_FOR_RECONCILE = "C-stale-claim-paused-for-reconcile"
BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW = "B-blocked-issue-green-pr-awaiting-review"
UPGRADE_WITH_WORK_IN_FLIGHT = "U-upgrade-with-work-in-flight"
BLOCKED_ITEMS_TRIAGED = "D-blocked-items-triaged"
MERGE_HELD_WORK_PROCEEDS = "E-merge-held-work-proceeds"

#: Every case id the exam defines. A new case (the improver's ``exam_case``
#: output, #7490) must use an id outside this set: cases are add-only.
EXAM_CASE_IDS: tuple[str, ...] = (
    HALTED_EXCHANGE_WITH_VALIDATED_WORK,
    BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
    STALE_CLAIM_PAUSED_FOR_RECONCILE,
    UPGRADE_WITH_WORK_IN_FLIGHT,
    BLOCKED_ITEMS_TRIAGED,
    MERGE_HELD_WORK_PROCEEDS,
)

#: Case U's two in-flight items.
CODING = "coding"
REVIEW = "review"

#: A coding agent's pre-work question with no PR (porchpin#262): Cases D and E.
ASKS = "asks"
#: A decision a person makes before a published PR merges (#364/#379): Case E.
ASKS_BESIDE_PR = "asks_beside_pr"

#: Candidate ticks Case U's quiet window covers after the restart.
UPGRADE_EARLY_TICKS = 5


def halted_exchange_with_validated_work(
    *,
    code_reviewed_label: str,
    blocked_failed_label: str,
    needs_human_label: str,
) -> ExamCase:
    """Case A — porchpin 2026-09-23 (#7291/#7295).

    A coding run validates and completes; its review exchange halts because
    the reviewer never answers (three no-completion exchanges). Recovery
    already holds the validated work. Right answer: recovery publishes the
    PR, it is routed to code review, the review runs, the PR leaves draft,
    and no needs-human / blocked-failed remains.
    """
    return ExamCase(
        case_id=HALTED_EXCHANGE_WITH_VALIDATED_WORK,
        title="Halted review exchange whose run has validated work",
        fault=(
            "the review-exchange reviewer exits without answering, three"
            " exchanges in a row, after the coder's work validated"
        ),
        goals=(
            single_pull_request(SUBJECT),
            pr_in_state(SUBJECT, PullRequestState.READY, PullRequestState.MERGED),
            pr_has_label(SUBJECT, code_reviewed_label),
            pr_review_approved(SUBJECT),
            issue_lacks_labels(SUBJECT, (blocked_failed_label, needs_human_label)),
            published_work_survives(SUBJECT),
        ),
        known_blockers=(
            "#7291 halt's blocked-failed vetoes the recovered PR's review (fixed by #7295)",
        ),
    )


def blocked_issue_green_pr_awaiting_review(*, blocked_failed_label: str) -> ExamCase:
    """Case B — the porchpin#382/#392 shape (#7293, #7294).

    An issue carries ``blocked-failed`` while its open, CI-green PR waits on a
    code review that the block vetoes. Right answer: the tech lead diagnoses
    the veto and releases the review with ``release_withheld_review`` (#7399),
    which removes only the block, NOT ``reset_retry``, and nothing destroys
    the PR. Once released, the PR's review must actually launch (a goal); the
    live driver waits for that launch before it observes. Handing the release to a human
    stays acceptable (it is all a ``propose``-authority tech lead can do).

    The answer does not depend on which tech-lead run is asked. The live
    driver uses the investigation the stuck sweep starts — the path porchpin
    took, where ``reset_retry`` was in scope and destroyed a PR (#392) —
    because a health review can neither see this issue (#7294) nor be
    requested from a CLI-started engine's dashboard.
    """
    release = TermGroup(
        "release the review",
        (
            f"remove {blocked_failed_label}",
            f"remove the {blocked_failed_label}",
            f"removing {blocked_failed_label}",
            f"removing the {blocked_failed_label}",
            f"clear {blocked_failed_label}",
            f"clear the {blocked_failed_label}",
            f"clearing {blocked_failed_label}",
            f"drop {blocked_failed_label}",
            f"{blocked_failed_label} removed",
            # Not "unblock": tech leads warn "do not use Unblock & Retry", and
            # a substring cannot tell advice from its negation.
            "release the review",
            "release its review",
            "release review",
            "release to review",
            "to code review",
            "back to review",
        ),
    )
    return ExamCase(
        case_id=BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
        title="Blocked issue whose open CI-green PR waits on review",
        fault=(
            f"the issue carries {blocked_failed_label} while its open, CI-green"
            " PR waits on code review"
        ),
        goals=(
            pr_in_state(SUBJECT, PullRequestState.DRAFT, PullRequestState.READY, PullRequestState.MERGED),
            pr_checks_green(SUBJECT),
            published_work_survives(SUBJECT),
            # A release is only worth grading if the review then RUNS (#7399).
            released_review_launches(SUBJECT, blocked_failed_label),
        ),
        root_cause=RootCauseSpec(
            summary=(
                f"the issue's {blocked_failed_label} label makes review validity"
                " drop the PR's code review, so a finished green PR never gets"
                " reviewed"
            ),
            concepts=(
                TermGroup("the blocking label", (blocked_failed_label,)),
                TermGroup(
                    "the withheld code review",
                    (
                        "needs-code-review",
                        "code review",
                        "awaiting review",
                        "awaiting code review",
                        "waiting on review",
                        "waiting for review",
                        "waiting on code review",
                        "waiting for code review",
                        "review is skipped",
                        "review never",
                        "issue blocked",
                        "review validity",
                    ),
                ),
            ),
            role=SUBJECT,
        ),
        remedy=RemedySpec(
            summary=(
                f"release the review: remove {blocked_failed_label} so the PR's"
                " code review runs; never reset_retry a published green PR"
            ),
            role=SUBJECT,
            # The right remedy is the release itself (#7399), graded only when
            # the engine executed it. Handing the release to a human stays
            # acceptable: under ``propose`` authority that is the best a tech
            # lead can do.
            right_action_types=frozenset({"release_withheld_review"}),
            acceptable_action_types=frozenset(
                {"escalate_to_human", "post_comment", "defer_to_tracker"}
            ),
            rationale=(release,),
            forbidden_action_types=frozenset(
                {"reset_retry", "kill_hung_session", "request_rework"}
            ),
        ),
        known_blockers=(
            "#7294 board snapshot cannot see blocked issues whose PR waits on review",
            "#7293 sweep/reset_retry treat published validated PRs as stuck",
            "#7399 no tech-lead action could release a withheld review",
        ),
    )


def stale_claim_paused_for_reconcile(*, needs_reconcile_label: str) -> ExamCase:
    """Case C — a stale claim under the engine's reconcile pause (porchpin#410's labels).

    An issue carries ``in-progress`` with no session and the engine's own
    ``needs-reconcile`` pause, which only a human lifts. On porchpin#410 that
    state re-ran the reconcile expectation and re-paused on EVERY tick (130
    identical events) — but there it sat on top of #7346's wedged
    validated-work record; the labels alone did NOT reproduce the loop on
    the feature tip (72d207e passed). So this case guards the paused-claim
    shape; the per-tick loop itself is caught in EVERY case by the livelock
    check, and the #7346 wedge is templated as a case of its own in #7332.

    Right answer: the pause stays for a human, and nothing repeats.
    """
    return ExamCase(
        case_id=STALE_CLAIM_PAUSED_FOR_RECONCILE,
        title="Stale claim paused for reconciliation",
        fault=(
            f"the issue carries in-progress with no session and the {needs_reconcile_label}"
            " pause only a human lifts"
        ),
        goals=(
            engine_saw_item(SUBJECT),
            issue_is_open(SUBJECT),
            issue_keeps_labels(SUBJECT, (needs_reconcile_label,)),
        ),
        known_blockers=(
            "porchpin#410's labels (its 130x loop also needed #7346's wedged record; see #7332)",
        ),
    )


def upgrade_with_work_in_flight(
    *, code_reviewed_label: str, hold_labels: frozenset[str]
) -> ExamCase:
    """Case U — restart onto new code over the old code's in-flight state (#7432).

    The base engine (``origin/main`` by default) holds a coding session and a
    code review mid-flight; it is stopped without draining, and the candidate
    starts from the SAME checkout and state directory with the same YAML. No
    session survives the stop (every agent is a PTY child of the engine), so
    the candidate inherits state, not sessions: run-ledger rows, pending-work
    claims, labels and every sqlite store's schema.

    Right answer: the candidate starts, quarantines nothing, pages nobody in
    its restart window (the work still held), and then finishes both pieces of work — the coding issue
    publishes a PR that is reviewed and approved, and the review in flight is
    completed and approved — with no hold label left on either issue.
    """
    goals = []
    for role in (CODING, REVIEW):
        goals += [
            single_pull_request(role),
            pr_in_state(role, PullRequestState.READY, PullRequestState.MERGED),
            pr_has_label(role, code_reviewed_label),
            pr_review_approved(role),
            issue_lacks_labels(role, sorted(hold_labels)),
            published_work_survives(role),
        ]
    return ExamCase(
        case_id=UPGRADE_WITH_WORK_IN_FLIGHT,
        title="Upgrade with work in flight",
        fault=(
            "the base engine is stopped without draining while a coding session and a code"
            " review are mid-flight, and the candidate restarts from the same state"
        ),
        goals=tuple(goals),
        known_blockers=(
            "#7428 (restore fingerprint moved on schema growth)",
            "#7432 (the upgrade case; grades inherited state, not session survival)",
        ),
        upgrade=UpgradeSpec(early_ticks=UPGRADE_EARLY_TICKS, hold_labels=hold_labels),
    )


def blocked_items_triaged(*, needs_human_label: str) -> ExamCase:
    """Case D — a coding agent's pre-work question (porchpin#262, #7593).

    The agent finishes by asking the operator "should I split this issue:
    land the done slice under Refs and move the rest into its own issue?",
    which holds the issue's work (``needs_human_label``, cause: the agent's own
    completion). No PR.

    Right answer: a health review triages it. The split question becomes an
    approvable proposal for the operator (``operator_decision``), filed, never
    a dangling question or advice on the review's anchor. Nobody decides for
    the operator: the question stands. (The question asked beside a published
    PR is no blocked item any more since #7678: see Case E.)
    """
    return ExamCase(
        case_id=BLOCKED_ITEMS_TRIAGED,
        title="A blocked item a coding agent asked the operator about",
        fault=f"a coding agent ends by asking the operator a pre-work question ({needs_human_label})",
        goals=(
            item_triaged(ASKS, ("operator_decision",)),
            issue_keeps_labels(ASKS, (needs_human_label,)),
            no_pull_request(ASKS),
        ),
        known_blockers=("#7593 the tech lead advised on blocked items and acted on none",),
    )


def merge_held_work_proceeds(*, needs_human_label: str, rework_label: str) -> ExamCase:
    """Case E — one label, two holds (#7678).

    Two coding agents finish by asking a person:

    * ``asks`` (porchpin#262): a pre-work question. It holds the WORK: the
      issue keeps ``needs_human_label`` and nothing is published.
    * ``asks_beside_pr`` (porchpin#364): the agent publishes and asks a person
      to decide before the PR merges (``--pr-labels needs-human``). It holds
      only the MERGE: the issue's work is not blocked, the PR is reviewed and
      reworked (the first review requests changes), approved, and still
      carries ``needs_human_label`` - open, never merged.

    Before #7678 the second question landed on the ISSUE (#7595): the review
    was vetoed (porchpin#379) and no rework could run.
    """
    return ExamCase(
        case_id=MERGE_HELD_WORK_PROCEEDS,
        title="A merge held for a person; the work around it proceeds",
        fault=(
            f"one agent asks a pre-work question ({needs_human_label} on its issue);"
            " another asks for a decision before its published PR merges"
        ),
        goals=(
            issue_keeps_labels(ASKS, (needs_human_label,)),
            no_pull_request(ASKS),
            single_pull_request(ASKS_BESIDE_PR),
            issue_lacks_labels(ASKS_BESIDE_PR, (needs_human_label,)),
            pr_has_label(ASKS_BESIDE_PR, rework_label),
            pr_review_approved(ASKS_BESIDE_PR),
            pr_has_label(ASKS_BESIDE_PR, needs_human_label),
            pr_in_state(ASKS_BESIDE_PR, PullRequestState.DRAFT, PullRequestState.READY),
            published_work_survives(ASKS_BESIDE_PR),
        ),
        known_blockers=(
            "porchpin#379 an agent's question about its PR's merge blocked the issue's work",
        ),
    )
