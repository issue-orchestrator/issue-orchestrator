"""A person's hand actions on GitHub, told apart from automation (#8001)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from issue_orchestrator.domain.operator_interventions import (
    ATTRIBUTION_LIMITS,
    attribution,
    github_interventions,
    is_automation,
)
from issue_orchestrator.ports.operator_activity import (
    Actor,
    ContentEdit,
    ItemActivity,
    RepoActivityRead,
    RepoComment,
    RepoEvent,
    Review,
    SourceRead,
)

T0 = datetime(2026, 10, 5, 2, 0, tzinfo=UTC)
SINCE, UNTIL = T0 - timedelta(days=1), T0 + timedelta(days=1)
PERSON = Actor("BruceBGordon", "User")
ENGINE = Actor("porchpin-bot[bot]", "Bot")
GRAPH_ENGINE = Actor("porchpin-bot", "Bot")


def _event(event: str, actor: Actor, *, label: str | None = None, at: datetime = T0, number: int = 364) -> RepoEvent:
    return RepoEvent(at=at, event=event, number=number, actor=actor, label=label, rename=None, ref=f"e:{event}")


def _read(events=(), comments=(), items=()) -> RepoActivityRead:  # type: ignore[no-untyped-def]
    return RepoActivityRead(
        events=tuple(events), comments=tuple(comments), items=tuple(items),
        sources=(SourceRead("issue events", True, "1 page(s)"),),
    )


@pytest.mark.parametrize(
    ("actor", "automated"),
    [
        (PERSON, False),
        (ENGINE, True),
        (GRAPH_ENGINE, True),
        (Actor("dependabot[bot]", "Bot"), True),
        # A GitHub App acting for a user is the App's action, not a hand action.
        (Actor("BruceBGordon", "User", via_app=True), True),
        # No actor (a deleted account): nothing says a person acted.
        (Actor(None, None), True),
        (Actor("org", "Organization"), True),
    ],
)
def test_only_a_person_account_acting_by_itself_is_a_hand_action(actor: Actor, automated: bool) -> None:
    assert is_automation(actor) is automated


def test_label_changes_by_a_person_are_hand_actions_and_the_engines_are_not() -> None:
    read = _read(events=[
        _event("unlabeled", PERSON, label="io:needs-reconcile"),
        _event("labeled", PERSON, label="approved", number=501),
        _event("labeled", ENGINE, label="needs-human"),
        _event("referenced", PERSON),
    ])

    hand, excluded = github_interventions(read, since=SINCE, until=UNTIL)

    assert [(i.kind, i.subject, i.detail, i.attribution, i.actor) for i in hand] == [
        ("label_removed", "#364", "io:needs-reconcile", "person", "BruceBGordon"),
        ("label_added", "#501", "approved", "person", "BruceBGordon"),
    ]
    assert excluded == 1
    assert all(i.source == "github_events" and i.ref for i in hand)


def test_the_operators_identity_is_never_guessed_to_be_the_operator() -> None:
    """The coordinator acts under the operator's identity: without the
    coordinator's signature a hand action is a person's, and the coverage
    says why."""
    signed = RepoComment(at=T0, updated_at=T0, number=8, actor=PERSON, body="Round 2 ... 🤖 Generated with [Claude Code](x)", ref="c1")
    plain = RepoComment(at=T0, updated_at=T0, number=8, actor=PERSON, body="Ruling: rework #379 to this design.", ref="c2")

    hand, _ = github_interventions(_read(comments=[signed, plain]), since=SINCE, until=UNTIL)

    assert [(i.attribution, i.ref) for i in hand] == [("coordinator", "c1"), ("person", "c2")]
    assert attribution("") == "person"
    assert any("acts under the operator's GitHub identity" in limit for limit in ATTRIBUTION_LIMITS)


def test_body_edits_reviews_merges_and_hand_opened_items() -> None:
    pr = ItemActivity(
        number=511, is_pr=True, created_at=T0 - timedelta(hours=1), author=ENGINE,
        edits=(
            # The first "edit" is the creation itself.
            ContentEdit(T0 - timedelta(hours=1), GRAPH_ENGINE),
            ContentEdit(T0, PERSON),
            ContentEdit(T0 + timedelta(minutes=1), GRAPH_ENGINE),
        ),
        reviews=(Review(T0, "APPROVED", PERSON), Review(T0, "COMMENTED", GRAPH_ENGINE), Review(T0, "DISMISSED", PERSON)),
        merged_at=T0 + timedelta(hours=1), merged_by=PERSON, ref="pr511",
    )
    issue = ItemActivity(
        # A person's own creation shows as an "edit" at creation time: not an edit.
        number=520, is_pr=False, created_at=T0, author=PERSON, edits=(ContentEdit(T0 + timedelta(seconds=1), PERSON),),
        reviews=(), merged_at=None, merged_by=None, ref="i520",
    )
    # The merge also closes the PR: one hand action, not two.
    close = _event("closed", PERSON, at=T0 + timedelta(hours=1, seconds=1), number=511)

    hand, excluded = github_interventions(_read(events=[close], items=[pr, issue]), since=SINCE, until=UNTIL)

    assert sorted((i.kind, i.subject) for i in hand) == [
        ("body_edited", "PR #511"), ("merged", "PR #511"), ("opened", "#520"), ("review_approved", "PR #511"),
        ("review_since_dismissed", "PR #511"),
    ]
    assert excluded == 3  # the bot-opened PR, the bot's later edit, the bot's review


def test_only_the_window_counts() -> None:
    early = _event("labeled", PERSON, label="x", at=SINCE - timedelta(seconds=1))
    late = _event("labeled", ENGINE, label="x", at=UNTIL + timedelta(seconds=1))

    assert github_interventions(_read(events=[early, late]), since=SINCE, until=UNTIL) == ((), 0)


def test_a_comment_edited_after_the_cutoff_keeps_its_act_but_not_its_later_text() -> None:
    """r1 F1: GitHub serves a comment's CURRENT body; text written after the
    cutoff is not what the window saw."""
    late = RepoComment(at=T0, updated_at=UNTIL + timedelta(minutes=5), number=379, actor=PERSON,
                       body="Edited later: approve now 🤖 Generated with [Claude Code](x)", ref="c3")

    [hand], _ = github_interventions(_read(comments=[late]), since=SINCE, until=UNTIL)

    assert hand.kind == "commented" and hand.ref == "c3"
    assert "Edited later" not in hand.detail and "withheld" in hand.detail
    # Nor is the later text's signature trusted.
    assert hand.attribution == "person"


def test_a_comment_edited_in_the_window_keeps_its_text_but_not_its_signature() -> None:
    """r3 F1: the coordinator may edit a person's comment under the same
    identity; the signature added by the edit says nothing about who wrote it."""
    edited = RepoComment(at=T0, updated_at=T0 + timedelta(hours=1), number=379, actor=PERSON,
                         body="Ruling: rework. 🤖 Generated with [Claude Code](x)", ref="c4")

    [hand], _ = github_interventions(_read(comments=[edited]), since=SINCE, until=UNTIL)

    assert hand.attribution == "person"
    assert hand.detail.startswith("(edited at ") and "Ruling: rework." in hand.detail


def test_a_review_dismissed_since_is_kept_with_its_verdict_unknown() -> None:
    """r3 F2: GitHub reports a dismissed review's current state only."""
    pr = ItemActivity(
        number=379, is_pr=True, created_at=SINCE, author=ENGINE, edits=(),
        reviews=(Review(T0, "DISMISSED", PERSON),), merged_at=None, merged_by=None, ref="pr379",
    )

    hand, _ = github_interventions(_read(items=[pr]), since=SINCE, until=UNTIL)

    assert [(i.kind, i.detail) for i in hand if i.kind.startswith("review")] == [
        ("review_since_dismissed", "dismissed since; its verdict at submission is not reported"),
    ]
