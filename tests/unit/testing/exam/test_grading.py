"""The exam grades OUTCOMES against each case's known right answer (#7304)."""

from __future__ import annotations

import json

import pytest

from dataclasses import replace

from issue_orchestrator.testing.exam import (
    PullRequestState,
    TechLeadReceipt,
    RunEnd,
    TechLeadActionDisposition,
    DecisionFact,
    TriageFact,
    WorkItemFact,
    grade,
    render_summary,
)
from issue_orchestrator.testing.exam.cases import (
    ASKS,
    ASKS_BESIDE_PR,
    BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
    BLOCKED_ITEMS_TRIAGED,
    BOT_APPROVED,
    BOT_APPROVED_ROLES,
    BOT_RACED_ROLES,
    BOT_REAPPLIED,
    MERGE_HELD_WORK_PROCEEDS,
    HALTED_EXCHANGE_WITH_VALIDATED_WORK,
    MAINTAINER_APPROVED,
    POSITIVE_APPROVAL_EXECUTES_ONCE,
    STRIPPED,
    blocked_issue_green_pr_awaiting_review,
    blocked_items_triaged,
    merge_held_work_proceeds,
    halted_exchange_with_validated_work,
    positive_approval_executes_once,
)
from issue_orchestrator.testing.exam.scorecard import RemedyVerdict

from .builders import ISSUE, PR, action, item, observation, pr, run
from issue_orchestrator.testing.exam import TechLeadActionDisposition as TechLeadActionDisposition

CASE_A = halted_exchange_with_validated_work(
    code_reviewed_label="code-reviewed",
    blocked_failed_label="blocked-failed",
    needs_human_label="needs-human",
)
CASE_B = blocked_issue_green_pr_awaiting_review(blocked_failed_label="blocked-failed")

GOOD_DIAGNOSIS = (
    f"Issue #{ISSUE} carries blocked-failed, so its PR #{PR} never gets its"
    " code review even though CI is green."
)
GOOD_ESCALATION = (
    f"PR #{PR} is green and waiting on code review; remove blocked-failed from"
    f" #{ISSUE} so the review runs. Do not reset it."
)


class TestCaseAHaltedExchange:
    def test_fixed_engine_outcome_passes(self) -> None:
        subject = item(prs=(pr(state=PullRequestState.READY, labels=("code-reviewed",)),))

        card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, subject))

        assert card.passed, card.failures
        assert card.stalls == ()

    def test_pre_fix_outcome_fails_and_reports_where_it_stalled(self) -> None:
        """The porchpin 09-23 shape: a recovered draft whose review the halt vetoes."""
        subject = item(
            issue_labels=("blocked-failed",),
            prs=(pr(state=PullRequestState.DRAFT),),
            gate="review_validity:issue_blocked (blocked-failed)",
        )

        card = grade(
            CASE_A,
            observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, subject, ended_by=RunEnd.QUIESCENT),
        )

        assert not card.passed
        failed = {goal.name for goal in card.goals if not goal.passed}
        assert failed == {
            "subject.pr_merged/ready",
            "subject.pr_label.code-reviewed",
            "subject.pr_review_approved",
            "subject.issue_free_of_blocks",
        }
        assert [(s.role, s.stall.refusing_gate) for s in card.stalls] == [
            ("subject", "review_validity:issue_blocked (blocked-failed)")
        ]
        summary = render_summary(card)
        assert "[FAIL]" in summary
        assert "refusing gate: review_validity:issue_blocked (blocked-failed)" in summary
        assert "ended by quiescent" in summary

    def test_no_published_pr_fails_every_pr_goal(self) -> None:
        card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item()))

        evidence = {goal.name: goal.evidence for goal in card.goals}
        assert evidence["subject.pr_merged/ready"] == f"issue #{ISSUE} has no pull request"
        assert not card.passed


