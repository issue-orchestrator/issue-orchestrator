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
    issue_is_open,
    issue_keeps_labels,
    issue_lacks_labels,
    pr_checks_green,
    pr_has_label,
    pr_in_state,
    pr_review_approved,
    published_work_survives,
    single_pull_request,
)
from .observation import PullRequestState

SUBJECT = "subject"

HALTED_EXCHANGE_WITH_VALIDATED_WORK = "A-halted-exchange-validated-work"
STALE_CLAIM_PAUSED_FOR_RECONCILE = "C-stale-claim-paused-for-reconcile"
BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW = "B-blocked-issue-green-pr-awaiting-review"


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
    the veto and proposes releasing the review (removing the block), NOT
    ``reset_retry``, and nothing destroys the PR.

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
            # No tech-lead action type releases a review today (the vocabulary
            # is post_comment/create_issue/escalate/defer/flag_pattern plus
            # reset_retry/kill/request_rework/recover_validated_work), so the
            # best achievable answer is to hand the release to a human.
            right_action_types=frozenset(),
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
            issue_is_open(SUBJECT),
            issue_keeps_labels(SUBJECT, (needs_reconcile_label,)),
        ),
        known_blockers=(
            "porchpin#410's labels (its 130x loop also needed #7346's wedged record; see #7332)",
        ),
    )
