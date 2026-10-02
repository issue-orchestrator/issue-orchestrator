"""The tech-lead charter: what each role may do unattended (#7329, #7330).

The tech lead plays a fixed set of ROLES. Per role, the operator sets two dials
in ``tech_lead.charter``:

* ``depth`` — how deep a remedy the role may drive:
  ``workaround`` < ``fix`` < ``restructure``.
* ``authority`` — whether a remedy inside that depth runs unattended
  (``execute``) or waits for the operator (``propose``).

This module is the ONE owner of two questions, and both are pure:

1. **Which role and depth does an action need?** :data:`CHARTER_ACTION_CLASSES`
   maps every action kind the tech lead can take to a :class:`CharterActionClass`.
   The ORCHESTRATOR derives it from the action's type; nothing the agent writes
   in its decision can name a role or a depth, so an agent can never route an
   action through a more permissive role (#7329 comment, point 5).
2. **What does the charter allow for it?** :func:`decide_charter` returns a
   :class:`CharterVerdict`: executed, proposed (awaiting approval), advice only,
   or refused as destructive — with a stable :class:`CharterReason` code and a
   plain-language reason an operator can read.

Principles the table encodes (#7329):

* Roles limit what the tech lead may DO unattended, never what it may NOTICE or
  ADVISE. Diagnosis comments and pattern observations are
  :attr:`CharterBinding.ADVISORY`: the charter never downgrades them.
* Routing to a human (``escalate_to_human``) and recording a completed
  investigation's disposition (``defer_to_tracker``) are safety floors that
  always execute (:attr:`CharterBinding.FLOOR`).
* Destructive actions — reset from scratch, closing a PR, deleting a branch —
  never run unattended, whatever any dial says
  (:attr:`CharterBinding.DESTRUCTIVE`).
* A role that is disabled, or an action deeper than the role's depth, is
  recorded as advice, not asked about.

The per-action ``tech_lead.authority.*`` modes and ``tech_lead.findings.promote``
predate the charter. They are the per-action CEILING the caller passes in
(``action_ceiling``), so the charter composes with them instead of running a
parallel policy. The default charter is permissive for every named role, which
makes the ceiling the only thing that decides — exactly today's behaviour until
an operator edits a dial.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class CharterRole(str, Enum):
    """The roles the tech lead plays (#7329)."""

    FLOW = "flow"
    REVIEW_LOOP = "review_loop"
    ABSTRACTION = "abstraction"
    PLATFORM = "platform"
    INTAKE = "intake"
    LEARNING = "learning"
    #: Catch-all for an action that fits no named role. Its volume is the signal
    #: that a role is missing, so it is recorded like any other role.
    GENERAL = "general"


class CharterDepth(str, Enum):
    """How deep a remedy may go. Ordered: see :attr:`rank`."""

    WORKAROUND = "workaround"
    FIX = "fix"
    RESTRUCTURE = "restructure"

    @property
    def rank(self) -> int:
        return _DEPTH_RANK[self]

    def covers(self, required: "CharterDepth") -> bool:
        """True when a role allowed this depth may take an action needing *required*."""
        return self.rank >= required.rank


_DEPTH_RANK: Mapping[CharterDepth, int] = {
    CharterDepth.WORKAROUND: 0,
    CharterDepth.FIX: 1,
    CharterDepth.RESTRUCTURE: 2,
}


#: Plain-language verb for each depth, for operator-facing reasons.
_VERB: Mapping[CharterDepth, str] = {
    CharterDepth.WORKAROUND: "apply workarounds",
    CharterDepth.FIX: "fix",
    CharterDepth.RESTRUCTURE: "restructure",
}


class CharterAuthority(str, Enum):
    """Whether an in-charter action runs unattended or waits for the operator."""

    PROPOSE = "propose"
    EXECUTE = "execute"


@dataclass(frozen=True)
class RoleCharter:
    """One role's dials. ``enabled=False`` means the role does nothing on its own."""

    depth: CharterDepth
    authority: CharterAuthority
    enabled: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "depth": self.depth.value,
            "authority": self.authority.value,
        }


#: Named roles default to the widest dials, so the per-action ceilings decide
#: alone — today's behaviour. ``general`` defaults narrow: anything that lands
#: there is visible and approvable, never silently executed (#7329 comment).
DEFAULT_ROLE_CHARTERS: Mapping[CharterRole, RoleCharter] = {
    **{
        role: RoleCharter(depth=CharterDepth.RESTRUCTURE, authority=CharterAuthority.EXECUTE)
        for role in CharterRole
        if role is not CharterRole.GENERAL
    },
    CharterRole.GENERAL: RoleCharter(
        depth=CharterDepth.WORKAROUND, authority=CharterAuthority.PROPOSE
    ),
}