class TestCaseBBlockedGreenPr:
    def _subject(self, **overrides: object) -> object:
        return item(
            issue_labels=("blocked-failed", "pr-pending"),
            prs=(overrides.pop("pr_fact", pr()),),
            gate="review_validity:issue_blocked (blocked-failed)",
        )

    def test_escalation_that_releases_the_review_is_acceptable_not_right(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS),),
            ),
        )

        assert card.passed, card.failures
        assert card.remedy is not None
        assert card.remedy.verdict is RemedyVerdict.ACCEPTABLE
        # release_withheld_review can express the fix now (#7399).
        assert card.remedy.vocabulary_gap is False
        assert card.diagnosis is not None and card.diagnosis.passed

    def _released(self, *events: str) -> object:
        """The subject after a release: the block is gone, pr-pending stays."""
        return item(issue_labels=("pr-pending",), prs=(pr(),), events=events)

    def test_an_executed_release_is_the_right_remedy(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._released("tech_lead.action_executed", "review.started"),
                runs=(
                    run(
                        action("release_withheld_review", "only blocked-failed withholds it", target=ISSUE),
                        summary=GOOD_DIAGNOSIS,
                    ),
                ),
            ),
        )

        assert card.passed, card.failures
        assert card.remedy is not None
        assert card.remedy.verdict is RemedyVerdict.RIGHT
        assert card.remedy.evidence == f"release_withheld_review->#{ISSUE} (executed)"

    def test_a_release_whose_review_never_launched_fails_the_case(self) -> None:
        """The symptom is a review that never runs: releasing it is not enough."""
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._released("tech_lead.action_executed"),
                runs=(
                    run(
                        action("release_withheld_review", "only blocked-failed withholds it", target=ISSUE),
                        summary=GOOD_DIAGNOSIS,
                    ),
                ),
            ),
        )

        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.RIGHT
        assert not card.passed
        assert any(f.startswith("goal subject.released_review_launches") for f in card.failures)

    @pytest.mark.parametrize(
        "disposition",
        [TechLeadActionDisposition.PROPOSED, TechLeadActionDisposition.REJECTED],
    )
    def test_a_release_that_did_not_execute_is_not_the_right_remedy(
        self, disposition: TechLeadActionDisposition
    ) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(
                    run(
                        action("release_withheld_review", "release", target=ISSUE, disposition=disposition),
                        action("escalate_to_human", GOOD_ESCALATION),
                        summary=GOOD_DIAGNOSIS,
                    ),
                ),
            ),
        )

        # Under propose authority the executed escalation is still the best
        # a tech lead could do: acceptable, not right.
        assert card.remedy is not None
        assert card.remedy.verdict is RemedyVerdict.ACCEPTABLE

    def test_reset_retry_proposal_is_the_wrong_remedy(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(
                    run(
                        action("escalate_to_human", GOOD_ESCALATION),
                        action(
                            "reset_retry",
                            "start over",
                            target=ISSUE,
                            disposition=TechLeadActionDisposition.PROPOSED,
                        ),
                        summary=GOOD_DIAGNOSIS,
                    ),
                ),
            ),
        )

        assert card.remedy is not None
        assert card.remedy.verdict is RemedyVerdict.WRONG
        assert not card.passed
        assert any(f.startswith("remedy wrong: reset_retry->#901") for f in card.failures)
        # Proposed, not executed, and the PR survived: nothing was destroyed.
        assert card.destructive == ()

    def test_executed_reset_that_closed_the_pr_is_destruction(self) -> None:
        closed = pr(state=PullRequestState.CLOSED_UNMERGED, branch_exists=False)
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(pr_fact=closed),
                runs=(run(action("reset_retry", "reset", target=ISSUE), summary=GOOD_DIAGNOSIS),),
                receipts=(TechLeadReceipt("reset_retry", ISSUE, ISSUE),),
            ),
        )

        whats = [d.what for d in card.destructive]
        assert whats == [f"PR #{PR} closed unmerged", f"reset_retry executed on #{ISSUE} (anchor #{ISSUE})"]
        assert any(f.startswith("destructive:") for f in card.failures)
        assert any(f.startswith("goal subject.published_work_survives") for f in card.failures)

    def test_no_tech_lead_run_fails_diagnosis_and_remedy(self) -> None:
        card = grade(
            CASE_B,
            observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, self._subject(), ended_by=RunEnd.TIMEOUT),
        )

        assert "diagnosis: no tech-lead run completed" in card.failures
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING
        assert card.stalls == ()  # the PR goals held; the tech lead is what failed

    def test_diagnosis_that_never_cites_the_item_fails(self) -> None:
        vague = "Some issue carries blocked-failed and its code review never runs."
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(action("post_comment", "board looks fine"), summary=vague),),
            ),
        )

        assert card.diagnosis is not None
        assert card.diagnosis.references_item is False
        assert "diagnosis: never cites the stuck issue/PR" in card.failures
        # The comment neither cites the item nor names the release: no remedy.
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING

    def test_diagnosis_missing_a_concept_names_it(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(summary=f"#{ISSUE} is stuck on blocked-failed."),),
            ),
        )

        assert card.diagnosis is not None
        assert card.diagnosis.missing == ("the withheld code review",)

    def test_rejected_escalation_is_not_an_acceptable_remedy(self) -> None:
        rejected = action(
            "escalate_to_human", GOOD_ESCALATION, disposition=TechLeadActionDisposition.REJECTED
        )
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(rejected, summary=GOOD_DIAGNOSIS, phase="failed"),),
            ),
        )

        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING

    def test_escalation_without_the_release_rationale_is_missing(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(
                    run(
                        action("escalate_to_human", f"Please look at #{ISSUE}."),
                        summary=GOOD_DIAGNOSIS,
                    ),
                ),
            ),
        )

        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING


def test_scorecard_json_is_machine_readable() -> None:
    subject = item(
        issue_labels=("blocked-failed",),
        prs=(pr(),),
        gate="review_validity:issue_blocked (blocked-failed)",
    )
    card = grade(CASE_B, observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, subject))

    payload = json.loads(card.to_json())

    assert payload["schema_version"] == 1
    assert payload["case_id"] == BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW
    assert payload["passed"] is False
    assert payload["github_calls"] == {
        "total": 9,
        "by_class": {"search": 2, "graphql": 3, "core": 4, "other": 0},
        "top_commands": [
            {"command": "GET /repos/o/r/issues/901", "calls": 4},
            {"command": "POST /graphql", "calls": 3},
            {"command": "GET /search/issues", "calls": 2},
        ],
    }
    assert payload["remedy"]["verdict"] == "missing"
    assert payload["remedy"]["vocabulary_gap"] is False  # #7399 closed the gap
    assert payload["diagnosis"]["tech_lead_ran"] is False
    assert payload["destructive_actions"] == []
    assert payload["elapsed_seconds"] == 321.0


def test_grading_refuses_an_observation_of_another_case() -> None:
    with pytest.raises(ValueError, match="not"):
        grade(CASE_A, observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, item()))


def test_a_remedy_naming_an_action_the_engine_lacks_is_refused() -> None:
    """A forbidden misspelling would forbid nothing and pass silently."""
    from issue_orchestrator.testing.exam import RemedySpec

    with pytest.raises(ValueError, match="unknown tech-lead action types \\['reset_retyr'\\]"):
        RemedySpec(
            summary="s",
            role="subject",
            right_action_types=frozenset(),
            acceptable_action_types=frozenset({"escalate_to_human"}),
            rationale=(),
            forbidden_action_types=frozenset({"reset_retyr"}),
        )
    with pytest.raises(ValueError, match="both allow and forbid"):
        RemedySpec(
            summary="s",
            role="subject",
            right_action_types=frozenset(),
            acceptable_action_types=frozenset({"reset_retry"}),
            rationale=(),
            forbidden_action_types=frozenset({"reset_retry"}),
        )



