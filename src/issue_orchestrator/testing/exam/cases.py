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
    item_handed_over,
    item_resolved,
    no_tech_lead_decision,
    item_triaged,
    never_worked,
    no_pull_request,
    pr_checks_green,
    pr_has_label,
    pr_in_state,
    pr_review_approved,
    released_review_launches,
    published_work_survives,
    single_pull_request,
    delivery_lists_every_pr,
    integration_steps_applied,
    no_rework,
    contradicting_approval_refused,
    reviews_after_rework_carry_rulings,
    rework_prompts_carry_rulings,
    rulings_in_body,
    resolution_answer_in_body,
    decision_steps_ran_once,
    issue_in_milestone,
    issue_is_closed,
    no_hand_steps_in_prose,
    operator_checklist_names,
    body_ruling_states,
)
from .case import Goal
from .observation import PullRequestState
from .upgrade import UpgradeSpec
from ...domain.standing_ruling import RulingAuthority

SUBJECT = "subject"

HALTED_EXCHANGE_WITH_VALIDATED_WORK = "A-halted-exchange-validated-work"
STALE_CLAIM_PAUSED_FOR_RECONCILE = "C-stale-claim-paused-for-reconcile"
BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW = "B-blocked-issue-green-pr-awaiting-review"
UPGRADE_WITH_WORK_IN_FLIGHT = "U-upgrade-with-work-in-flight"
BLOCKED_ITEMS_TRIAGED = "D-blocked-items-triaged"
POSITIVE_APPROVAL_EXECUTES_ONCE = "H-positive-approval-executes-once"
MERGE_HELD_WORK_PROCEEDS = "E-merge-held-work-proceeds"
BLOCKS_RESOLVED_UNDER_EXECUTE = "F-needs-human-blocks-resolved"
BLOCK_RESOLUTIONS_PROPOSED = "G-needs-human-block-resolutions-proposed"
RULING_BINDS_REWORK_AND_REVIEW = "I-ruling-binds-rework-and-review"
DECISION_STEPS_RUN_ON_APPROVAL = "J-decision-steps-run-on-approval"
INTEGRATION_BRANCH_LANDS_APPROVED_WORK = "K-integration-branch-lands-approved-work"

#: Every case id the exam defines. A new case (the improver's ``exam_case``
#: output, #7490) must use an id outside this set: cases are add-only.
EXAM_CASE_IDS: tuple[str, ...] = (
    HALTED_EXCHANGE_WITH_VALIDATED_WORK,
    BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
    STALE_CLAIM_PAUSED_FOR_RECONCILE,
    UPGRADE_WITH_WORK_IN_FLIGHT,
    BLOCKED_ITEMS_TRIAGED,
    POSITIVE_APPROVAL_EXECUTES_ONCE,
    MERGE_HELD_WORK_PROCEEDS,
    BLOCKS_RESOLVED_UNDER_EXECUTE,
    BLOCK_RESOLUTIONS_PROPOSED,
    RULING_BINDS_REWORK_AND_REVIEW,
    DECISION_STEPS_RUN_ON_APPROVAL,
    INTEGRATION_BRANCH_LANDS_APPROVED_WORK,
)

#: Case U's two in-flight items.
CODING = "coding"
REVIEW = "review"

#: A coding agent's pre-work question with no PR (porchpin#262): Cases D and E.
ASKS = "asks"
#: A decision a person makes before a published PR merges (#364/#379): Case E.
ASKS_BESIDE_PR = "asks_beside_pr"

#: Cases F and G's four needs-human blocks, each a porchpin item on 2026-10-02:
#: an agent's split question (porchpin#262), a block the engine gave up on
#: beside a stale blocked-cross-milestone (#326), an agent question beside its
#: published PR that the issue's own spec answers (#364/#379), and account
#: provisioning only a human can do (#179).
SPLIT = "split"
STALE = "stale"
BESIDE_PR = "beside_pr"
PROVISIONING = "provisioning"

#: Case H's gated tech-lead proposals (#7763): one a maintainer approves, one
#: whose waiting label is stripped, and several a bot "approves" (#8346).
MAINTAINER_APPROVED = "maintainer_approved"
STRIPPED = "stripped"
BOT_APPROVED = "bot_approved"
#: Bot approvals racing the filing: GitHub then attributes the bot's
#: ``approved`` to the maintainer who filed the issue too (#8346). The race is
#: lost about one time in three, so the case runs it five times.
BOT_RACED_ROLES = (BOT_APPROVED, "bot_approved_2", "bot_approved_3", "bot_approved_4", "bot_approved_5")
#: A maintainer approves, takes it back, and a bot re-applies ``approved``.
BOT_REAPPLIED = "bot_reapplied"
#: Every case H proposal only a bot "approved": none may ever be worked.
BOT_APPROVED_ROLES = (*BOT_RACED_ROLES, BOT_REAPPLIED)