@dataclass(frozen=True)
class TechLeadCharter:
    """Every role's dials; always complete (one entry per :class:`CharterRole`)."""

    roles: Mapping[CharterRole, RoleCharter]

    def __post_init__(self) -> None:
        missing = [role.value for role in CharterRole if role not in self.roles]
        if missing:
            raise ValueError(f"tech_lead charter is missing role(s): {missing}")

    @classmethod
    def default(cls) -> "TechLeadCharter":
        return cls(roles=dict(DEFAULT_ROLE_CHARTERS))

    def for_role(self, role: CharterRole) -> RoleCharter:
        return self.roles[role]

    def to_dict(self) -> dict[str, dict[str, object]]:
        return {role.value: self.roles[role].to_dict() for role in CharterRole}


class CharterBinding(str, Enum):
    """How the charter binds an action kind."""

    #: Always executes; no dial can remove it (a safety surface).
    FLOOR = "floor"
    #: Noticing or advising: role and depth never restrict it. Only its
    #: per-action ceiling decides execute versus a would-have-done record.
    ADVISORY = "advisory"
    #: Restricted by role and depth; under ``propose`` it goes through the
    #: existing per-instance approval gate (a gated proposal issue).
    APPROVABLE = "approvable"
    #: Like APPROVABLE, but never runs unattended whatever the dials say.
    DESTRUCTIVE = "destructive"
    #: The operator's own call (#7593): always filed for approval, never
    #: executed by the tech lead, and - like a floor - never restricted by role
    #: or depth, because putting a decision in front of the operator is a
    #: hand-over, not an act.
    OPERATOR_DECISION = "operator_decision"


@dataclass(frozen=True)
class CharterActionClass:
    """The role and depth an action kind needs, and how the charter binds it."""

    role: CharterRole
    depth: CharterDepth
    binding: CharterBinding
    #: Plain words for the reason text ("reset and retry from scratch").
    description: str


#: The kind an orchestrator-originated finding promotion is decided under.
PROMOTE_FINDING_KIND = "promote_finding"

#: Tech-lead action kinds the ORCHESTRATOR originates (not agent-proposed).
ORCHESTRATOR_CHARTER_ACTION_KINDS: tuple[str, ...] = (PROMOTE_FINDING_KIND,)

_W, _F = CharterDepth.WORKAROUND, CharterDepth.FIX

#: THE mapping owner. Every action kind the tech lead can take — each
#: agent-proposable ``action_type`` plus every orchestrator-originated kind —
#: has exactly one row. A kind with no row fails loudly in
#: :func:`classify_charter_action` and fails the completeness test, so a new
#: action type cannot ship unclassified. A kind that fits no named role maps to
#: :attr:`CharterRole.GENERAL` explicitly.
CHARTER_ACTION_CLASSES: Mapping[str, CharterActionClass] = {
    "post_comment": CharterActionClass(
        CharterRole.FLOW, _W, CharterBinding.ADVISORY, "post a diagnosis comment"
    ),
    "flag_pattern": CharterActionClass(
        CharterRole.LEARNING, _W, CharterBinding.ADVISORY,
        "record a recurring pattern in its case file",
    ),
    "escalate_to_human": CharterActionClass(
        CharterRole.FLOW, _W, CharterBinding.FLOOR, "escalate to a human"
    ),
    "defer_to_tracker": CharterActionClass(
        CharterRole.FLOW, _W, CharterBinding.FLOOR,
        "defer a diagnosed issue to its open tracker",
    ),
    "kill_hung_session": CharterActionClass(
        CharterRole.FLOW, _W, CharterBinding.APPROVABLE, "terminate a hung session"
    ),
    "recover_validated_work": CharterActionClass(
        CharterRole.FLOW, _W, CharterBinding.APPROVABLE,
        "publish retained validated work",
    ),
    # Removes the issue's own blocked-failed so its green PR's review runs
    # (#7399): a label comes off, nothing is lost, so it is not destructive.
    "release_withheld_review": CharterActionClass(
        CharterRole.FLOW, _F, CharterBinding.APPROVABLE,
        "release a review withheld only by the issue's own block",
    ),
    # Decides a needs-human WORK block in the operator's stead (#7658): it
    # posts the decision, files a split's children at create_issue's depth and
    # discharges only the causes it resolved. Nothing is destroyed and the
    # operator can put the block back (the cause is then never resolved
    # again), so it is approvable, not destructive. It is a FIX, not a
    # workaround: it removes the cause of the block rather than routing
    # around it, the same depth as the release and the filing it composes.
    "resolve_block": CharterActionClass(
        CharterRole.FLOW, _F, CharterBinding.APPROVABLE,
        "resolve a needs-human block by deciding it",
    ),
    "propose_decision": CharterActionClass(
        CharterRole.FLOW, _W, CharterBinding.OPERATOR_DECISION,
        "propose a decision for the operator to approve",
    ),
    "reset_retry": CharterActionClass(
        CharterRole.FLOW, _W, CharterBinding.DESTRUCTIVE,
        "reset an issue and retry it from scratch",
    ),
    "create_issue": CharterActionClass(
        CharterRole.FLOW, _F, CharterBinding.APPROVABLE, "file a follow-up issue"
    ),
    "request_rework": CharterActionClass(
        CharterRole.REVIEW_LOOP, _F, CharterBinding.APPROVABLE,
        "send a PR back for scoped rework",
    ),
    PROMOTE_FINDING_KIND: CharterActionClass(
        CharterRole.LEARNING, _F, CharterBinding.APPROVABLE,
        "promote a pattern case file to a runnable fix issue",
    ),
}