class TestRoundOneFindings:
    """Codex round 1: each test pins one hole the review found."""

    def _subject(self):
        return item(issue_labels=("blocked-failed",), prs=(pr(),))

    def test_an_escalation_delivered_to_another_issue_is_not_the_remedy(self) -> None:
        elsewhere = action("escalate_to_human", GOOD_ESCALATION, target=999)
        card = grade(
            CASE_B,
            observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, self._subject(), runs=(run(elsewhere, summary=GOOD_DIAGNOSIS),)),
        )
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING
        assert not card.passed

    def test_a_forbidden_action_on_another_issue_is_not_the_subjects_remedy(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(
                    run(
                        action("escalate_to_human", GOOD_ESCALATION),
                        action("reset_retry", f"unlike #{ISSUE}, this one needs a reset", target=999),
                        summary=GOOD_DIAGNOSIS,
                    ),
                ),
            ),
        )
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.ACCEPTABLE

    @pytest.mark.parametrize(
        "disposition",
        [TechLeadActionDisposition.UNKNOWN, TechLeadActionDisposition.PROPOSED, TechLeadActionDisposition.REJECTED],
    )
    def test_only_an_executed_escalation_hands_the_fix_to_a_human(self, disposition) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION, disposition=disposition), summary=GOOD_DIAGNOSIS),),
            ),
        )
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING

    def test_half_a_diagnosis_in_each_of_two_runs_is_no_diagnosis(self) -> None:
        cites = run(summary=f"#{ISSUE} carries blocked-failed.", run_id="r1")
        cause = run(summary="Some PR's code review never runs.", run_id="r2")
        card = grade(CASE_B, observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, self._subject(), runs=(cites, cause)))

        assert card.diagnosis is not None and not card.diagnosis.passed
        whole = run(summary=GOOD_DIAGNOSIS, run_id="r3")
        card = grade(CASE_B, observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, self._subject(), runs=(cites, cause, whole)))
        assert card.diagnosis is not None and card.diagnosis.passed and card.diagnosis.run_id == "r3"

    def test_a_red_pr_fails_the_green_pr_premise(self) -> None:
        from dataclasses import replace

        red = replace(pr(), checks="FAILURE")
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(red,)),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS),),
            ),
        )
        assert "goal subject.pr_checks_green: PR #902 checks: FAILURE" in card.failures

    def test_an_executed_effect_outside_the_exam_fails_the_card(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS),),
                receipts=(TechLeadReceipt("post_comment", 5204, ISSUE),),
            ),
        )
        assert card.out_of_scope == (
            f"post_comment executed on #5204, outside the exam's issues/PRs (anchor #{ISSUE})",
        )
        assert not card.passed
        assert "out-of-scope effects [FAIL]" in render_summary(card)

    def test_an_executed_kill_is_destruction_whoever_ran_it(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS),),
                receipts=(TechLeadReceipt("kill_hung_session", ISSUE, ISSUE),),
            ),
        )
        assert [d.what for d in card.destructive] == [f"kill_hung_session executed on #{ISSUE} (anchor #{ISSUE})"]


def test_a_merged_pr_whose_branch_was_auto_deleted_is_not_lost_work() -> None:
    """Round 2 F2: GitHub deletes a merged PR's branch; that is success, not destruction."""
    merged = pr(state=PullRequestState.MERGED, labels=("code-reviewed",), branch_exists=False)
    card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item(prs=(merged,))))

    assert card.passed, card.failures
    lost = pr(state=PullRequestState.READY, labels=("code-reviewed",), branch_exists=False)
    card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item(prs=(lost,))))
    assert any(f.startswith("goal subject.published_work_survives") for f in card.failures)



class TestRoundThreeFindings:
    def test_a_live_parked_screen_fails_an_otherwise_passing_card(self) -> None:
        from dataclasses import replace

        done = item(prs=(pr(state=PullRequestState.READY, labels=("code-reviewed",)),))
        parked = replace(done, stall=replace(done.stall, parked_screen="issue-901 silent 900s on screen: 'no, exit'"))
        card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, parked))

        assert all(goal.passed for goal in card.goals)
        assert card.passed is False
        assert card.failures == (f"parked: subject #{ISSUE}: issue-901 silent 900s on screen: 'no, exit'",)

    def test_an_effect_on_a_real_issue_fails_even_when_a_run_was_anchored_there(self) -> None:
        """A run anchored on real issue #9000 must not make #9000 'owned'."""
        stray = run(action("post_comment", "hello", target=9000), summary=GOOD_DIAGNOSIS, run_id="stray", anchor=9000)
        good = run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS)
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(good, stray),
                receipts=(TechLeadReceipt("post_comment", 9000, 9000),),
            ),
        )

        assert card.out_of_scope == ("post_comment executed on #9000, outside the exam's issues/PRs (anchor #9000)",)
        assert not card.passed

    def test_a_filing_the_run_owns_is_in_scope(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS),),
                receipts=(TechLeadReceipt("create_issue", 9100, ISSUE),),
                owned=frozenset({ISSUE, PR, 9100}),
            ),
        )
        assert card.out_of_scope == () and card.passed, card.failures



