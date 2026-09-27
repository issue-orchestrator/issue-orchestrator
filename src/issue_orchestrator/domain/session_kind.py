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

A tech lead's FLAVOR (batch review / health review / failure investigation) is
deliberately NOT part of the kind. Its single owner is
``TechLeadLaunchScope`` / the launch-authority row, and every flavor has the
same session-level behaviour; putting it here as well would give the flavor a
second owner and make pre-#7347 ledger rows (which recorded only "tech-lead")
ambiguous.
"""

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
    def is_review_only(self) -> bool:
        """Whether this kind is read-only: it makes no commits and publishes nothing.

        Review-only sessions (auditing a PR or existing merged work) produce no
        branch commits, so the publish/code-validation-retry machinery - which
        exists to validate a coder's changes before opening a PR - does not apply
        to them. Treating a review-only session as ordinary coding work leads to
        empty-branch ``create_pr`` attempts (see issue #6426).
        """
        return self in {SessionKind.REVIEW, SessionKind.RETROSPECTIVE_REVIEW}

    @property
    def holds_issue_custody(self) -> bool:
        """Whether a session of this kind holds its issue's in-progress claim.

        Its launch adds ``in-progress`` and its failure, timeout or block
        releases it and applies the issue's blocking labels. Before #7347 this
        was read off the terminal-name prefix (``issue-``), which covered coding
        sessions and - because they launched as ``issue-N`` - tech-lead runs
        and every validation retry. The kind keeps coding and tech-lead runs; a
        rework (including its validation retry) never takes the claim, since
        its open PR holds the issue.
        """
        return self in {SessionKind.CODE, SessionKind.TECH_LEAD}

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
