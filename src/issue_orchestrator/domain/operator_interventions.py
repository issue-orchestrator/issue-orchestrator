"""Which GitHub activity is a person's hand action, and whose (#8001).

The one policy that turns the audited repository's raw activity
(:mod:`..ports.operator_activity`) into the operator's interventions for the
improver. Automation is left out: a ``Bot`` account (the engine's App,
Dependabot) or a GitHub App acting for a user is not a hand action.

What remains is a person's identity, and here the evidence runs out: the
coordinator (an AI coding session working for the operator) acts under the
operator's own GitHub identity. Only text can tell them apart: a comment
carrying the coordinator's signature (:data:`COORDINATOR_SIGNATURES`) is
``coordinator``. Everything else a person's identity did is ``person``,
never guessed to be the operator's, and the coverage says so
(:data:`ATTRIBUTION_LIMITS`).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta

from ..contracts.improver_inputs import Intervention, InterventionAttribution, InterventionKind
from ..ports.operator_activity import Actor, ItemActivity, RepoActivityRead, RepoComment, RepoEvent

#: Text only the coordinator's sessions put in what they write.
COORDINATOR_SIGNATURES: tuple[str, ...] = (
    "Generated with [Claude Code]",
    "Codex adversarial review",
)

ATTRIBUTION_LIMITS: tuple[str, ...] = (
    "The coordinator acts under the operator's GitHub identity: a label change, edit, close,"
    " merge or review by that identity is attributed `person`, and may be the operator's or the"
    " coordinator's. Only text carrying the coordinator's signature is attributed `coordinator`.",
    "Automation is excluded: a Bot account (the engine's App, Dependabot) or an App acting for a user.",
)

#: Issue events that are hand actions when a person does them.
_EVENT_KINDS: dict[str, InterventionKind] = {
    "labeled": "label_added",
    "unlabeled": "label_removed",
    "renamed": "title_edited",
    "closed": "closed",
    "reopened": "reopened",
}
_REVIEW_KINDS: dict[str, InterventionKind] = {
    "APPROVED": "review_approved",
    "CHANGES_REQUESTED": "review_changes_requested",
    "COMMENTED": "review_commented",
}
#: An edit this close to an item's creation is its creation, not an edit.
_CREATION_EDIT = timedelta(seconds=2)
_COMMENT_EXCERPT = 300
#: A merge's own close lands within seconds of it.
_MERGE_CLOSE = timedelta(seconds=30)


def is_automation(actor: Actor) -> bool:
    """A Bot account, an App acting for a user, or no actor at all (a
    deleted account: nothing says a person acted)."""
    return actor.via_app or actor.account_type != "User" or actor.login is None


def attribution(text: str = "") -> InterventionAttribution:
    """``coordinator`` only on the coordinator's signature; else ``person``."""
    return "coordinator" if any(sig in text for sig in COORDINATOR_SIGNATURES) else "person"


def github_interventions(
    read: RepoActivityRead, *, since: datetime, until: datetime
) -> tuple[tuple[Intervention, ...], int]:
    """The hand actions in ``[since, until]``, and how many automation
    actions in the window were left out."""
    found: list[Intervention] = []
    excluded = 0
    merges = {
        (item.number, item.merged_by.login): item.merged_at
        for item in read.items
        if item.merged_at is not None and item.merged_by is not None
    }
    for actor, intervention in (
        *_events(read.events),
        *_comments(read.comments),
        *(pair for item in read.items for pair in _item(item)),
    ):
        if not since <= intervention.at <= until or _is_merge_close(intervention, merges):
            continue
        if is_automation(actor):
            excluded += 1
            continue
        found.append(intervention)
    return tuple(found), excluded


def _is_merge_close(
    intervention: Intervention, merges: dict[tuple[int, str | None], datetime]
) -> bool:
    """A merge also closes its PR: that ``closed`` event is the merge, not a
    second hand action."""
    if intervention.kind != "closed" or not intervention.subject.startswith("#"):
        return False
    merged_at = merges.get((int(intervention.subject[1:]), intervention.actor))
    return merged_at is not None and abs(intervention.at - merged_at) <= _MERGE_CLOSE


def _events(events: Iterable[RepoEvent]) -> Iterator[tuple[Actor, Intervention]]:
    for e in events:
        kind = _EVENT_KINDS.get(e.event)
        if kind is None:
            continue
        detail = (
            e.label if e.label is not None
            else f"{e.rename[0]!r} -> {e.rename[1]!r}" if e.rename is not None
            else e.event
        )
        yield e.actor, Intervention(
            at=e.at, kind=kind, subject=f"#{e.number}", detail=detail or e.event,
            source="github_events", attribution=attribution(), actor=e.actor.login, ref=e.ref,
        )


def _comments(comments: Iterable[RepoComment]) -> Iterator[tuple[Actor, Intervention]]:
    for c in comments:
        excerpt = " ".join(c.body.split())[:_COMMENT_EXCERPT]
        yield c.actor, Intervention(
            at=c.at, kind="commented", subject=f"#{c.number}", detail=excerpt,
            source="github_comments", attribution=attribution(c.body), actor=c.actor.login, ref=c.ref,
        )


def _item(item: ItemActivity) -> Iterator[tuple[Actor, Intervention]]:
    subject = f"{'PR ' if item.is_pr else ''}#{item.number}"

    def made(at: datetime, kind: InterventionKind, actor: Actor, detail: str) -> tuple[Actor, Intervention]:
        return actor, Intervention(
            at=at, kind=kind, subject=subject, detail=detail,
            source="github_items", attribution=attribution(), actor=actor.login, ref=item.ref,
        )

    yield made(item.created_at, "opened", item.author, "opened")
    for edit in item.edits:
        if edit.at - item.created_at > _CREATION_EDIT:
            yield made(edit.at, "body_edited", edit.editor, "the body was edited")
    for review in item.reviews:
        kind = _REVIEW_KINDS.get(review.state)
        if kind is not None:
            yield made(review.at, kind, review.author, review.state.lower())
    if item.merged_at is not None and item.merged_by is not None:
        yield made(item.merged_at, "merged", item.merged_by, "merged")


__all__ = [
    "ATTRIBUTION_LIMITS",
    "COORDINATOR_SIGNATURES",
    "attribution",
    "github_interventions",
    "is_automation",
]