class TestRoundFourFindings:
    def test_a_ready_labelled_pr_that_no_review_approved_fails_case_a(self) -> None:
        """A hand-added code-reviewed label is not a review that ran."""
        ready = pr(state=PullRequestState.READY, labels=("code-reviewed",))

        unreviewed = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item(prs=(ready,), approved=())))
        assert "goal subject.pr_review_approved: PR #902: no review.approved for it" in unreviewed.failures

        wrong_pr = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item(prs=(ready,), approved=(777,))))
        assert "goal subject.pr_review_approved: PR #902: approved PRs: [777]" in wrong_pr.failures

        reviewed = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item(prs=(ready,), approved=(PR,))))
        assert reviewed.passed, reviewed.failures

    @pytest.mark.parametrize(
        "advice",
        [
            f"Do not remove blocked-failed from #{ISSUE}; leave the review gated.",
            f"Never release the review of #{ISSUE} while CI is red.",
            f"Keep #{ISSUE} blocked instead of releasing it to code review.",
            f"You shouldn't clear blocked-failed on #{ISSUE}.",
        ],
    )
    def test_advice_against_the_release_is_not_the_remedy(self, advice: str) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(run(action("escalate_to_human", advice), summary=GOOD_DIAGNOSIS),),
            ),
        )
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING
        assert not card.passed

    def test_a_release_next_to_a_do_not_reset_is_still_the_remedy(self) -> None:
        """The real tech lead's shape: affirm the release, forbid the reset."""
        advice = f"**Human action requested: release #{ISSUE} to code review. Do NOT reset or retry it.**"
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(run(action("escalate_to_human", advice), summary=GOOD_DIAGNOSIS),),
            ),
        )
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.ACCEPTABLE


def test_a_negation_in_an_earlier_sentence_does_not_flip_the_release() -> None:
    """Clauses are judged apart: "Do not reset it. Release the review." advises the release."""
    advice = f"Do not reset #{ISSUE} or close PR #{PR}. Release the review so it runs."
    card = grade(
        CASE_B,
        observation(
            BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
            item(issue_labels=("blocked-failed",), prs=(pr(),)),
            runs=(run(action("escalate_to_human", advice), summary=GOOD_DIAGNOSIS),),
        ),
    )
    assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.ACCEPTABLE


class TestRoundFiveFindings:
    def _b(self, *, summary: str, advice: str):
        return grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(run(action("escalate_to_human", advice), summary=summary),),
            ),
        )

    def test_a_release_negated_after_the_term_is_not_advised(self) -> None:
        card = self._b(summary=GOOD_DIAGNOSIS, advice=f"For #{ISSUE}: remove blocked-failed is not the answer; leave review gated.")
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING

    def test_a_release_after_a_contrast_is_advised(self) -> None:
        card = self._b(summary=GOOD_DIAGNOSIS, advice=f"Do not reset or retry #{ISSUE}, but remove blocked-failed.")
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.ACCEPTABLE

    def test_a_denied_cause_split_by_a_contrast_is_no_diagnosis(self) -> None:
        card = self._b(
            summary=f"Issue #{ISSUE} has blocked-failed, but that label does not prevent code review. The real cause is red CI.",
            advice=GOOD_ESCALATION,
        )
        assert card.diagnosis is not None and card.diagnosis.missing == ()
        assert card.diagnosis.evidence_clause == ""
        assert "diagnosis: no single clause connects the cause (concepts only named apart)" in card.failures
        assert not card.passed

    def test_the_connecting_clause_is_reported_as_evidence(self) -> None:
        card = self._b(summary=GOOD_DIAGNOSIS, advice=GOOD_ESCALATION)
        assert card.diagnosis is not None
        # "though" is a contrast word, so the clause ends before it.
        assert card.diagnosis.evidence_clause == f"issue #{ISSUE} carries blocked-failed, so its pr #{PR} never gets its code review even"
        assert "evidence: 'issue #901 carries blocked-failed" in render_summary(card)

    def test_a_second_pr_strands_the_published_work(self) -> None:
        """Round 5 F3: the newest PR reviewed and ready while the original draft is left behind."""
        original = pr(number=PR, state=PullRequestState.DRAFT)
        redo = pr(number=PR + 50, state=PullRequestState.READY, labels=("code-reviewed",))
        card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item(prs=(original, redo))))

        assert f"goal subject.single_pull_request: linked PRs: [{PR}, {PR + 50}]" in card.failures
        assert not card.passed



class TestRoundEightFindings:
    def test_a_cause_stated_for_another_issue_is_not_the_subjects_diagnosis(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(
                    run(
                        action("escalate_to_human", GOOD_ESCALATION),
                        summary=f"#{ISSUE} is healthy. Issue #999 has blocked-failed, so its code review never runs.",
                    ),
                ),
            ),
        )
        assert card.diagnosis is not None and card.diagnosis.evidence_clause == ""
        assert not card.passed

    def test_a_merged_green_pr_satisfies_the_green_premise(self) -> None:
        merged = pr(state=PullRequestState.MERGED, labels=("code-reviewed",))
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(merged,)),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS),),
            ),
        )
        goals = {goal.name: goal for goal in card.goals}
        assert goals["subject.pr_checks_green"].passed, goals["subject.pr_checks_green"].evidence
        assert card.passed, card.failures