def promotion_ceiling(promote_mode: str) -> tuple[CharterAuthority, str]:
    """The per-action ceiling ``tech_lead.findings.promote`` sets for a promotion.

    ``auto`` files ungated (execute); ``gated`` needs approval (propose). ``off``
    switches the lane off entirely and is never decided here.
    """
    if promote_mode == "off":
        raise ValueError("finding promotion is off; it has no charter ceiling")
    authority = (
        CharterAuthority.EXECUTE if promote_mode == "auto" else CharterAuthority.PROPOSE
    )
    return authority, f"tech_lead.findings.promote ({promote_mode})"


class UnclassifiedCharterActionError(KeyError):
    """An action kind reached the charter with no classification row."""


def classify_charter_action(kind: str) -> CharterActionClass:
    """The role and depth *kind* needs. Unknown kinds raise — never guessed."""
    try:
        return CHARTER_ACTION_CLASSES[kind]
    except KeyError:
        raise UnclassifiedCharterActionError(
            f"tech-lead action kind {kind!r} has no charter classification;"
            " add it to CHARTER_ACTION_CLASSES"
        ) from None


class CharterOutcome(str, Enum):
    """What the orchestrator did with one tech-lead action, per the charter."""

    EXECUTED = "executed"
    #: Filed through the per-instance approval gate; waits for the operator.
    PROPOSED = "proposed"
    #: Recorded for the operator to read; nothing runs and nothing awaits approval.
    ADVICE_ONLY = "advice_only"
    #: Execution refused because the action is destructive; it went through the
    #: approval gate instead, whatever the dials said.
    REFUSED_DESTRUCTIVE = "refused_destructive"

    @property
    def awaits_approval(self) -> bool:
        return self in (CharterOutcome.PROPOSED, CharterOutcome.REFUSED_DESTRUCTIVE)


class CharterReason(str, Enum):
    """Stable reason codes for a :class:`CharterVerdict`."""

    FLOOR_ALWAYS_EXECUTES = "floor_always_executes"
    ADVISORY_EXECUTES = "advisory_executes"
    ADVISORY_ACTION_AUTHORITY_PROPOSE = "advisory_action_authority_propose"
    ROLE_DISABLED = "role_disabled"
    BEYOND_DEPTH = "beyond_depth"
    DESTRUCTIVE_REQUIRES_APPROVAL = "destructive_requires_approval"
    OPERATOR_DECISION_ALWAYS_PROPOSED = "operator_decision_always_proposed"
    ROLE_AUTHORITY_PROPOSE = "role_authority_propose"
    ACTION_AUTHORITY_PROPOSE = "action_authority_propose"
    WITHIN_CHARTER_EXECUTE = "within_charter_execute"


@dataclass(frozen=True)
class CharterVerdict:
    """The charter's decision for one action, with everything that produced it."""

    kind: str
    action_class: CharterActionClass
    role_charter: RoleCharter
    #: The per-action ceiling in force (``tech_lead.authority.*`` or the
    #: promotion mode), recorded so a reader sees BOTH dials that decided.
    action_ceiling: CharterAuthority
    #: Where the ceiling came from, for the reason text ("tech_lead.authority.x").
    ceiling_source: str
    outcome: CharterOutcome
    reason_code: CharterReason
    reason: str

    @property
    def role(self) -> CharterRole:
        return self.action_class.role

    @property
    def required_depth(self) -> CharterDepth:
        return self.action_class.depth

    @property
    def executes(self) -> bool:
        return self.outcome is CharterOutcome.EXECUTED

    @property
    def awaits_approval(self) -> bool:
        return self.outcome.awaits_approval

    @property
    def advice_only(self) -> bool:
        return self.outcome is CharterOutcome.ADVICE_ONLY


