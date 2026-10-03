"""A tech lead deciding a ``needs-human`` WORK block itself (#7658).

``resolve_block`` is the charter action that lets an operator hand the tech
lead the decisions it can make from the item's own record: answer an agent's
question from the issue's spec, ADRs and CUJs; decide a split, filing the
children with their dependency edges; or lift a block shown to be stale or
false. This module is the typed vocabulary of that decision and the rules no
agent text can bypass:

* **Only work blocks.** :func:`is_resolvable_work_block` is the one decision
  of which ``needs-human`` causes a decision may discharge: an agent's own question
  (``agent_completion``) and the engine giving up on the item
  (``session_lifecycle``: a session that ended without a completion, a stuck
  sweep that spent its budget). A merge escalation is a merge gate the operator
  owns by design; a tech-lead hand-over, a claim quarantine, a validated-work
  disposition and a parked action each have their own owner. None is ever
  resolvable, and a ``needs-human`` with no recorded cause is the operator's
  own label, which nothing second-guesses.
* **Never human-only work.** :func:`human_only_work` screens the item's text
  and the decision's own text for work only a person can do: credentials,
  external accounts, infrastructure provisioning, money, legal. A match always
  hands the item over; the screen is deliberately conservative, so a false
  match escalates and never resolves.
* **Once per cause.** A resolution posts a durable marker per cause it
  discharged (:func:`cause_marker`). Finding one from an EARLIER decision means
  the operator (or the agent) put the block back after the tech lead lifted it:
  that cause is the operator's from then on (:func:`prior_resolutions`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from .dependencies import EDGE_DIRECTIVE_PATTERN
from .human_block import HumanHoldScope, NeedsHumanCause

#: The tech-lead action type this module types (one charter row, one ceiling).
RESOLVE_BLOCK_ACTION = "resolve_block"

def is_resolvable_work_block(cause: NeedsHumanCause) -> bool:
    """THE one decision of whether a needs-human cause is a WORK block a
    resolution may decide (#7658).

    Only a cause that holds the item's WORK (#7678's :class:`HumanHoldScope`)
    can be resolvable: a merge decision holds a PR's merge, which the operator
    makes by design. Among work holds, an agent's own question and the engine
    giving up ask nothing only a person can answer; every other cause has its
    own owner (a tech-lead hand-over, a quarantine, a disposition, a parked
    action, a merge escalation), so none of those is resolvable. Every check
    (the decision's validation, the shared block's owner) asks this function.
    """
    return cause.scope is HumanHoldScope.WORK and cause in (
        NeedsHumanCause.AGENT_COMPLETION, NeedsHumanCause.SESSION_LIFECYCLE,
    )


#: The causes :func:`is_resolvable_work_block` admits, for display and prompts.
RESOLVABLE_CAUSES: frozenset[NeedsHumanCause] = frozenset(
    cause for cause in NeedsHumanCause if is_resolvable_work_block(cause)
)

#: Bounds on agent-authored resolution content (untrusted input).
MAX_RESOLUTION_CHILDREN = 3
MAX_RESOLUTION_EVIDENCE = 10
MAX_RESOLUTION_TEXT_CHARS = 20_000
MAX_RESOLUTION_TITLE_CHARS = 300
#: A child becomes a GitHub issue, whose title GitHub caps at 256 characters.
MAX_CHILD_TITLE_CHARS = 256


class ResolutionKind(StrEnum):
    """What the tech lead decided in the operator's stead."""

    #: Answered the agent's question from the item's spec, ADRs and CUJs.
    ANSWER = "answer"
    #: Decided a split: the children are filed and the parent narrowed or closed.
    SPLIT = "split"
    #: The block is stale or false (the engine gave up on something that holds
    #: no longer, or never did): lift it and requeue the item.
    LIFT = "lift"


class ParentDisposition(StrEnum):
    """What a split does to the item it splits."""

    #: The item keeps the slice it has and is requeued to finish it.
    NARROW = "narrow"
    #: Everything moved into the children; the item closes.
    CLOSE = "close"


class ChildEdge(StrEnum):
    """How a split child waits on its predecessor (issue-dependency-stacking).

    ``depends_on`` waits for the predecessor to close; ``stack_after`` builds
    on its unmerged, agent-reviewed branch and merges after it.
    """

    DEPENDS_ON = "depends_on"
    STACK_AFTER = "stack_after"

    @property
    def directive(self) -> str:
        return "Depends-on" if self is ChildEdge.DEPENDS_ON else "Stack-after"