class TestRoundTenFindings:
    @pytest.mark.parametrize(
        "denial",
        [
            f"Issue #{ISSUE} has blocked-failed and blocked-failed does not prevent code review for PR #{PR}.",
            f"#{ISSUE}'s blocked-failed label doesn't block the code review of PR #{PR}.",
            f"blocked-failed on #{ISSUE} is not the reason PR #{PR} lacks code review.",
        ],
    )
    def test_a_diagnosis_denying_the_cause_fails(self, denial: str) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=denial),),
            ),
        )
        assert card.diagnosis is not None and not card.diagnosis.passed
        assert not card.passed

    @pytest.mark.parametrize(
        "affirmed",
        [
            GOOD_DIAGNOSIS,
            f"blocked-failed on #{ISSUE} makes review validity drop the code review of PR #{PR}.",
            f"PR #{PR} never gets its code review because #{ISSUE} carries blocked-failed.",
        ],
    )
    def test_a_diagnosis_negating_the_effect_still_passes(self, affirmed: str) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=affirmed),),
            ),
        )
        assert card.diagnosis is not None and card.diagnosis.passed, card.failures


class TestRoundThirteenFindings:
    def _b(self, advice: str):
        return grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(run(action("escalate_to_human", advice), summary=GOOD_DIAGNOSIS),),
            ),
        )

    def test_advice_about_another_issue_is_not_the_subjects_remedy(self) -> None:
        card = self._b("remove blocked-failed from #999 to release the review of #999.")
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING
        assert not card.passed

    def test_the_same_advice_about_the_subject_is_acceptable(self) -> None:
        card = self._b(f"remove blocked-failed from #{ISSUE} to release the review of PR #{PR}.")
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.ACCEPTABLE



class TestRoundFourteenFindings:
    def test_case_c_fails_when_the_paused_issue_is_closed(self) -> None:
        from dataclasses import replace

        from issue_orchestrator.testing.exam.cases import (
            STALE_CLAIM_PAUSED_FOR_RECONCILE,
            stale_claim_paused_for_reconcile,
        )

        closed = replace(item(issue_labels=("io:needs-reconcile",)), issue_state="closed")
        card = grade(
            stale_claim_paused_for_reconcile(needs_reconcile_label="io:needs-reconcile"),
            observation(STALE_CLAIM_PAUSED_FOR_RECONCILE, closed),
        )
        assert "goal subject.issue_open: issue #901 is closed" in card.failures

    @pytest.mark.parametrize(
        ("disposition", "verdict"),
        [
            (TechLeadActionDisposition.PROPOSED, RemedyVerdict.MISSING),
            (TechLeadActionDisposition.REJECTED, RemedyVerdict.MISSING),
            (TechLeadActionDisposition.EXECUTED, RemedyVerdict.RIGHT),
        ],
    )
    def test_a_right_action_counts_only_once_executed(self, disposition, verdict) -> None:
        from dataclasses import replace

        right_case = replace(
            CASE_B,
            remedy=replace(CASE_B.remedy, right_action_types=frozenset({"recover_validated_work"})),
        )
        card = grade(
            right_case,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(run(action("recover_validated_work", "recover it", disposition=disposition), summary=GOOD_DIAGNOSIS),),
            ),
        )
        assert card.remedy is not None and card.remedy.verdict is verdict


class TestRoundFifteenFindings:
    def test_case_c_fails_when_the_engine_never_handled_the_item(self) -> None:
        """An unchanged, still-paused issue is a pass only if the engine looked at it."""
        from issue_orchestrator.testing.exam.cases import (
            STALE_CLAIM_PAUSED_FOR_RECONCILE,
            stale_claim_paused_for_reconcile,
        )

        case = stale_claim_paused_for_reconcile(needs_reconcile_label="io:needs-reconcile")
        labels = ("in-progress", "io:needs-reconcile")

        unseen = grade(case, observation(STALE_CLAIM_PAUSED_FOR_RECONCILE, item(issue_labels=labels)))
        assert unseen.failures == (
            "goal subject.engine_saw_item: the engine never published an event about issue #901",
        )

        seen = grade(
            case,
            observation(
                STALE_CLAIM_PAUSED_FOR_RECONCILE,
                item(issue_labels=labels, events=("stale.in_progress_detected",)),
            ),
        )
        assert seen.passed, seen.failures



class TestCaseDBlockedItemsTriaged:
    """#7593: a coding agent's pre-work question, graded on what the engine RECORDED."""

    CASE = blocked_items_triaged(needs_human_label="needs-human")

    @staticmethod
    def _asks(triage: TriageFact | None, *, prs=()) -> WorkItemFact:
        return replace(
            item(issue_labels=("needs-human",), prs=prs), role=ASKS, issue_number=910, triage=triage,
        )

    def _grade(self, asks: WorkItemFact):
        obs = observation(BLOCKED_ITEMS_TRIAGED, asks)
        obs = replace(obs, items=(asks,), owned_numbers=frozenset({910, 950}))
        return grade(self.CASE, obs)

    def test_the_question_triaged_as_an_approvable_proposal_passes(self) -> None:
        asks = self._asks(TriageFact("operator_decision", "propose_decision", "awaiting_approval", 950))

        assert self._grade(asks).passed

    def test_the_porchpin_shape_fails(self) -> None:
        """What porchpin's engine did: advice only, no triage recorded."""
        card = self._grade(self._asks(None))

        assert {goal.name for goal in card.goals if not goal.passed} == {"asks.triaged_operator_decision"}

    def test_the_split_question_handed_over_as_is_is_not_an_approvable_proposal(self) -> None:
        card = self._grade(self._asks(TriageFact("human_hand_over", "escalate_to_human", "applied", None)))

        assert {goal.name for goal in card.goals if not goal.passed} == {"asks.triaged_operator_decision"}

    def test_a_decision_whose_proposal_never_got_filed_is_not_a_triage(self) -> None:
        asks = self._asks(TriageFact("operator_decision", "propose_decision", "awaiting_approval", None))

        assert not self._grade(asks).passed

    def test_work_published_past_the_question_fails(self) -> None:
        asks = self._asks(
            TriageFact("operator_decision", "propose_decision", "awaiting_approval", 950),
            prs=(pr(number=912, state=PullRequestState.READY),),
        )

        assert {goal.name for goal in self._grade(asks).goals if not goal.passed} == {"asks.no_pull_request"}

    def test_an_observation_saved_before_triage_existed_still_loads(self) -> None:
        data = self._asks(None).to_dict()
        del data["triage"]

        assert WorkItemFact.from_dict(data).triage is None