#: Case I's two items (#8141): a PR a maintainer ruled on, whose conflict
#: rework and review must be bound by the ruling (porchpin#364/PR #379), and a
#: pre-work question the issue's spec answers, whose approved resolve_block
#: answer must land in the issue BODY (porchpin#327/#501).
RULED = "ruled"
ANSWERED = "answered"

#: Case J's four items (#8691), porchpin#459's shape: the item the decision is
#: about (#326), a sibling it moves to another milestone (#327), the parent
#: whose body gets a note (#262), and the older proposal it supersedes (#445).
DECIDED = "decided"
SIBLING = "sibling"
NOTED = "noted"
SUPERSEDED = "superseded"
#: The milestone Case J's decision moves the sibling to (#459's "M1 - Surfaces").
DECISION_MILESTONE = "EXAM-J M1"
#: What only the operator can do in Case J (#456's CI ceiling): a workflow edit.
OPERATOR_ONLY_TERM = "ci.yml"
#: The notes Case J's spec tells the decision to record, word for word (#459's
#: delivery plan in #326's body, and the line under #262's acceptance bullet).
DELIVERY_PLAN_NOTE = "Delivery plan: build slice A (the per-pickup deletion command) only; slice B waits."
PARENT_NOTE = "The deletion fence that meets this bullet is built by the route issue split from the decided issue."

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
            # A filed resolve_block proposal (#7658) puts the same decision
            # to the operator, so it answers the split question too.
            item_triaged(ASKS, ("operator_decision",), or_resolution_proposed=True),
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


def _resolution_case_goals(*, needs_human_label: str, proposals: bool) -> tuple[Goal, ...]:
    """What cases F (``proposals=False``) and G (``proposals=True``) demand."""
    if proposals:
        # The decision waits for the operator: a filed, approvable proposal
        # carrying it, and the block stays until the operator approves.
        decided = tuple(
            goal
            for role in (SPLIT, STALE)
            for goal in (
                item_resolved(role, ("awaiting_approval",)),
                issue_keeps_labels(role, (needs_human_label,)),
            )
        )
        progress: tuple[Goal, ...] = ()
    else:
        # Decided, cleared and moving: each item's next session published
        # its work (a narrowed split, a lifted block).
        decided = tuple(
            goal
            for role in (SPLIT, STALE)
            for goal in (
                item_resolved(role, ("applied",)),
                issue_lacks_labels(role, (needs_human_label,)),
            )
        )
        progress = tuple(
            goal
            for role in (SPLIT, STALE)
            for goal in (single_pull_request(role), published_work_survives(role))
        )
    return (
        *decided,
        *progress,
        # porchpin#364: a question beside published work is a MERGE hold on
        # its PR (#7678), not a work block. Its work proceeds under either
        # setting (the issue is never held, the PR is reviewed) and no
        # resolution touches the merge: the operator merges, by design.
        issue_lacks_labels(BESIDE_PR, (needs_human_label,)),
        single_pull_request(BESIDE_PR),
        pr_in_state(BESIDE_PR, PullRequestState.DRAFT, PullRequestState.READY),
        pr_has_label(BESIDE_PR, needs_human_label),
        no_tech_lead_decision(BESIDE_PR, "resolve_block"),
        pr_review_approved(BESIDE_PR),
        published_work_survives(BESIDE_PR),
        # Account provisioning is a person's work under every setting.
        item_handed_over(PROVISIONING),
        no_tech_lead_decision(PROVISIONING, "resolve_block"),
        issue_keeps_labels(PROVISIONING, (needs_human_label,)),
    )


def needs_human_blocks_resolved(*, needs_human_label: str) -> ExamCase:
    """Case F — porchpin's needs-human blocks, with ``resolve_block: execute`` (#7658).

    The operator wants to do less: ``tech_lead.authority.resolve_block`` is
    ``execute``. Right answer: a health review decides the three work blocks
    itself and they end cleared and moving (the split question is decided and
    the item requeued, the stale block lifted); the question beside a
    published PR is a merge hold (#7678) its review proceeds under and no
    resolution touches; the provisioning item is still handed over, because
    human-only work is never resolvable.
    """
    return ExamCase(
        case_id=BLOCKS_RESOLVED_UNDER_EXECUTE,
        title="Needs-human work blocks the tech lead may decide itself",
        fault=(
            f"four items carry {needs_human_label}: a split question, a block the engine"
            " gave up on beside a stale blocked-cross-milestone, a question beside a"
            " published PR, and account provisioning"
        ),
        goals=_resolution_case_goals(needs_human_label=needs_human_label, proposals=False),
        known_blockers=(
            "#7658 no tech-lead action could clear a needs-human block",
            "porchpin#262/#326/#364 stayed blocked whatever the config said",
        ),
    )