#: Bindings no dial restricts, and the one verdict each always gets: a floor
#: always executes; an operator decision is always the operator's (#7593).
_UNRESTRICTED_BINDINGS: Mapping[CharterBinding, tuple[CharterOutcome, CharterReason, str]] = {
    CharterBinding.FLOOR: (
        CharterOutcome.EXECUTED,
        CharterReason.FLOOR_ALWAYS_EXECUTES,
        "Always executes: to {what} is a safety floor no charter setting removes.",
    ),
    CharterBinding.OPERATOR_DECISION: (
        CharterOutcome.PROPOSED,
        CharterReason.OPERATOR_DECISION_ALWAYS_PROPOSED,
        "Waiting on you: to {what} is yours to approve or decline; no charter"
        " setting lets the tech lead decide it.",
    ),
}


def decide_charter(
    kind: str,
    charter: TechLeadCharter,
    *,
    action_ceiling: CharterAuthority,
    ceiling_source: str,
) -> CharterVerdict:
    """Decide one action kind against the charter and its per-action ceiling.

    Order matters and is the whole policy: floors first (nothing removes them),
    then advice (nothing restricts it), then the role's enabled/depth gate
    (out-of-charter is advice, not a question), then the destructive rule (never
    unattended), then the two authority dials — the role's and the action's —
    where either one saying ``propose`` sends the action to the approval gate.
    """
    action_class = classify_charter_action(kind)
    role_charter = charter.for_role(action_class.role)
    role = action_class.role.value
    what = action_class.description

    def verdict(outcome: CharterOutcome, code: CharterReason, reason: str) -> CharterVerdict:
        return CharterVerdict(
            kind=kind,
            action_class=action_class,
            role_charter=role_charter,
            action_ceiling=action_ceiling,
            ceiling_source=ceiling_source,
            outcome=outcome,
            reason_code=code,
            reason=reason,
        )

    ceiling_executes = action_ceiling is CharterAuthority.EXECUTE
    fixed = _UNRESTRICTED_BINDINGS.get(action_class.binding)
    if fixed is not None:
        outcome, code, template = fixed
        return verdict(outcome, code, template.format(what=what))
    if action_class.binding is CharterBinding.ADVISORY:
        if ceiling_executes:
            return verdict(
                CharterOutcome.EXECUTED,
                CharterReason.ADVISORY_EXECUTES,
                f"Done: advice is never restricted by the charter, and"
                f" {ceiling_source} is execute.",
            )
        return verdict(
            CharterOutcome.ADVICE_ONLY,
            CharterReason.ADVISORY_ACTION_AUTHORITY_PROPOSE,
            f"Recorded, not done: {ceiling_source} is propose, so the proposal to"
            f" {what} is kept as a would-have-done record.",
        )
    if not role_charter.enabled:
        return verdict(
            CharterOutcome.ADVICE_ONLY,
            CharterReason.ROLE_DISABLED,
            f"Advice only: the {role} role is disabled in the charter, so the"
            f" proposal to {what} is recorded, not acted on.",
        )
    if not role_charter.depth.covers(action_class.depth):
        return verdict(
            CharterOutcome.ADVICE_ONLY,
            CharterReason.BEYOND_DEPTH,
            f"Advice only: to {what} is a {action_class.depth.value}, deeper than"
            f" {role}'s allowed depth ({role_charter.depth.value}).",
        )
    if action_class.binding is CharterBinding.DESTRUCTIVE:
        return verdict(
            CharterOutcome.REFUSED_DESTRUCTIVE,
            CharterReason.DESTRUCTIVE_REQUIRES_APPROVAL,
            f"Always needs approval: to {what} is destructive, so it never runs"
            " unattended.",
        )
    if role_charter.authority is CharterAuthority.PROPOSE:
        return verdict(
            CharterOutcome.PROPOSED,
            CharterReason.ROLE_AUTHORITY_PROPOSE,
            f"Waiting on you: {role} may {_VERB[action_class.depth]} but must"
            " propose (authority: propose).",
        )
    if not ceiling_executes:
        return verdict(
            CharterOutcome.PROPOSED,
            CharterReason.ACTION_AUTHORITY_PROPOSE,
            f"Waiting on you: {ceiling_source} is propose.",
        )
    return verdict(
        CharterOutcome.EXECUTED,
        CharterReason.WITHIN_CHARTER_EXECUTE,
        f"Done: {role} may {_VERB[action_class.depth]} and act on its own"
        " (authority: execute).",
    )
