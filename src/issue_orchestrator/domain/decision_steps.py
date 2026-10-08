"""What approving a tech-lead decision does BEYOND its item (#8691).

A ``propose_decision`` or ``resolve_block`` settles one blocked ISSUE: it files
follow-ups, posts the decision, records it as a standing ruling and releases
the item. Real decisions also have consequences elsewhere: porchpin#459 moved
two siblings to another milestone and added notes to two other issue bodies;
#456 closed the proposal it superseded; #327's ruling had to retarget its PR
from ``Closes`` to ``Refs`` and send the PR back for rework. Those were
written as "Before you approve" prose, and the operator did each by hand.

This module is the typed vocabulary of those consequences, the
:class:`DecisionFollowThrough` a decision carries:

* **steps** the engine executes on approval, through the owners that already
  make each write, exactly once and in order. Only what io can do safely is a
  step kind (:class:`DecisionStepKind`); each states its own precondition and
  the orchestrator re-checks every one before the first write;
* **operator steps**: what io cannot do (a workflow-file edit the bot may not
  push, a repository setting), shown to the operator as a checklist, never
  executed and never buried in prose.

The agent writes the steps (untrusted intent, bounded here). Only the
orchestrator binds a PR rework to the PR head it observed at launch
(:attr:`DecisionStep.rework`): the agent can name a PR, never a head.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from .scoped_rework import ReworkRequest

#: Bounds on agent-authored follow-through (untrusted input).
MAX_DECISION_STEPS = 8
MAX_OPERATOR_STEPS = 8
MAX_STEP_TEXT_CHARS = 10_000
MAX_OPERATOR_STEP_CHARS = 1_000
MAX_MILESTONE_CHARS = 200


class DecisionStepKind(StrEnum):
    """The consequences io executes on approval: nothing else is a step."""

    #: Move an issue to an existing open milestone.
    SET_MILESTONE = "set_milestone"
    #: Record text in an issue body's standing-rulings block, which every
    #: session, review and rework on that issue reads (#8141).
    RECORD_RULING = "record_ruling"
    #: Close another tech-lead proposal this decision supersedes.
    CLOSE_SUPERSEDED_PROPOSAL = "close_superseded_proposal"
    #: Rewrite the decided issue's open PR from ``Closes #N`` to ``Refs #N``:
    #: the PR delivers part of the issue, and its merge must not close it.
    RETARGET_PR = "retarget_pr"
    #: Send the decided issue's open PR back for rework with the decision as
    #: its brief. The approval is the person's decision the PR's merge hold
    #: waited for, so that hold comes off; needs-rework goes on.
    REQUEST_PR_REWORK = "request_pr_rework"
    #: Comment on an issue or PR (a link, a pointer to the decision).
    COMMENT = "comment"


#: Kinds that act on a PR (the decided issue's own), not on an issue.
PR_STEP_KINDS: frozenset[DecisionStepKind] = frozenset(
    {DecisionStepKind.RETARGET_PR, DecisionStepKind.REQUEST_PR_REWORK}
)
#: Kinds that need ``text``; every other kind must not carry it.
_TEXT_KINDS = frozenset({DecisionStepKind.RECORD_RULING, DecisionStepKind.COMMENT})
#: Kinds that run only once the decided item is released: the engine refuses
#: a blocked issue's rework, so the rework follows the item's release.
AFTER_RELEASE_KINDS: frozenset[DecisionStepKind] = frozenset({DecisionStepKind.REQUEST_PR_REWORK})


@dataclass(frozen=True)
class DecisionStep:
    """One consequence a decision executes on approval."""

    kind: DecisionStepKind
    #: The issue (or, for a PR kind or a PR comment, the PR) it acts on.
    number: int
    text: str = ""
    milestone: str = ""
    #: A ``comment`` on a PR rather than an issue.
    on_pr: bool = False
    #: A ``request_pr_rework`` bound by the ORCHESTRATOR to the PR head it
    #: observed at launch; the agent never supplies it.
    rework: ReworkRequest | None = None

    def __post_init__(self) -> None:
        kind = cast(object, self.kind)
        _require(isinstance(kind, DecisionStepKind), f"a decision step's kind must be a DecisionStepKind, got {kind!r}")
        number = cast(object, self.number)
        _require(isinstance(number, int) and not isinstance(number, bool) and number > 0,
                 f"{self.kind.value} step needs a positive number, got {number!r}")
        text = cast(object, self.text)
        _require(isinstance(text, str) and len(text) <= MAX_STEP_TEXT_CHARS,
                 f"{self.kind.value} step text must be at most {MAX_STEP_TEXT_CHARS} characters")
        _require(bool(self.text.strip()) == (self.kind in _TEXT_KINDS),
                 f"{self.kind.value} step {'requires' if self.kind in _TEXT_KINDS else 'takes no'} text")
        milestone = cast(object, self.milestone)
        _require(isinstance(milestone, str) and len(milestone) <= MAX_MILESTONE_CHARS,
                 f"milestone must be at most {MAX_MILESTONE_CHARS} characters")
        _require(bool(self.milestone.strip()) == (self.kind is DecisionStepKind.SET_MILESTONE),
                 f"{self.kind.value} step {'requires' if self.kind is DecisionStepKind.SET_MILESTONE else 'takes no'}"
                 " milestone")
        _require(not self.on_pr or self.kind is DecisionStepKind.COMMENT,
                 f"{self.kind.value} step cannot set on_pr: only a comment chooses its surface")
        rework = cast(object, self.rework)
        _require(rework is None or (self.kind is DecisionStepKind.REQUEST_PR_REWORK and isinstance(rework, ReworkRequest)),
                 f"{self.kind.value} step cannot carry a rework binding")
        if isinstance(rework, ReworkRequest):
            _require(rework.target.pr_number == self.number,
                     f"request_pr_rework of PR #{self.number} is bound to PR #{rework.target.pr_number}")

    @property
    def acts_on_pr(self) -> bool:
        return self.kind in PR_STEP_KINDS or self.on_pr

    @property
    def after_release(self) -> bool:
        return self.kind in AFTER_RELEASE_KINDS

    def describe(self, subject: int) -> str:
        """What the step does, in the operator's words."""
        number = self.number
        match self.kind:
            case DecisionStepKind.SET_MILESTONE:
                return f"Moves #{number} to milestone `{self.milestone}`."
            case DecisionStepKind.RECORD_RULING:
                return f"Records this in #{number}'s body (standing rulings block): {_preview(self.text)}"
            case DecisionStepKind.CLOSE_SUPERSEDED_PROPOSAL:
                return f"Closes proposal #{number}, which this decision supersedes."
            case DecisionStepKind.RETARGET_PR:
                return f"Rewrites PR #{number}'s `Closes #{subject}` to `Refs #{subject}`: its merge leaves #{subject} open."
            case DecisionStepKind.REQUEST_PR_REWORK:
                return (f"Sends PR #{number} back for rework with this decision as its brief, once #{subject}"
                        " is released (its observed needs-human comes off, needs-rework goes on).")
            case DecisionStepKind.COMMENT:
                return f"Comments on {'PR' if self.on_pr else 'issue'} #{number}: {_preview(self.text)}"
        raise AssertionError(self.kind)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind.value, "number": self.number}
        if self.text:
            payload["text"] = self.text
        if self.milestone:
            payload["milestone"] = self.milestone
        if self.on_pr:
            payload["on_pr"] = True
        if self.rework is not None:
            payload["rework"] = self.rework.to_dict()
        return payload

    @classmethod
    def from_agent(cls, data: Any, *, context: str) -> "DecisionStep":
        """Parse one agent-written step: never a rework binding."""
        if not isinstance(data, dict):
            raise ValueError(f"{context} must be an object, got {type(data).__name__}")
        item = cast(dict[str, Any], data)
        unknown = sorted(set(item) - {"kind", "number", "text", "milestone", "on_pr"})
        if unknown:
            raise ValueError(f"{context} has unknown fields {unknown}")
        try:
            kind = DecisionStepKind(item.get("kind"))
        except ValueError:
            raise ValueError(
                f"{context} kind must be one of {sorted(k.value for k in DecisionStepKind)},"
                f" got {item.get('kind')!r}: anything else is an operator step"
            ) from None
        on_pr = item.get("on_pr", False)
        if not isinstance(on_pr, bool):
            raise ValueError(f"{context} on_pr must be a boolean")
        try:
            return cls(kind=kind, number=item.get("number"),  # type: ignore[arg-type]
                       text=_str(item.get("text"), f"{context} text"),
                       milestone=_str(item.get("milestone"), f"{context} milestone"), on_pr=on_pr)
        except ValueError as error:
            raise ValueError(f"{context}: {error}") from None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DecisionStep":
        """A stored step (the orchestrator's own record)."""
        rework = data.get("rework")
        return cls(
            kind=DecisionStepKind(data["kind"]), number=int(data["number"]),
            text=str(data.get("text", "")), milestone=str(data.get("milestone", "")),
            on_pr=bool(data.get("on_pr", False)),
            rework=ReworkRequest.from_dict(cast(dict[str, Any], rework)) if rework is not None else None,
        )


