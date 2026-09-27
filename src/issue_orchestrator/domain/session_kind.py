"""SessionKind - the ONE authoritative answer to "what kind of session is this?".

Before #7347 the answer was spread over six vocabularies - ``SessionKind`` on the
slot key, ``SessionType`` on terminal refs, the terminal-name prefix, the
run-directory phase label, the agent label, and the run ledger's
``completion_task`` - and they disagreed. A tech-lead run launched stamped
``CODE`` / ``issue-N`` / ``coding-1`` and was recognisable only by its agent
label; a rework's validation retry relaunched stamped ``CODE`` because the
retry dropped its source task. Every ``task is CODE`` policy check therefore
silently included tech leads and reworks-in-retry.

The kind is now stamped ONCE, by the launcher that starts the session, and:

- persisted in the durable run ledger (``issue_runs.task``) and in the run
  directory (``session-identity.json`` ``"task"``);
- carried through a validation retry (``PendingValidationRetry.source_kind``);
- restored from the ledger row of the exact run on restart.

Every other vocabulary is DERIVED from it here, at one boundary:

=====================  =============================================
slot identity          ``SessionKey(issue, kind)``
terminal naming lane   :attr:`SessionKind.session_type` (``SessionType``)
terminal name          :meth:`SessionKind.terminal_name`
run-phase label        :meth:`SessionKind.phase_label`
event/identity "task"  ``kind.value``
=====================  =============================================

Nothing derives a kind back from those names. The only readers that turn an
older stamp into a kind are the documented LEGACY decoders below
(:meth:`SessionKind.from_ledger_stamps`, :meth:`SessionKind.from_phase_label`),
which exist because records written before this change carry the old stamps.

What a kind MAY DO is its capability row (:class:`SessionCapabilities`, the
``_CAPABILITIES`` table below, read as ``kind.capabilities``). Policy asks the
table, never the kind directly; a static guard
(``tests/unit/domain/test_session_kind_guard.py``) fails the build on any new
direct kind comparison outside this module. Behaviour that is genuinely one
kind's own - verdict routing, workspace shape, tech-lead exclusivity - stays in
per-kind handlers the guard lists by name.

A tech lead's FLAVOR (batch review / health review / failure investigation) is
deliberately NOT part of the kind. Its single owner is
``TechLeadLaunchScope`` / the launch-authority row, and every flavor has the
same session-level behaviour; putting it here as well would give the flavor a
second owner and make pre-#7347 ledger rows (which recorded only "tech-lead")
ambiguous.
"""

from dataclasses import dataclass
from enum import Enum

#: The agent label the historical-completion intake records its runs under.
#: A historical run is an operator import of work done outside the
#: orchestrator; it never has a terminal or an agent session.
HISTORICAL_AGENT_LABEL = "operator:historical"


class SessionType(Enum):
    """The terminal NAMING LANE of a session: the ``<lane>-<number>`` prefix.

    Derived from :class:`SessionKind` (``kind.session_type``) when a terminal is
    named. It is a handle vocabulary, not a kind: a terminal that predates
    #7347 can sit in a lane its kind no longer maps to (a tech lead launched as
    ``issue-N``), so policy about a live session reads ``Session.kind`` and
    never re-derives it from the lane.
    """

    ISSUE = "issue"
    REVIEW = "review"
    RETROSPECTIVE_REVIEW = "retrospective-review"
    REWORK = "rework"
    TECH_LEAD = "tech-lead"


class CompletionProtocol(Enum):
    """How a session of a kind reports that it is done."""

    CODING_DONE = "coding-done"
    REVIEWER_DONE = "reviewer-done"
    NONE = "none"  # never an agent session (a historical import)


class SandboxRole(Enum):
    """The sandbox-relevant role a session plays.

    Distinct from :class:`SessionKind`: several kinds collapse to one sandbox
    role (a ``CODE`` and a ``REWORK`` session are both a ``CODER``). The role is
    the axis the scope policy branches on, and the seam future policies extend
    (e.g. a tech-lead's evidence-map-driven read scope).
    """

    CODER = "coder"
    REVIEWER = "reviewer"
    TECH_LEAD = "tech-lead"


