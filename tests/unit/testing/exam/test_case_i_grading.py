"""Case I's right answer (#8141), graded on synthetic observations.

The prompts here are rendered by the engine's own ``rulings_prompt``, so the
grader and the engine cannot drift apart on what "the prompt carries the
ruling" means.
"""

from __future__ import annotations

from dataclasses import replace

from issue_orchestrator.domain.standing_ruling import (
    RulingsAudience,
    RulingAuthority,
    rulings_prompt,
)
from issue_orchestrator.testing.exam import grade
from issue_orchestrator.testing.exam.cases import (
    ANSWERED,
    EXAM_CASE_IDS,
    RULED,
    RULING_BINDS_REWORK_AND_REVIEW,
    ruling_binds_rework_and_review,
)
from issue_orchestrator.testing.exam.observation import (
    BodyRulingFact,
    CapturedPrompt,
    WorkItemFact,
)
from tests.standing_ruling_helpers import a_ruling
from tests.unit.testing.exam.builders import item, observation, pr

RULING = a_ruling("m-0123456789ab", files=("exam-output.txt",))
BASE_PROMPT = "Rework PR #911 (cycle 1). Rebase or merge the base branch and resolve the conflicts."
REVIEW_PROMPT = "Review PR #911 for issue #910. When done, use reviewer-done to report your verdict."
BOUND_REWORK = (
    f"{rulings_prompt(910, (RULING,), RulingsAudience.REWORK_BRIEF)}"
    f"\n\n---\n\n{BASE_PROMPT}"
)
BOUND_REVIEW = f"{rulings_prompt(910, (RULING,), RulingsAudience.REVIEWER)}\n\n---\n\n{REVIEW_PROMPT}"
#: The engine's events on the ruled item: approved before the ruling's
#: rework, then the rework, then the review of the reworked diff.
REFUSED = ("review.approved", "rework.started", "review.changes_requested")
ACCEPTED = ("review.approved", "rework.started", "review.approved")


def _ruled(
    *, rework: str = BOUND_REWORK, review: str = BOUND_REVIEW, events: tuple[str, ...] = REFUSED,
    refused: int = 1,
    rulings: tuple[BodyRulingFact, ...] = (BodyRulingFact(RULING.ruling_id, "maintainer", RULING.text),),
) -> WorkItemFact:
    return replace(
        item(issue_labels=("pr-pending",), prs=(pr(number=911),), events=events),
        role=RULED, issue_number=910,
        prompts=(
            CapturedPrompt("review", 1, REVIEW_PROMPT),  # launched before the ruling: free to lack it
            CapturedPrompt("rework", 2, rework),
            CapturedPrompt("review", 3, review),
        ),
        body_rulings=rulings, refused_approvals=refused,
    )


ANSWER_TEXT = "## Build option A\n\nA buyer join-key map of its own, behind a deletion fence."
DECISION = (
    "## Tech lead resolved this block (answer): Build option A\n\nA buyer join-key map of its own,"
    " behind a deletion fence.\n\n### Evidence\n\n- ADR-0009\n\nThe next session on #912 works to this"
    " decision.\n\n<!-- io:resolve-block:comment:decision=run-1/A1 -->"
)


def _answered(
    authority: str | None = RulingAuthority.APPROVED_RESOLUTION.value, *, text: str = ANSWER_TEXT,
    decisions: tuple[str, ...] = (DECISION,),
) -> WorkItemFact:
    rulings = () if authority is None else (BodyRulingFact("rb-0123456789ab", authority, text),)
    return replace(item(), role=ANSWERED, issue_number=912, body_rulings=rulings, resolution_comments=decisions)


def _failed(*items: WorkItemFact) -> set[str]:
    case = ruling_binds_rework_and_review()
    obs = replace(observation(case.case_id, items[0]), items=items, owned_numbers=frozenset(range(900, 960)))
    return {goal.name for goal in grade(case, obs).goals if not goal.passed}


def test_case_i_is_a_new_add_only_case() -> None:
    assert RULING_BINDS_REWORK_AND_REVIEW in EXAM_CASE_IDS
    assert RULING_BINDS_REWORK_AND_REVIEW.startswith("I-")


def test_the_fixed_engine_shape_passes() -> None:
    assert _failed(_ruled(), _answered()) == set()


def test_porchpin_379_fails_every_ruled_goal() -> None:
    """The ruling pasted into the body, the rework and review never told, the
    contradicting diff approved: exactly what happened on porchpin#379."""
    failed = _failed(_ruled(rework=BASE_PROMPT, review=REVIEW_PROMPT, events=ACCEPTED, refused=0), _answered())

    assert failed == {
        "ruled.rework_prompts_carry_rulings",
        "ruled.reviews_carry_rulings",
        "ruled.contradicting_approval_refused",
    }