#: A child's predecessor: the item being split, or an EARLIER child (1-based).
PARENT = "parent"


@dataclass(frozen=True)
class ResolutionChild:
    """One issue a split files, with at most one predecessor edge.

    One edge per child keeps a stack unambiguous (a single base branch); a
    chain or a fan-out is expressed by pointing each child at its own
    predecessor.
    """

    title: str
    body: str
    edge: ChildEdge | None = None
    #: ``"parent"`` or the 1-based index of an earlier child; None without an edge.
    after: str | int | None = None

    def __post_init__(self) -> None:
        _require_text(self.title, "child title", MAX_CHILD_TITLE_CHARS)
        _require_text(self.body, "child body", MAX_RESOLUTION_TEXT_CHARS)
        if (self.edge is None) != (self.after is None):
            raise ValueError("a split child names both its edge and what it comes after, or neither")
        after = cast(object, self.after)
        if after is not None and after != PARENT and (
            isinstance(after, bool) or not isinstance(after, int) or after < 1
        ):
            raise ValueError(f"a split child comes after 'parent' or an earlier child's index, not {after!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "edge": self.edge.value if self.edge is not None else None,
            "after": self.after,
        }

    @classmethod
    def from_mapping(cls, data: Any, *, context: str) -> "ResolutionChild":
        if not isinstance(data, Mapping):
            raise ValueError(f"{context} must be an object")
        edge = data.get("edge")
        try:
            parsed_edge = ChildEdge(edge) if edge is not None else None
        except ValueError:
            raise ValueError(f"{context} edge must be one of {[e.value for e in ChildEdge]}, got {edge!r}") from None
        return cls(
            title=_text(data.get("title"), f"{context} title"),
            body=_text(data.get("body"), f"{context} body"),
            edge=parsed_edge,
            after=data.get("after"),
        )