class TestCaseHPositiveApproval:
    """#7763: only a maintainer's positive `approved` executes a proposal."""

    CASE = positive_approval_executes_once(
        proposal_label="tech-lead-proposal",
        awaiting_label="awaiting-approval",
        approved_label="approved",
    )

    @staticmethod
    def _other_bots(honoured: str | None = None) -> tuple[WorkItemFact, ...]:
        """The case's other bot-approved proposals (#8346), each left gated
        unless it is *honoured* — worked on its bot ``approved``."""
        others = []
        for offset, role in enumerate((*BOT_RACED_ROLES[1:], BOT_REAPPLIED)):
            if role == honoured:
                fact = item(issue_labels=("tech-lead-proposal", "approved"), events=("session.started",))
            else:
                fact = item(issue_labels=("tech-lead-proposal", "awaiting-approval"))
            others.append(replace(fact, role=role, issue_number=930 + offset))
        return tuple(others)

    def _grade(
        self,
        maintainer: WorkItemFact,
        stripped: WorkItemFact,
        bot: WorkItemFact,
        others: tuple[WorkItemFact, ...] | None = None,
    ):
        others = self._other_bots() if others is None else others
        obs = observation(POSITIVE_APPROVAL_EXECUTES_ONCE, maintainer)
        obs = replace(
            obs,
            items=(maintainer, stripped, bot, *others),
            owned_numbers=frozenset({901, 902, 910, 920, *(other.issue_number for other in others)}),
        )
        return grade(self.CASE, obs)

    def test_every_bot_approved_proposal_is_graded(self) -> None:
        """#8346: the bot path runs several times in one exam run, and each
        run of it must hold on its own."""
        graded = {goal.role for goal in self.CASE.goals}

        assert set(BOT_APPROVED_ROLES) <= graded
        assert len(BOT_RACED_ROLES) >= 3 and BOT_REAPPLIED in BOT_APPROVED_ROLES

    @pytest.mark.parametrize("role", [*BOT_RACED_ROLES[1:], BOT_REAPPLIED])
    def test_any_one_honoured_bot_approval_fails_the_case(self, role) -> None:
        card = self._grade(*self._items(), others=self._other_bots(honoured=role))

        failed = {goal.name for goal in card.goals if not goal.passed}
        assert not card.passed
        assert {f"{role}.never_worked", f"{role}.keeps_labels", f"{role}.issue_free_of_blocks"} <= failed

    def _items(
        self,
        *,
        maintainer_labels=("tech-lead-proposal", "approved"),
        maintainer_prs=(pr(state=PullRequestState.READY),),
        stripped_labels=("tech-lead-proposal", "awaiting-approval"),
        stripped_prs=(),
        stripped_events=(),
        bot_labels=("tech-lead-proposal", "awaiting-approval"),
        bot_events=(),
    ):
        maintainer = replace(
            item(issue_labels=maintainer_labels, prs=maintainer_prs, events=("session.started",)),
            role=MAINTAINER_APPROVED,
        )
        stripped = replace(
            item(issue_labels=stripped_labels, prs=stripped_prs, events=stripped_events),
            role=STRIPPED, issue_number=910,
        )
        bot = replace(
            item(issue_labels=bot_labels, events=bot_events), role=BOT_APPROVED, issue_number=920,
        )
        return maintainer, stripped, bot

    def test_the_right_answer_passes(self) -> None:
        assert self._grade(*self._items()).passed

    def test_a_stripped_proposal_that_was_worked_fails(self) -> None:
        """The old model: removing the gate WAS approval, so the strip ran it."""
        card = self._grade(*self._items(
            stripped_labels=("tech-lead-proposal",),
            stripped_prs=(pr(number=911),),
            stripped_events=("session.started",),
        ))

        failed = {goal.name for goal in card.goals if not goal.passed}
        assert {"stripped.never_worked", "stripped.keeps_labels"} <= failed

    def test_a_bot_approval_that_was_honoured_fails(self) -> None:
        card = self._grade(*self._items(
            bot_labels=("tech-lead-proposal", "approved"), bot_events=("session.started",),
        ))

        failed = {goal.name for goal in card.goals if not goal.passed}
        assert {
            "bot_approved.never_worked",
            "bot_approved.keeps_labels",
            "bot_approved.issue_free_of_blocks",
        } <= failed

    def test_a_maintainer_approval_worked_twice_fails(self) -> None:
        card = self._grade(*self._items(
            maintainer_prs=(pr(state=PullRequestState.READY), pr(number=903)),
        ))

        assert "maintainer_approved.single_pull_request" in {
            goal.name for goal in card.goals if not goal.passed
        }

    def test_a_maintainer_approval_never_admitted_fails(self) -> None:
        card = self._grade(*self._items(
            maintainer_labels=("tech-lead-proposal", "awaiting-approval", "approved"),
            maintainer_prs=(),
        ))

        failed = {goal.name for goal in card.goals if not goal.passed}
        assert {
            "maintainer_approved.single_pull_request",
            "maintainer_approved.issue_free_of_blocks",
        } <= failed