def needs_human_block_resolutions_proposed(*, needs_human_label: str) -> ExamCase:
    """Case G — the same blocks under the default ``resolve_block: propose`` (#7658).

    Right answer: each work block ends with an approvable ``resolve_block``
    proposal carrying the decision, the block still in place until the
    operator approves it, and the provisioning item handed over.
    """
    return ExamCase(
        case_id=BLOCK_RESOLUTIONS_PROPOSED,
        title="Needs-human work blocks resolved as approvable proposals",
        fault=(
            f"the same four {needs_human_label} items as case F, with the default"
            " resolve_block authority (propose)"
        ),
        goals=_resolution_case_goals(needs_human_label=needs_human_label, proposals=True),
        known_blockers=("#7658 no tech-lead action could clear a needs-human block",),
    )


def positive_approval_executes_once(
    *,
    proposal_label: str,
    awaiting_label: str,
    approved_label: str,
) -> ExamCase:
    """Case H — approval is a positive, maintainer-applied act (#7763).

    Gated tech-lead follow-ups (the ``create_issue`` proposals a
    ``propose``-authority tech lead files) are planted before the engine
    starts. A maintainer adds ``approved`` to the first. The second loses its
    ``awaiting-approval`` label the way an engine retry used to strip "every
    blocking label" (porchpin#444) — under the old model that removal WAS the
    approval. Five more get ``approved`` from a GitHub App (bot) identity
    the moment they are filed, racing GitHub's late attribution of a filing's
    labels to its maintainer author (#8346), and one last is approved by the
    maintainer, un-approved, and re-approved by the bot.

    Right answer: the maintainer's proposal is admitted and worked exactly
    once; the stripped one is never worked and gets its waiting label back;
    every bot ``approved`` is removed and none of those is ever worked.
    """
    gated = (proposal_label, awaiting_label)
    return ExamCase(
        case_id=POSITIVE_APPROVAL_EXECUTES_ONCE,
        title="Only a maintainer's positive approval executes a tech-lead proposal",
        fault=(
            "gated tech-lead proposals: one approved by a maintainer, one with its"
            " waiting label stripped, five 'approved' by a bot as they are filed,"
            " one re-'approved' by a bot after the maintainer took it back"
        ),
        goals=(
            single_pull_request(MAINTAINER_APPROVED),
            issue_lacks_labels(MAINTAINER_APPROVED, (awaiting_label,)),
            issue_keeps_labels(MAINTAINER_APPROVED, (proposal_label, approved_label)),
            never_worked(STRIPPED),
            issue_keeps_labels(STRIPPED, gated),
            issue_lacks_labels(STRIPPED, (approved_label,)),
            *(
                goal
                for role in BOT_APPROVED_ROLES
                for goal in (
                    never_worked(role),
                    issue_keeps_labels(role, gated),
                    issue_lacks_labels(role, (approved_label,)),
                )
            ),
        ),
        known_blockers=(
            "#7763 approval was the REMOVAL of proposed-tech-lead: any strip approved",
        ),
    )


def ruling_binds_rework_and_review() -> ExamCase:
    """Case I — a maintainer ruling binds every agent on the issue (#8141).

    ``ruled`` (porchpin#364/PR #379): an approved PR that conflicts with its
    base; a maintainer records a ruling that retires what the next rework
    would extend (``exam-output.txt``). The conflict rework fires, and its
    scripted coder extends exactly that; its scripted reviewer approves
    whatever it sees, as #379's did. Right answer: every rework prompt
    carries the ruling (the first as its BRIEF), every later review prompt
    carries it, and the approval of the contradicting diff is refused and
    flagged implementation-required, never accepted.

    ``answered`` (porchpin#327/proposal #501): a coder asks a pre-work
    question its issue's spec answers; the tech lead's ``resolve_block``
    (``execute``) answers it. Right answer: the answer is a standing ruling in
    the issue BODY (sessions read the body, not comments).
    """
    return ExamCase(
        case_id=RULING_BINDS_REWORK_AND_REVIEW,
        title="A standing ruling binds the conflict rework and its review",
        fault=(
            "a maintainer rules on an approved PR that conflicts with its base; its conflict"
            " rework extends what the ruling retires and the reviewer approves it; and an"
            " approved resolve_block answer stays in a comment"
        ),
        goals=(
            rulings_in_body(RULED),
            rework_prompts_carry_rulings(RULED),
            reviews_after_rework_carry_rulings(RULED),
            contradicting_approval_refused(RULED),
            resolution_answer_in_body(ANSWERED, authority=RulingAuthority.APPROVED_RESOLUTION.value),
        ),
        known_blockers=(
            "porchpin#379 a conflict rework and its review never saw the maintainer's ruling",
            "porchpin#327 an approved resolve_block answer never reached the issue body",
        ),
    )


