"""Releasing validated work rebuilt elsewhere with rewritten history (#9092).

Work republished on another PR of its issue is released by proof: the PR's
head contains the record's head (#8137, ``carried_by_issue_pull_request``).
A PR later rebased with conflicts resolved carries none of the record's
commits, so no ancestry or patch-identity proof connects them and inferring
one would be guessing. Releasing such records is the operator's explicit
decision, never an inference.

This module holds the typed vocabulary of that decision:

* :class:`~.validated_work_release_intent.ValidatedWorkReleaseIntent` is what
  a tech lead PROPOSES. It is untrusted agent input: record ids and the PR
  that rebuilt their work.
* :class:`ValidatedWorkRelease` is what the operator APPROVES. Every named
  record is bound to the exact authority snapshot observed at launch, so an
  approval can only ever release the facts the operator read; a record whose
  evidence or observations moved since then is refused by the store's CAS.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from .validated_work import require_positive, require_text
from .validated_work_commands import ValidatedWorkAuthoritySnapshot
from .validated_work_release_intent import (
    RELEASE_VALIDATED_WORK_ACTION as RELEASE_VALIDATED_WORK_ACTION,
    ValidatedWorkReleaseIntent,
    parse_pr_number as _pr_number,
    parse_record_ids as _record_ids,
)

if TYPE_CHECKING:
    from .tech_lead_artifacts import ProposedTechLeadAction

@dataclass(frozen=True, slots=True)
class ValidatedWorkRelease:
    """An approvable release: each named record bound to its launch authority.

    Everything approval runs is here, so this value IS the operation's
    identity (its proposal's ledger key and reuse check compare it whole):
    the records' snapshots, the PR, and the rationale written into every
    record's resolution.
    """

    superseding_pr_number: int
    #: Sorted by record id, one per record, all of one issue and repository.
    authorities: tuple[ValidatedWorkAuthoritySnapshot, ...]
    #: Why the PR rebuilt the work, as the operator approved it.
    rationale: str

    def __post_init__(self) -> None:
        _pr_number(self.superseding_pr_number, context="release")
        require_text(self.rationale, "release rationale")
        authorities = cast(object, self.authorities)
        if type(authorities) is not tuple or any(
            type(item) is not ValidatedWorkAuthoritySnapshot
            for item in cast(tuple[object, ...], authorities)
        ):
            raise ValueError("a release binds typed authority snapshots")
        _record_ids(self.record_ids, context="release")
        if self.record_ids != tuple(sorted(self.record_ids)):
            raise ValueError("release authorities must be sorted by record id")
        if len({item.issue_number for item in self.authorities}) != 1:
            raise ValueError("a release names the records of exactly one issue")
        if len({item.repo_slug for item in self.authorities}) != 1:
            raise ValueError("a release names the records of exactly one repository")

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(item.record_id for item in self.authorities)

    @property
    def issue_number(self) -> int:
        return self.authorities[0].issue_number

    @property
    def repo_slug(self) -> str:
        return self.authorities[0].repo_slug

    def resolution_reason(self, proposal_issue_number: int) -> str:
        """The durable resolution reason, which is also the operation's identity.

        It names the approved proposal, the PR that rebuilt the work and the
        rationale. The store recognizes a replay only by this exact reason
        (with each record's snapshot), so a retry of the same approved
        proposal replays, and no other proposal over the same records ever
        passes as its replay.
        """
        require_positive(proposal_issue_number, "proposal_issue_number")
        return (
            f"Released on operator approval of tech-lead proposal"
            f" #{proposal_issue_number}: the work was rebuilt in merged PR"
            f" #{self.superseding_pr_number} with rewritten history, so no"
            f" ancestry proof connects it. Rationale: {self.rationale}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "superseding_pr_number": self.superseding_pr_number,
            "authorities": [item.to_dict() for item in self.authorities],
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ValidatedWorkRelease":
        """Parse a persisted release; malformed content fails loudly."""
        unexpected = sorted(set(data) - {"superseding_pr_number", "authorities", "rationale"})
        if unexpected:
            raise ValueError(f"validated-work release has unexpected fields: {unexpected}")
        raw = data.get("authorities")
        if not isinstance(raw, list):
            raise ValueError("validated-work release authorities must be a list")
        return cls(
            superseding_pr_number=data.get("superseding_pr_number"),  # type: ignore[arg-type]
            authorities=tuple(
                ValidatedWorkAuthoritySnapshot.from_dict(cast(dict[str, Any], item))
                for item in cast(list[object], raw)
            ),
            rationale=data.get("rationale"),  # type: ignore[arg-type]
        )


def bind_release(
    intent: ValidatedWorkReleaseIntent,
    *,
    issue_number: int,
    grants: Iterable[ValidatedWorkAuthoritySnapshot],
) -> tuple[ValidatedWorkAuthoritySnapshot, ...] | None:
    """The launch-observed grant of each record an intent names, sorted.

    ``None`` when any named record was not granted for *issue_number* at
    launch: the agent may name only records it was shown, never retarget to
    another issue's, and never have one silently dropped from what it proposed.
    """
    require_positive(issue_number, "issue_number")
    granted = {
        grant.record_id: grant for grant in grants if grant.issue_number == issue_number
    }
    if any(record_id not in granted for record_id in intent.record_ids):
        return None
    return tuple(granted[record_id] for record_id in sorted(intent.record_ids))


#: Binds an intent for an issue to that launch's grants (the launch
#: authority's ``bind_validated_work_release``); None when any record is ungranted.
ReleaseBinder = Callable[
    [int, ValidatedWorkReleaseIntent], "tuple[ValidatedWorkAuthoritySnapshot, ...] | None"
]


def no_release_grants(_issue_number: int, _intent: ValidatedWorkReleaseIntent) -> None:
    """The binder of a caller that granted no releasable record."""
    return None


def bound_release(
    proposed: "ProposedTechLeadAction", binder: ReleaseBinder
) -> ValidatedWorkRelease | None:
    """The launch-bound release a ``release_validated_work`` proposes, else None.

    Raises when the proposal names a record its launch never granted: the
    target-scope check refuses such a decision first, so reaching this is a bug.
    """
    if proposed.action_type != RELEASE_VALIDATED_WORK_ACTION:
        return None
    if proposed.target_number is None or proposed.release is None:
        raise ValueError("release_validated_work requires its target and release")
    authorities = binder(proposed.target_number, proposed.release)
    if authorities is None:
        raise ValueError("release_validated_work names records with no launch authority")
    return ValidatedWorkRelease(
        superseding_pr_number=proposed.release.superseding_pr_number,
        authorities=authorities,
        rationale=proposed.body or "",
    )