@dataclass(frozen=True, slots=True)
class SessionCapabilities:
    """What a session of one kind may do: ONE row of the capability table.

    - ``produces_commits``: it may commit on its branch, so the code validation
      gate runs on its completion and a failed validation is retried AS this
      kind. Review-only kinds make no commits (#6426).
    - ``capturable``: its validated, publication-requesting completion is the
      issue's deliverable, so termination captures it into validated-work
      recovery (with a head that is ahead of base). A tech-lead run is never
      captured - its own completion owns its branch (#7323, #7346) - and a
      completion that is not capturable may finish with nothing to publish.
    - ``open_pr_means_done``: an open PR on its branch means its work is done,
      so the observer may ``/exit`` it and read its exit as COMPLETED. Only a
      coding session's PR is its own output; every other kind starts with the
      PR already open (#7343).
    - ``holds_issue_custody``: it holds its issue's in-progress claim - taken at
      launch, released at every terminal outcome - and its failure, timeout,
      block or invalid record applies the issue's blocking labels. Coding runs
      and tech-lead runs (on their anchor or focus issue) do; a rework - and so
      a rework's validation retry - never took the claim, its open PR holds
      the issue; a reviewer's completion never touches it.
    - ``killable_generation``: it is an issue-runtime owner - the tech lead may
      kill it, an issue reset terminates it, a stop terminates it with capture.
    - ``completion_protocol`` / ``sandbox_role``: which completion command,
      default prompt and sandbox role its agent gets.
    """

    produces_commits: bool
    capturable: bool
    open_pr_means_done: bool
    holds_issue_custody: bool
    killable_generation: bool
    completion_protocol: CompletionProtocol
    sandbox_role: SandboxRole | None

    @property
    def reports_verdict(self) -> bool:
        """Its completion is a reviewer's verdict (reviewer-done), never a PR."""
        return self.completion_protocol is CompletionProtocol.REVIEWER_DONE