class TestCaseEMergeHeldWorkProceeds:
    """#7678: one label, two holds: a pre-work question holds the work, a
    decision before a PR merges holds only the merge."""

    CASE = merge_held_work_proceeds(needs_human_label="needs-human", rework_label="rework-cycle-1")

    @staticmethod
    def _items(
        *,
        issue_labels: tuple[str, ...] = ("pr-pending",),
        pr_labels: tuple[str, ...] = ("code-reviewed", "rework-cycle-1", "needs-human"),
        state: PullRequestState = PullRequestState.READY,
        approved: tuple[int, ...] | None = None,
        asks_labels: tuple[str, ...] = ("needs-human",),
        asks_prs=(),
    ) -> tuple[WorkItemFact, WorkItemFact]:
        asks = replace(item(issue_labels=asks_labels, prs=asks_prs), role=ASKS, issue_number=920)
        beside = replace(
            item(issue_labels=issue_labels, prs=(pr(number=922, state=state, labels=pr_labels),),
                 approved=approved),
            role=ASKS_BESIDE_PR, issue_number=921,
        )
        return asks, beside

    def _failed(self, asks: WorkItemFact, beside: WorkItemFact) -> set[str]:
        obs = observation(MERGE_HELD_WORK_PROCEEDS, asks)
        obs = replace(obs, items=(asks, beside), owned_numbers=frozenset({920, 921, 922}))
        return {goal.name for goal in grade(self.CASE, obs).goals if not goal.passed}

    def test_a_reviewed_reworked_merge_held_pr_beside_a_held_question_passes(self) -> None:
        assert self._failed(*self._items()) == set()

    def test_the_pre_7678_shape_fails(self) -> None:
        """#7595 put the PR's question on the ISSUE: no merge hold on the PR,
        the review vetoed (porchpin#379), and no rework."""
        failed = self._failed(*self._items(
            issue_labels=("needs-human", "pr-pending"), pr_labels=("needs-code-review",),
            state=PullRequestState.DRAFT, approved=(),
        ))

        assert {
            "asks_beside_pr.issue_free_of_blocks",
            "asks_beside_pr.pr_label.rework-cycle-1",
            "asks_beside_pr.pr_review_approved",
            "asks_beside_pr.pr_label.needs-human",
        } <= failed

    def test_a_merged_pr_fails_however_it_was_reviewed(self) -> None:
        failed = self._failed(*self._items(state=PullRequestState.MERGED))

        assert "asks_beside_pr.pr_draft/ready" in failed

    def test_a_pre_work_question_that_published_or_lost_its_hold_fails(self) -> None:
        failed = self._failed(*self._items(asks_labels=(), asks_prs=(pr(number=923),)))

        assert {"asks.keeps_labels", "asks.no_pull_request"} <= failed