#: The authority a decision step's ruling is recorded with.
APPROVED_DECISION = RulingAuthority.APPROVED_DECISION.value


def decision_steps_run_on_approval(*, needs_human_label: str) -> ExamCase:
    """Case J — an approved decision carries out its own consequences (#8691).

    porchpin#459: the tech lead's split decision for #326 came with "Before
    you approve" chores (move #326 and #327 to M1, paste the delivery plan into
    #326's body, add a note to #262's body) and #456 closed the proposal it
    superseded; the operator carried out each by hand. Here a coder asks a
    question whose answer, per its issue's spec, also moves a sibling to
    another milestone, notes the parent's body, closes a superseded proposal,
    and needs a workflow edit only a person can push. A health review decides
    it, the harness approves the proposal as a maintainer, and does nothing
    else. Right answer: the decision carries typed steps (no "Before you
    approve" prose), the workflow edit is the operator's checklist item, and
    approval runs every step exactly once: the sibling is in the milestone,
    the parent's and the item's bodies carry the notes as standing rulings,
    the superseded proposal is closed, and the item is released.
    """
    return ExamCase(
        case_id=DECISION_STEPS_RUN_ON_APPROVAL,
        title="Approving a decision carries out its steps beyond the item",
        fault=(
            "an agent's question whose decision also moves a sibling to another milestone,"
            " notes two issue bodies, closes a superseded proposal and needs a workflow edit;"
            " the maintainer only approves"
        ),
        goals=(
            decision_steps_ran_once(DECIDED, at_least=3),
            operator_checklist_names(DECIDED, OPERATOR_ONLY_TERM),
            no_hand_steps_in_prose(DECIDED),
            body_ruling_states(DECIDED, DELIVERY_PLAN_NOTE, authority=APPROVED_DECISION),
            issue_lacks_labels(DECIDED, (needs_human_label,)),
            issue_in_milestone(SIBLING, DECISION_MILESTONE),
            body_ruling_states(NOTED, PARENT_NOTE, authority=APPROVED_DECISION),
            issue_is_closed(SUPERSEDED),
        ),
        known_blockers=(
            "porchpin#459 approval acted on the issue only: every other consequence was the operator's by hand",
            "porchpin#327/#530 approval could not route the decided issue's PR",
        ),
    )


#: Case K's three items (#8144): the first two to be published, and the sibling
#: created last, whose PR the first merge into integration leaves behind.
LANDED_FIRST = "landed_first"
LANDED_SECOND = "landed_second"
LEFT_BEHIND = "left_behind"


def integration_branch_lands_approved_work(
    *, needs_human_label: str, rework_labels: tuple[str, ...]
) -> ExamCase:
    """Case K — integration-branch mode lands approved work (#8144).

    Three coders publish non-conflicting PRs into an integration branch io
    creates itself; the scripted reviewer approves each, and the harness then
    releases all three at once (the ``merge_after: tech-lead-reviewed`` label).
    io merges one per pass, oldest first, so the first merge leaves the others
    behind: each must be brought up to the tip mechanically, never by an agent
    rework, and merge. GitHub closes no issue on a non-default-branch merge:
    io's awaiting-merge reconcile must close each one. The delivery PR
    (integration -> main) lists all three.

    Before #8144 porchpin's operator merged every PR, and every merge sent the
    siblings to rework (PR #379: 9 of 10 cycles, mostly rebases).
    """
    goals: list[Goal] = []
    for role in (LANDED_FIRST, LANDED_SECOND, LEFT_BEHIND):
        goals.extend((
            single_pull_request(role),
            pr_in_state(role, PullRequestState.MERGED),
            issue_is_closed(role),
            issue_lacks_labels(role, (needs_human_label,)),
            no_rework(role, rework_labels),
        ))
    goals.append(integration_steps_applied(LEFT_BEHIND, at_least=2))
    goals.append(delivery_lists_every_pr((LANDED_FIRST, LANDED_SECOND, LEFT_BEHIND)))
    return ExamCase(
        case_id=INTEGRATION_BRANCH_LANDS_APPROVED_WORK,
        title="Integration mode lands approved PRs; a left-behind sibling is updated, not reworked",
        fault=(
            "three approved PRs into an integration branch io must create; each merge"
            " leaves the remaining PRs behind its new tip"
        ),
        goals=tuple(goals),
        known_blockers=("porchpin#379 every merge sent the siblings to rework", "#8144"),
    )