class SessionKind(Enum):
    """The kind of work a session (or recorded run) performs.

    The values are the persisted strings: they are what the run ledger's
    ``task`` column, the run directory's ``session-identity.json`` and the
    queued-work payloads store.
    """

    CODE = "code"  # A coding session on an issue
    REVIEW = "review"  # Reviewing a PR
    RETROSPECTIVE_REVIEW = "retrospective-review"  # Reviewing existing merged work
    REWORK = "rework"  # Fixing issues found in review
    TECH_LEAD = "tech-lead"  # A tech-lead run (any flavor; see module docstring)
    HISTORICAL = "historical"  # An operator's historical-completion import

    @property
    def capabilities(self) -> SessionCapabilities:
        """This kind's row of the capability table: what its sessions may do."""
        return _CAPABILITIES[self]

    @property
    def runs_as_agent_session(self) -> bool:
        """Whether a run of this kind is an agent session with a terminal."""
        return self in _SESSION_TYPE

    @property
    def session_type(self) -> SessionType:
        """The terminal naming lane a session of this kind is launched in."""
        try:
            return _SESSION_TYPE[self]
        except KeyError:
            raise ValueError(f"a {self.value} run has no terminal session") from None

    def terminal_name(self, number: int) -> str:
        """The terminal name a NEW session of this kind is launched under.

        Only for naming a terminal at launch. An existing session's name is its
        ``Session.terminal_id``, which may predate this mapping.
        """
        return f"{self.session_type.value}-{number}"

    def conflicting_terminal_names(self, number: int) -> tuple[str, ...]:
        """Every terminal name a live run of this kind for ``number`` may hold.

        Its own name first; then, for a tech-lead run - which launched under
        the issue lane before #7347 - the ``issue-N`` a run launched before the
        upgrade may still be running under. A launch must treat either as an
        existing terminal, or it starts a second run of the same work.
        """
        own = self.terminal_name(number)
        if self in _LAUNCHED_AS_ISSUE_BEFORE_7347:
            return (own, SessionKind.CODE.terminal_name(number))
        return (own,)

    def phase_label(self, attempt: int) -> str:
        """The run-directory phase label for this kind's ``attempt``-th run.

        Coding and rework share the ``coding-N`` label on purpose: it is the
        issue's coding-iteration number the timeline and dialogs present
        (a rework cycle is coding attempt ``cycle + 1``). The label is
        presentation and run identity only; nothing reads the kind back from
        it except the legacy pre-filter :meth:`from_phase_label`.
        """
        if type(attempt) is not int or attempt <= 0:
            raise ValueError(f"phase attempt must be a positive int, got {attempt!r}")
        try:
            prefix = _PHASE_PREFIX[self]
        except KeyError:
            raise ValueError(f"a {self.value} run has no launch phase") from None
        return f"{prefix}-{attempt}"

    @classmethod
    def for_issue_launch(
        cls,
        agent_label: str | None,
        tech_lead_agent: str | None,
        *,
        granted_tech_lead_scope: bool = False,
    ) -> "SessionKind":
        """The kind an issue-lane launch stamps: the ONE agent-label mapping.

        An issue carrying the configured tech-lead agent label is launched as
        a tech-lead run (whether it came off the pending tech-lead queue or was
        picked up as an ordinary issue); every other issue launch is coding.
        This is the only place the agent label decides a kind; afterwards the
        stamped kind is the answer. A launch granted a tech-lead scope that
        would stamp anything else is a composition error and raises.
        """
        kind = cls.TECH_LEAD if tech_lead_agent and agent_label == tech_lead_agent else cls.CODE
        if granted_tech_lead_scope and kind is not cls.TECH_LEAD:
            raise ValueError(
                f"a launch granted a tech-lead scope would run as {kind.value} "
                f"(agent {agent_label!r}, tech lead {tech_lead_agent!r})"
            )
        return kind

    @classmethod
    def issue_is_work_item(
        cls, agent_label: str | None, tech_lead_agent: str | None
    ) -> bool:
        """Whether an issue is a work item that stuck-issue recovery may re-drive.

        Asked of the capability table through the kind a launch of the issue
        would stamp: an issue that launches as a kind whose output is not the
        issue's deliverable (not ``capturable``) - a tech lead's batch or
        health-review anchor - is tech-lead machinery. Its run holds the
        in-progress claim and a failed or blocked run labels it like any
        claimed issue, which made the stuck sweep launch a failure
        investigation of the tech lead's own anchor (#7347 blind spot 7).
        """
        return cls.for_issue_launch(agent_label, tech_lead_agent).capabilities.capturable

    @classmethod
    def from_ledger_stamps(
        cls, task: str, completion_task: str | None, agent_label: str | None
    ) -> "SessionKind":
        """Decode a run-ledger row's kind, including rows written before #7347.

        A row written since #7347 stores the kind in BOTH ``task`` and
        ``completion_task``. Older rows carry the old stamps, read as follows:

        - ``completion_task`` NULL: the row predates role recording
          (``agent_label`` is NULL too). The slot kind is read as stamped; such
          a row authorizes no completion processing (its role is unknown).
        - ``task`` ``code`` + ``completion_task`` ``tech-lead``: a tech-lead run
          launched under the old ``CODE`` stamp. ``completion_task`` was the
          durable role record, so it is ``TECH_LEAD``.
        - ``task`` = ``completion_task`` = ``code`` under the historical-intake
          label: an operator historical import, stamped ``CODE`` before it had
          a kind of its own -> ``HISTORICAL``.
        - any other agreeing pair: that kind, as stamped. (A rework's
          validation retry was recorded ``code`` and ran as coding work; it is
          read as what it ran as, not re-guessed.)

        Any other disagreement is corruption and raises ``ValueError``; the
        ledger turns that into its typed "evidence unavailable" refusal.
        """
        stamped = cls(task)
        if (completion_task is None) != (agent_label is None):
            raise ValueError(
                "run ledger row records only half of its role "
                f"(agent_label={agent_label!r}, completion_task={completion_task!r})"
            )
        if completion_task is None:
            return stamped
        recorded = cls(completion_task)
        if recorded is stamped:
            if stamped is cls.CODE and agent_label == HISTORICAL_AGENT_LABEL:
                return cls.HISTORICAL
            return stamped
        if stamped is cls.CODE and recorded is cls.TECH_LEAD:
            return cls.TECH_LEAD
        raise ValueError(
            f"run ledger row kind stamps disagree: task={task!r} "
            f"completion_task={completion_task!r}"
        )

    @classmethod
    def from_retry_stamp(cls, stamped: str, *, carries_authority: bool) -> "SessionKind":
        """Decode a queued validation retry's source kind, including pre-#7347 rows.

        Before #7347 a tech-lead run was stamped ``code``, and so was its retry.
        It is still recognisable without guessing: only a tech-lead retry
        inherits launch authority, so a ``code`` retry that carries an
        ``authority_run`` is a tech-lead retry. Every other stored value is read
        as stamped (a rework's retry of a retry was queued ``code`` and ran as
        coding work; it is read as what it ran as).
        """
        kind = cls(stamped)
        if kind is cls.CODE and carries_authority:
            return cls.TECH_LEAD
        return kind

    @classmethod
    def from_phase_label(cls, label: str) -> "SessionKind | None":
        """LEGACY pre-filter: the kind a run directory's phase label implies.

        Validation-retry discovery scans a worktree's run directories before it
        can join them to the ledger, and needs to skip review-only runs there.
        The phase label is all the directory name carries, and for runs written
        before #7347 it under-reports: a tech-lead or rework run is labelled
        ``coding-N``. That is safe for this pre-filter (neither is review-only);
        the AUTHORITATIVE kind of a run-scoped retry is the ledger row it is
        then joined to. Never use this for any other decision.

        Returns ``None`` for an unrecognized label so callers fail safe.
        Prefixes are matched longest-first so ``retrospective-review-`` is
        never shadowed by ``review-``.
        """
        for prefix, kind in _LABEL_PREFIXES:
            if label.startswith(prefix):
                return kind
        return None