@dataclass(frozen=True)
class DecisionFollowThrough:
    """A decision's consequences beyond its item: executed steps, and the
    operator's own checklist of what io cannot do."""

    steps: tuple[DecisionStep, ...] = ()
    operator_steps: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        steps = cast(object, self.steps)
        _require(isinstance(steps, tuple) and all(isinstance(s, DecisionStep) for s in cast(tuple[object, ...], steps)),
                 "follow-through steps must be DecisionStep items")
        _require(len(self.steps) <= MAX_DECISION_STEPS, f"at most {MAX_DECISION_STEPS} steps, got {len(self.steps)}")
        operator = cast(object, self.operator_steps)
        _require(isinstance(operator, tuple) and all(
            isinstance(s, str) and s.strip() and len(s) <= MAX_OPERATOR_STEP_CHARS
            for s in cast(tuple[object, ...], operator)),
            f"operator steps must be non-empty strings of at most {MAX_OPERATOR_STEP_CHARS} characters")
        _require(len(self.operator_steps) <= MAX_OPERATOR_STEPS,
                 f"at most {MAX_OPERATOR_STEPS} operator steps, got {len(self.operator_steps)}")
        for kind in PR_STEP_KINDS:
            prs = [step.number for step in self.steps if step.kind is kind]
            _require(len(prs) <= 1, f"at most one {kind.value} step, got PRs {prs}")
        closes = [step.number for step in self.steps if step.kind is DecisionStepKind.CLOSE_SUPERSEDED_PROPOSAL]
        _require(len(closes) == len(set(closes)), f"a proposal is closed once, got {closes}")

    def __bool__(self) -> bool:
        return bool(self.steps or self.operator_steps)

    @property
    def before_release(self) -> tuple[tuple[int, DecisionStep], ...]:
        """(1-based index, step) of the steps that run before the item is released, in order."""
        return tuple((index, step) for index, step in enumerate(self.steps, start=1) if not step.after_release)

    @property
    def after_release(self) -> tuple[tuple[int, DecisionStep], ...]:
        return tuple((index, step) for index, step in enumerate(self.steps, start=1) if step.after_release)

    def validate_for(self, subject: int, *, proposal_issue_number: int | None = None) -> None:
        """Rules that need the decided item: a PR step acts on ITS PR (checked
        when executed), and a decision never closes the item or itself."""
        for step in self.steps:
            if step.kind is DecisionStepKind.CLOSE_SUPERSEDED_PROPOSAL:
                _require(step.number not in (subject, proposal_issue_number),
                         f"close_superseded_proposal #{step.number} would close the decision's own item or proposal")
            if step.rework is not None:
                _require(step.rework.target.issue_number == subject,
                         f"PR #{step.number} belongs to #{step.rework.target.issue_number}, not #{subject}")

    def with_steps(self, steps: Sequence[DecisionStep]) -> "DecisionFollowThrough":
        return DecisionFollowThrough(steps=tuple(steps), operator_steps=self.operator_steps)

    def to_dict(self) -> dict[str, Any]:
        return {"steps": [step.to_dict() for step in self.steps], "operator_steps": list(self.operator_steps)}

    def identity(self) -> dict[str, Any] | None:
        """What approving it would DO, for proposal identity: the steps and the
        checklist, a PR rework by its bound head (never its run-specific report
        text). None when empty, so a decision without steps keeps its old key."""
        if not self:
            return None
        steps = []
        for step in self.steps:
            payload = step.to_dict()
            if step.rework is not None:
                payload["rework"] = step.rework.target.head_sha
            steps.append(payload)
        return {"steps": steps, "operator_steps": list(self.operator_steps)}

    @classmethod
    def from_agent(cls, steps: Any, operator_steps: Any, *, context: str) -> "DecisionFollowThrough":
        if steps is not None and not isinstance(steps, list):
            raise ValueError(f"{context} steps must be a list")
        if operator_steps is not None and not isinstance(operator_steps, list):
            raise ValueError(f"{context} operator_steps must be a list")
        raw_steps = cast(list[Any], steps or [])
        if len(raw_steps) > MAX_DECISION_STEPS:
            raise ValueError(f"{context} has {len(raw_steps)} steps (max {MAX_DECISION_STEPS})")
        parsed = tuple(
            DecisionStep.from_agent(item, context=f"{context} step #{index}")
            for index, item in enumerate(raw_steps, start=1)
        )
        operator = tuple(
            _str(item, f"{context} operator step #{index}").strip()
            for index, item in enumerate(cast(list[Any], operator_steps or []), start=1)
        )
        try:
            return cls(steps=parsed, operator_steps=operator)
        except ValueError as error:
            raise ValueError(f"{context}: {error}") from None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DecisionFollowThrough":
        return cls(
            steps=tuple(DecisionStep.from_dict(item) for item in cast(list[Any], data.get("steps", []))),
            operator_steps=tuple(str(item) for item in cast(list[Any], data.get("operator_steps", []))),
        )