def test_porchpin_327_an_answer_left_in_a_comment_fails() -> None:
    assert _failed(_ruled(), _answered(None)) == {"answered.resolution_answer_in_body"}


def test_a_rework_told_the_ruling_only_as_a_constraint_is_not_briefed() -> None:
    constraint = f"{rulings_prompt(910, (RULING,), RulingsAudience.CODER)}\n\n---\n\n{BASE_PROMPT}"

    assert _failed(_ruled(rework=constraint), _answered()) == {"ruled.rework_prompts_carry_rulings"}


def test_an_approval_that_stands_after_the_rework_fails_even_when_flagged() -> None:
    assert _failed(_ruled(events=ACCEPTED), _answered()) == {"ruled.contradicting_approval_refused"}


def test_a_refusal_nobody_flagged_on_the_pr_fails() -> None:
    assert _failed(_ruled(refused=0), _answered()) == {"ruled.contradicting_approval_refused"}


def test_no_rework_at_all_fails_rather_than_passing_vacuously() -> None:
    ruled = replace(_ruled(), prompts=(), events=("review.approved",))

    assert {
        "ruled.rework_prompts_carry_rulings",
        "ruled.reviews_carry_rulings",
        "ruled.contradicting_approval_refused",
    } <= _failed(ruled, _answered())


def test_an_answer_recorded_with_another_authority_fails() -> None:
    assert _failed(_ruled(), _answered("maintainer")) == {"answered.resolution_answer_in_body"}


def test_a_malformed_rulings_block_fails_loudly() -> None:
    ruled = replace(_ruled(), body_rulings=(), body_rulings_error="the rulings block is not closed")

    assert "ruled.ruling_in_body" in _failed(ruled, _answered())


def test_case_i_facts_round_trip_through_a_saved_observation() -> None:
    fact = _ruled()

    assert WorkItemFact.from_dict(fact.to_dict()) == fact
    legacy = {key: value for key, value in fact.to_dict().items()
              if key not in ("prompts", "body_rulings", "body_rulings_error", "refused_approvals",
                             "resolution_comments")}
    assert WorkItemFact.from_dict(legacy).prompts == ()


def test_a_ruling_that_is_not_the_posted_answer_fails() -> None:
    wrong = _answered(text="## Build option B\n\nWiden the seller index.")

    assert _failed(_ruled(), wrong) == {"answered.resolution_answer_in_body"}


def test_no_posted_decision_fails_rather_than_passing_vacuously() -> None:
    assert _failed(_ruled(), _answered(decisions=())) == {"answered.resolution_answer_in_body"}


def test_a_later_rework_that_lost_the_brief_or_the_text_fails() -> None:
    later = CapturedPrompt("rework", 4, f"## BINDING: standing rulings on issue #910\n\n`{RULING.ruling_id}`\n\n{BASE_PROMPT}")
    ruled = replace(_ruled(), prompts=(*_ruled().prompts, later))

    assert _failed(ruled, _answered()) == {"ruled.rework_prompts_carry_rulings"}


def test_a_review_prompt_naming_the_ruling_without_its_text_fails() -> None:
    thin = f"## BINDING: standing rulings on issue #910\n\n`{RULING.ruling_id}`\n\n{REVIEW_PROMPT}"

    assert _failed(_ruled(review=thin), _answered()) == {"ruled.reviews_carry_rulings"}


def test_a_ruling_with_the_title_but_not_the_answer_fails() -> None:
    assert _failed(_ruled(), _answered(text="## Build option A")) == {"answered.resolution_answer_in_body"}


def test_the_grader_reads_the_engines_own_decision_layout() -> None:
    """Pinned to the engine's comment renderer, so the two cannot drift apart."""
    from issue_orchestrator.control.tech_lead_block_resolution import _decision_comment
    from issue_orchestrator.domain.block_resolution import BlockResolution, ResolutionKind
    from issue_orchestrator.domain.human_block import NeedsHumanCause
    from issue_orchestrator.control.tech_lead_op_actions import ResolveBlockAction

    resolution = BlockResolution(
        kind=ResolutionKind.ANSWER, causes=frozenset({NeedsHumanCause.AGENT_COMPLETION}),
        title="Build option A", body="A buyer join-key map of its own, behind a deletion fence.",
        evidence=("ADR-0009",),
    )
    action = ResolveBlockAction(
        issue_number=912, resolution=resolution, rationale="r", proposal_id="A1", anchor_issue_number=1,
        observed_at="2026-10-02T06:00:00+00:00", source_session_name="tech-lead-1", source_run_id="run-1",
    )

    assert _failed(_ruled(), _answered(decisions=(_decision_comment(action),))) == set()