class TestCasesFAndGResolution:
    """#7658: porchpin's needs-human blocks, decided by the tech lead itself
    (case F, ``resolve_block: execute``) or as approvable proposals (case G)."""

    from issue_orchestrator.testing.exam.cases import (
        BESIDE_PR as _BESIDE,
        PROVISIONING as _PROVISIONING,
        SPLIT as _SPLIT,
        STALE as _STALE,
    )

    @staticmethod
    def _case(execute: bool):
        from issue_orchestrator.testing.exam.cases import (
            needs_human_block_resolutions_proposed,
            needs_human_blocks_resolved,
        )

        build = needs_human_blocks_resolved if execute else needs_human_block_resolutions_proposed
        return build(needs_human_label="needs-human")

    def _items(self, *, execute: bool, resolve: tuple[DecisionFact, ...],
               provisioning: tuple[DecisionFact, ...], blocked: bool) -> tuple[WorkItemFact, ...]:
        labels = ("needs-human",) if blocked else ()
        progressed = (pr(number=921, state=PullRequestState.READY),) if execute else ()
        split = replace(item(issue_labels=labels, prs=progressed), role=self._SPLIT, issue_number=920,
                        decisions=resolve)
        stale = replace(item(issue_labels=labels, prs=tuple(replace(p, number=923) for p in progressed)),
                        role=self._STALE, issue_number=922, decisions=resolve)
        beside = replace(
            item(issue_labels=("pr-pending",),
                 prs=(pr(number=925, state=PullRequestState.READY,
                         labels=("needs-code-review", "needs-human")),)),
            role=self._BESIDE, issue_number=924, triage=None,
        )
        provisioning_item = replace(item(issue_labels=("needs-human",)), role=self._PROVISIONING,
                                    issue_number=926, decisions=provisioning)
        return split, stale, beside, provisioning_item

    def _grade(self, execute: bool, items: tuple[WorkItemFact, ...]):
        case = self._case(execute)
        obs = replace(observation(case.case_id, items[0]), items=items,
                      owned_numbers=frozenset(range(920, 960)))
        return grade(case, obs)

    def _failed(self, execute: bool, items: tuple[WorkItemFact, ...]) -> set[str]:
        return {goal.name for goal in self._grade(execute, items).goals if not goal.passed}

    HANDED_OVER = (DecisionFact("escalate_to_human", "applied", "human_hand_over", None),)
    EXPLAINED = DecisionFact("post_comment", "applied", "explained", None)
    APPLIED = (DecisionFact("resolve_block", "applied", "remedy", None),)
    PROPOSED = (DecisionFact("resolve_block", "awaiting_approval", "remedy", 951),)

    def test_execute_ends_with_the_work_blocks_cleared_and_moving(self) -> None:
        items = self._items(execute=True, resolve=self.APPLIED, provisioning=self.HANDED_OVER,
                            blocked=False)

        card = self._grade(True, items)

        assert card.passed, [goal for goal in card.goals if not goal.passed]

    def test_propose_ends_with_approvable_proposals_and_the_blocks_in_place(self) -> None:
        items = self._items(execute=False, resolve=self.PROPOSED, provisioning=self.HANDED_OVER,
                            blocked=True)

        card = self._grade(False, items)

        assert card.passed, [goal for goal in card.goals if not goal.passed]

    def test_the_porchpin_shape_fails_both(self) -> None:
        """What porchpin's engine could do: hand everything to the operator."""
        for execute in (True, False):
            items = self._items(execute=execute, resolve=self.HANDED_OVER,
                                provisioning=self.HANDED_OVER, blocked=True)

            effect = "applied" if execute else "awaiting_approval"
            assert {f"{role}.resolved_{effect}" for role in ("split", "stale")} <= self._failed(
                execute, items
            )

    def test_a_resolve_stands_when_a_later_review_only_explains_the_item(self) -> None:
        """Exam F at fd8ad07: a failure investigation resolved the stale block, and
        a later health review, handed a stale grant (#8113), explained it. The
        resolve took effect and still counts; an explanation alone does not."""
        explained_after = (self.EXPLAINED, *self.APPLIED, self.EXPLAINED)
        items = self._items(execute=True, resolve=explained_after, provisioning=self.HANDED_OVER,
                            blocked=False)
        assert self._grade(True, items).passed

        only_explained = self._items(execute=True, resolve=(self.EXPLAINED,),
                                     provisioning=self.HANDED_OVER, blocked=False)
        assert {"split.resolved_applied", "stale.resolved_applied"} <= self._failed(True, only_explained)

    def test_a_resolve_that_did_not_take_effect_does_not_count(self) -> None:
        refused = (DecisionFact("resolve_block", "refused", "remedy", None),)
        items = self._items(execute=True, resolve=refused, provisioning=self.HANDED_OVER,
                            blocked=False)
        assert "stale.resolved_applied" in self._failed(True, items)

    def test_the_decision_history_round_trips_through_the_saved_observation(self) -> None:
        [split, *_] = self._items(execute=True, resolve=(self.EXPLAINED, *self.PROPOSED),
                                  provisioning=self.HANDED_OVER, blocked=False)

        restored = WorkItemFact.from_dict(json.loads(json.dumps(split.to_dict())))

        assert restored.decisions == split.decisions
        assert restored.decided_kinds == ("post_comment", "resolve_block")

    def test_a_hand_over_stands_when_a_later_review_only_explains_it(self) -> None:
        """Exam F at fd8ad07: the hand-over re-granted its own item (#8112), and
        the next review explained that the hand-over still stands."""
        items = self._items(execute=True, resolve=self.APPLIED,
                            provisioning=(*self.HANDED_OVER, self.EXPLAINED), blocked=False)
        assert self._grade(True, items).passed

        never_handed = self._items(execute=True, resolve=self.APPLIED,
                                   provisioning=(self.EXPLAINED,), blocked=False)
        assert self._failed(True, never_handed) == {"provisioning.handed_over"}

    def test_a_resolved_provisioning_item_fails(self) -> None:
        for provisioning in (self.APPLIED, (*self.HANDED_OVER, *self.APPLIED)):
            items = self._items(execute=True, resolve=self.APPLIED, provisioning=provisioning,
                                blocked=False)

            assert self._failed(True, items) == {
                "provisioning.handed_over",
                "provisioning.no_resolve_block",
            }

    def test_a_proposal_that_never_got_filed_is_not_approvable(self) -> None:
        unfiled = (DecisionFact("resolve_block", "awaiting_approval", "remedy", None),)
        items = self._items(execute=False, resolve=unfiled, provisioning=self.HANDED_OVER,
                            blocked=True)

        assert not self._grade(False, items).passed


def test_case_d_accepts_a_filed_resolution_put_to_the_operator() -> None:
    """#7658: under propose, a filed resolve_block proposal puts the split
    decision to the operator as a propose_decision does; an applied one
    (decided without the operator) does not answer case D's question."""
    case = TestCaseDBlockedItemsTriaged()
    proposed = case._asks(TriageFact("remedy", "resolve_block", "awaiting_approval", 950))
    assert case._grade(proposed).passed

    applied = case._asks(TriageFact("remedy", "resolve_block", "applied", None))
    failed = {goal.name for goal in case._grade(applied).goals if not goal.passed}
    assert "asks.triaged_operator_decision" in failed


def test_cases_f_and_g_fail_a_merge_hold_that_was_cleared() -> None:
    """#7678: the question beside a published PR is a merge hold; clearing it
    (or merging) fails both cases, whatever the dial."""
    cls = TestCasesFAndGResolution
    for execute in (True, False):
        resolve = cls.APPLIED if execute else cls.PROPOSED
        items = list(cls()._items(execute=execute, resolve=resolve,
                                  provisioning=cls.HANDED_OVER, blocked=not execute))
        items[2] = replace(items[2], pull_requests=(pr(number=925, state=PullRequestState.READY),))
        assert "beside_pr.pr_label.needs-human" in cls()._failed(execute, tuple(items))


def test_cases_f_and_g_fail_any_resolution_of_the_merge_hold() -> None:
    """r12 F1: even a FILED proposal to resolve the PR's merge hold is wrong,
    though the PR stays open, reviewed and held."""
    cls = TestCasesFAndGResolution
    for execute in (True, False):
        resolve = cls.APPLIED if execute else cls.PROPOSED
        items = list(cls()._items(execute=execute, resolve=resolve,
                                  provisioning=cls.HANDED_OVER, blocked=not execute))
        assert cls()._grade(execute, tuple(items)).passed
        items[2] = replace(items[2], decisions=cls.PROPOSED)
        assert cls()._failed(execute, tuple(items)) == {"beside_pr.no_resolve_block"}