#: How a tech lead writes a decision's consequences beyond its item: restated
#: in the tech-lead prompt and in every health review's triage instructions.
DECISION_STEPS_PROMPT_RULES = """- **A decision's consequences beyond its item are typed `steps`, never prose.**
  `propose_decision` and `resolve_block` take optional `steps` (at most 8; the
  engine runs each once, in order, when the operator approves) and
  `operator_steps` (strings, at most 8). Never write "Before you approve" or
  "the maintainer then ..." chores in a body: if io can do it, it is a step;
  if not, it is an operator step. The step kinds (`number` is the issue or PR):
  `{"kind": "set_milestone", "number": N, "milestone": "<open milestone title>"}`,
  `{"kind": "record_ruling", "number": N, "text": "..."}` (a block in issue N's
  body, which every session on N reads: a delivery plan, a note under an
  acceptance bullet), `{"kind": "close_superseded_proposal", "number": N}` (an
  open tech-lead proposal this decision replaces), `{"kind": "retarget_pr",
  "number": PR}` (the decided issue's PR goes from `Closes #N` to `Refs #N`),
  `{"kind": "request_pr_rework", "number": PR}` (the decided issue's PR, as
  listed in `scoped-rework-targets.json`, goes back for rework with the
  decision as its brief; its merge-decision `needs-human` comes off; it runs
  after the item is released), and `{"kind": "comment", "number": N, "text":
  "...", "on_pr": false}` (a link or pointer). Anything else (a workflow-file
  edit the bot may not push, a repository setting, a credential) goes in
  `operator_steps`, shown to the operator as a checklist. The engine re-checks
  every step before the first write and applies none if one no longer holds.
  A `resolve_block` with steps always waits for the operator's approval.
"""