@dataclass(frozen=True)
class BlockResolution:
    """The decision a ``resolve_block`` carries, recorded with its op.

    ``causes`` are the work-block causes the decision discharges, named by the
    tech lead and re-checked against the shared block's own records when it is
    applied: a cause not on record then is refused, never assumed.
    ``evidence`` cites what the decision rests on (the spec, an ADR, a CUJ, the
    agent's own report), and is posted with it.
    """

    kind: ResolutionKind
    causes: frozenset[NeedsHumanCause]
    title: str
    body: str
    evidence: tuple[str, ...]
    children: tuple[ResolutionChild, ...] = ()
    parent: ParentDisposition | None = None

    def __post_init__(self) -> None:
        kind = cast(object, self.kind)
        if not isinstance(kind, ResolutionKind):
            raise ValueError("resolution kind must be a ResolutionKind")
        causes = cast(object, self.causes)
        if not isinstance(causes, frozenset) or not causes:
            raise ValueError("a resolution names at least one cause it discharges")
        outside = sorted(
            str(getattr(cause, "value", cause))
            for cause in causes
            if not isinstance(cause, NeedsHumanCause) or not is_resolvable_work_block(cause)
        )
        if outside:
            raise ValueError(
                f"a resolution discharges only work blocks"
                f" ({sorted(c.value for c in RESOLVABLE_CAUSES)}); {outside} are not resolvable"
            )
        if self.kind in (ResolutionKind.ANSWER, ResolutionKind.SPLIT) and (
            NeedsHumanCause.AGENT_COMPLETION not in self.causes
        ):
            raise ValueError(f"an {self.kind.value} resolution answers the agent's question (agent_completion)")
        _require_text(self.title, "resolution title", MAX_RESOLUTION_TITLE_CHARS)
        _require_text(self.body, "resolution body", MAX_RESOLUTION_TEXT_CHARS)
        evidence = cast(object, self.evidence)
        if (
            not isinstance(evidence, tuple)
            or not evidence
            or len(evidence) > MAX_RESOLUTION_EVIDENCE
            or any(not isinstance(item, str) or not item.strip() for item in evidence)
        ):
            raise ValueError(
                f"a resolution cites 1-{MAX_RESOLUTION_EVIDENCE} non-empty evidence references"
            )
        self._validate_split()

    def _validate_split(self) -> None:
        children = cast(object, self.children)
        if not isinstance(children, tuple) or any(
            not isinstance(child, ResolutionChild) for child in children
        ):
            raise ValueError("resolution children must be ResolutionChild items")
        if self.kind is not ResolutionKind.SPLIT:
            if self.children or self.parent is not None:
                raise ValueError("only a split files children or disposes of its parent")
            return
        if not 1 <= len(self.children) <= MAX_RESOLUTION_CHILDREN:
            raise ValueError(f"a split files 1-{MAX_RESOLUTION_CHILDREN} children")
        if not isinstance(cast(object, self.parent), ParentDisposition):
            raise ValueError("a split says whether the parent is narrowed or closed")
        for index, child in enumerate(self.children, start=1):
            if EDGE_DIRECTIVE_PATTERN.search(child.body):
                raise ValueError(
                    f"split child {index}'s body carries a dependency line: declare its one"
                    " edge with edge/after instead, so the filed graph is exactly the decided one"
                )
            if isinstance(child.after, int) and child.after >= index:
                raise ValueError(f"split child {index} may only come after an EARLIER child")
            if child.after == PARENT and self.parent is ParentDisposition.CLOSE:
                raise ValueError(
                    f"split child {index} waits on the parent, which the split closes"
                )

    @property
    def texts(self) -> tuple[str, ...]:
        """Every word the decision carries, for the human-only screen: a split
        child that files a person's task is as human-only as the decision."""
        return (
            self.title, self.body, *self.evidence,
            *(text for child in self.children for text in (child.title, child.body)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "causes": sorted(cause.value for cause in self.causes),
            "title": self.title,
            "body": self.body,
            "evidence": list(self.evidence),
            "children": [child.to_dict() for child in self.children],
            "parent": self.parent.value if self.parent is not None else None,
        }

    @classmethod
    def from_mapping(cls, data: Any, *, context: str) -> "BlockResolution":
        """Parse agent-authored (or stored) content; any violation raises."""
        if not isinstance(data, Mapping):
            raise ValueError(f"{context} resolution must be an object")
        kind = data.get("kind")
        try:
            parsed_kind = ResolutionKind(kind)
        except ValueError:
            raise ValueError(
                f"{context} resolution kind must be one of {[k.value for k in ResolutionKind]}, got {kind!r}"
            ) from None
        raw_causes = data.get("causes")
        if not isinstance(raw_causes, list):
            raise ValueError(f"{context} resolution causes must be a list")
        try:
            causes = frozenset(NeedsHumanCause(value) for value in raw_causes)
        except ValueError:
            raise ValueError(
                f"{context} resolution causes must name needs-human causes, got {raw_causes!r}"
            ) from None
        raw_evidence = data.get("evidence")
        if not isinstance(raw_evidence, list):
            raise ValueError(f"{context} resolution evidence must be a list")
        raw_children = data.get("children") or []
        if not isinstance(raw_children, list):
            raise ValueError(f"{context} resolution children must be a list")
        parent = data.get("parent")
        try:
            parsed_parent = ParentDisposition(parent) if parent is not None else None
        except ValueError:
            raise ValueError(
                f"{context} resolution parent must be one of {[p.value for p in ParentDisposition]}"
            ) from None
        return cls(
            kind=parsed_kind,
            causes=causes,
            title=_text(data.get("title"), f"{context} resolution title"),
            body=_text(data.get("body"), f"{context} resolution body"),
            evidence=tuple(str(item) for item in raw_evidence),
            children=tuple(
                ResolutionChild.from_mapping(item, context=f"{context} child {index}")
                for index, item in enumerate(raw_children, start=1)
            ),
            parent=parsed_parent,
        )


# -- human-only work ---------------------------------------------------------


class HumanOnlyWork(StrEnum):
    """Work only a person can do: a resolution of it always escalates."""

    CREDENTIALS = "credentials"
    EXTERNAL_ACCOUNT = "external_account"
    INFRASTRUCTURE_PROVISIONING = "infrastructure_provisioning"
    MONEY = "money"
    LEGAL = "legal"


#: Phrases that name human-only work. Strong phrases only, calibrated against
#: porchpin's issue corpus: a bare "credential" or "provision" names code as
#: often as it names a person's task. A false match escalates (safe); a miss
#: is caught by the cause typing and the hand-over marker.
_HUMAN_ONLY_PHRASES: Mapping[HumanOnlyWork, tuple[str, ...]] = {
    HumanOnlyWork.CREDENTIALS: (
        r"\bapi (?:key|token)s?\b",
        r"\bdeploy(?:ment)? tokens?\b",
        r"\bservice tokens?\b",
        r"\bpersonal access tokens?\b",
        r"\b(?:add|set|create|rotate) (?:the |a |an )?(?:[\w-]+ )?secrets?\b",
        r"\bpassword\b",
    ),
    HumanOnlyWork.EXTERNAL_ACCOUNT: (
        r"\b(?:create|open|sign up for|register) (?:an?|the) (?:[\w-]+ ){0,3}account\b",
        r"\baccount (?:action|access|provisioning|setup)\b",
        r"\bmaintainer account work\b",
        r"\bgrant (?:me |us |the bot )?access\b",
    ),
    HumanOnlyWork.INFRASTRUCTURE_PROVISIONING: (
        r"\bprovisioning checklist\b",
        r"\b(?:human|manual|maintainer) provisioning\b",
        r"\bdns records?\b",
        r"\bregister (?:a|the) domain\b",
    ),
    HumanOnlyWork.MONEY: (
        r"\bbilling\b",
        r"\bpayment method\b",
        r"\bcredit card\b",
        r"\bpaid plan\b",
        r"\bpay for\b",
        r"\binvoice\b",
    ),
    HumanOnlyWork.LEGAL: (
        r"\blegal (?:review|sign-?off|approval|advice)\b",
        r"\blawyer\b",
        r"\battorney\b",
        r"\bcounsel\b",
        r"\bsign (?:the|a) contract\b",
    ),
}

_COMPILED_HUMAN_ONLY: tuple[tuple[HumanOnlyWork, re.Pattern[str]], ...] = tuple(
    (category, re.compile(pattern, re.IGNORECASE))
    for category, patterns in _HUMAN_ONLY_PHRASES.items()
    for pattern in patterns
)


@dataclass(frozen=True)
class HumanOnlyMatch:
    category: HumanOnlyWork
    phrase: str

    def describe(self) -> str:
        return f"{self.category.value} ({self.phrase!r})"


def human_only_work(texts: Iterable[str | None]) -> HumanOnlyMatch | None:
    """The first human-only work named in *texts*, else None."""
    for text in texts:
        if not text:
            continue
        for category, pattern in _COMPILED_HUMAN_ONLY:
            match = pattern.search(text)
            if match is not None:
                return HumanOnlyMatch(category, match.group(0))
    return None


# -- durable markers -----------------------------------------------------------

#: Every resolution marker starts with this: a comment read for them asks the
#: host for exactly the comments that carry one.
RESOLUTION_MARKER_PREFIX = "<!-- io:resolve-block:"

_CAUSE_MARKER = re.compile(
    r"<!-- io:resolve-block:cause=(?P<cause>[a-z_]+):decision=(?P<decision>[^ ]+) -->"
)


def cause_marker(cause: NeedsHumanCause, decision_id: str) -> str:
    """The durable record that *decision_id* discharged *cause* on an item."""
    if not decision_id or " " in decision_id:
        raise ValueError("a resolution marker names its decision without spaces")
    return f"<!-- io:resolve-block:cause={cause.value}:decision={decision_id} -->"


def decision_marker(decision_id: str) -> str:
    """Create-once marker of a resolution's comment on its item."""
    return f"<!-- io:resolve-block:comment:decision={decision_id} -->"


def discharge_marker(decision_id: str) -> str:
    """Create-once marker of the comment recording a COMMITTED discharge."""
    return f"<!-- io:resolve-block:discharged:decision={decision_id} -->"


def child_marker(decision_id: str, index: int) -> str:
    """Create-once marker of a split's child issue."""
    return f"<!-- io:resolve-block:child={index}:decision={decision_id} -->"


def prior_resolutions(comment_bodies: Iterable[str]) -> dict[NeedsHumanCause, frozenset[str]]:
    """Every decision that has discharged each cause on an item, from its comments."""
    found: dict[NeedsHumanCause, set[str]] = {}
    for body in comment_bodies:
        for match in _CAUSE_MARKER.finditer(body or ""):
            try:
                cause = NeedsHumanCause(match.group("cause"))
            except ValueError:
                continue  # another tool's marker shape; not a resolution
            found.setdefault(cause, set()).add(match.group("decision"))
    return {cause: frozenset(ids) for cause, ids in found.items()}


def _text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _require_text(value: object, context: str, limit: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{context} must be non-empty and at most {limit} characters")