_CAPABILITIES: dict[SessionKind, SessionCapabilities] = {
    SessionKind.CODE: SessionCapabilities(
        produces_commits=True,
        capturable=True,
        open_pr_means_done=True,
        holds_issue_custody=True,
        killable_generation=True,
        completion_protocol=CompletionProtocol.CODING_DONE,
        sandbox_role=SandboxRole.CODER,
    ),
    SessionKind.REWORK: SessionCapabilities(
        produces_commits=True,
        capturable=True,
        open_pr_means_done=False,  # its branch's PR is the one it is fixing
        holds_issue_custody=False,  # the open PR holds the issue
        killable_generation=True,
        completion_protocol=CompletionProtocol.CODING_DONE,
        sandbox_role=SandboxRole.CODER,
    ),
    SessionKind.REVIEW: SessionCapabilities(
        produces_commits=False,
        capturable=False,
        open_pr_means_done=False,  # it starts with the PR it reviews open
        holds_issue_custody=False,
        killable_generation=False,
        completion_protocol=CompletionProtocol.REVIEWER_DONE,
        sandbox_role=SandboxRole.REVIEWER,
    ),
    SessionKind.RETROSPECTIVE_REVIEW: SessionCapabilities(
        produces_commits=False,
        capturable=False,
        open_pr_means_done=False,
        holds_issue_custody=False,
        killable_generation=False,
        completion_protocol=CompletionProtocol.REVIEWER_DONE,
        sandbox_role=SandboxRole.REVIEWER,
    ),
    SessionKind.TECH_LEAD: SessionCapabilities(
        produces_commits=True,  # it may commit, and its validation is retried
        capturable=False,  # its completion owns its branch; never recovered
        open_pr_means_done=False,
        holds_issue_custody=True,
        killable_generation=False,
        completion_protocol=CompletionProtocol.CODING_DONE,
        sandbox_role=SandboxRole.TECH_LEAD,
    ),
    SessionKind.HISTORICAL: SessionCapabilities(
        produces_commits=False,  # never a session: nothing to validate or retry
        capturable=True,  # the operator imported it to be recovered
        open_pr_means_done=False,
        holds_issue_custody=False,
        killable_generation=False,
        completion_protocol=CompletionProtocol.NONE,
        sandbox_role=None,
    ),
}

_SESSION_TYPE: dict[SessionKind, SessionType] = {
    SessionKind.CODE: SessionType.ISSUE,
    SessionKind.REVIEW: SessionType.REVIEW,
    SessionKind.RETROSPECTIVE_REVIEW: SessionType.RETROSPECTIVE_REVIEW,
    SessionKind.REWORK: SessionType.REWORK,
    SessionKind.TECH_LEAD: SessionType.TECH_LEAD,
}

_LAUNCHED_AS_ISSUE_BEFORE_7347 = frozenset({SessionKind.TECH_LEAD})

_PHASE_PREFIX: dict[SessionKind, str] = {
    SessionKind.CODE: "coding",
    SessionKind.REWORK: "coding",
    SessionKind.REVIEW: "review",
    SessionKind.RETROSPECTIVE_REVIEW: "retrospective-review",
    SessionKind.TECH_LEAD: "tech-lead",
}

_LABEL_PREFIXES: tuple[tuple[str, SessionKind], ...] = (
    ("retrospective-review-", SessionKind.RETROSPECTIVE_REVIEW),
    ("review-", SessionKind.REVIEW),
    ("rework-", SessionKind.REWORK),
    ("tech-lead-", SessionKind.TECH_LEAD),
    ("coding-", SessionKind.CODE),  # validation-retry / coding phase label
    ("issue-", SessionKind.CODE),  # issue/coding session name
)