def follow_through_section(follow_through: DecisionFollowThrough, *, subject: int) -> str:
    """The proposal-body section: what approval also executes, and the
    operator's own checklist (#8691)."""
    if not follow_through:
        return ""
    parts: list[str] = []
    if follow_through.steps:
        listed = "\n".join(
            f"{index}. {step.describe(subject)}" for index, step in enumerate(follow_through.steps, start=1)
        )
        parts.append(
            "### Steps approval executes\n\n"
            "Each runs once, in this order, through the engine's own owners. Every"
            " precondition is re-checked before the first write: if one no longer holds,"
            " nothing is applied and this proposal closes saying why.\n\n" + listed + "\n"
        )
    if follow_through.operator_steps:
        checklist = "\n".join(f"- [ ] {item}" for item in follow_through.operator_steps)
        parts.append(f"{OPERATOR_CHECKLIST_HEADING}\n\n" + checklist + "\n")
    return "\n" + "\n".join(parts)


#: The proposal-body heading of the operator's own checklist.
OPERATOR_CHECKLIST_HEADING = "### You do by hand (io cannot)"

#: Every applied (or write-time refused) step's marker starts with this.
DECISION_STEP_MARKER_PREFIX = "<!-- io:decision-step:"


def step_marker(decision_key: str, index: int) -> str:
    """The durable marker of one applied step, on the decision's proposal."""
    return f"{DECISION_STEP_MARKER_PREFIX}{decision_key}:{index} -->"


def _preview(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _str(value: Any, context: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{context} must be a string")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
