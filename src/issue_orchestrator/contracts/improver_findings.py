"""The improver's output, ``improver-findings.json``, as a typed contract (#7490).

The improver (``examples/prompts/tech-lead-improver.md``) is an untrusted
agent: what it writes is intent, and the orchestrator decides what becomes of
it. These models are the file's SHAPE only: every field typed, every model
frozen and closed (an unknown field rejects the file), liveness facts as the
three strings the prompt names, never booleans.

Everything that relates one field to another, or a field to the staged inputs
(an anomaly key that must exist, a tracked issue that must be open, a
``not_noticed`` grade that needs a proven onset, ...), is the business of the
one validation owner, :mod:`..domain.improver_findings_validation`. Keeping
those rules out of the models keeps each rule in exactly one place, where a
test can name it.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

#: Bump when a field is removed, changes meaning, or is added as required.
#: The prompt's ``schema_version`` must match. An OPTIONAL field is added
#: without a bump (#8700): the champion's prompt is frozen (#8001), and a bump
#: would reject every answer it asks for until a promotion.
IMPROVER_FINDINGS_SCHEMA_VERSION = 6

#: The improver's one output, beside ``improver-data/`` in the run directory.
FINDINGS_FILE = "improver-findings.json"

#: A present/recurs fact: the prompt's three-valued string, never a boolean.
Liveness = Literal["true", "false", "unknown"]
Origin = Literal["before_start", "unknown"]
EvidenceKind = Literal["snapshot", "occurrence"]
SupportedClaim = Literal["present_after_start", "recurs_after_start", "origin"]
Classification = Literal["new_defect", "tracked", "unknown"]
StallPoint = Literal[
    "not_noticed", "noticed_not_acted", "acted_not_effective", "not_in_charter", "unknown"
]
Output = Literal[
    "exam_case", "capability_issue", "charter_proposal", "prompt_proposal", "needs_investigation"
]
ReproductionKind = Literal["exam_case", "unit_test", "integration_test"]
TrendValue = Literal["up", "flat", "down", "unobserved"]
#: How a blocked item is accounted for: by a finding about it, or as handed
#: to the operator by an applied tech-lead escalation of its own.
BlockedItemDisposition = Literal["finding", "awaiting_operator"]

NonEmpty = Annotated[str, StringConstraints(min_length=1, strip_whitespace=False)]
#: Text with something in it: not empty, not only whitespace.
Stated = Annotated[str, StringConstraints(pattern=r"\S")]
#: The pipeline action a block refuses: a PR's review or rework, or a
#: queued tech-lead run (the audit's ``refused_work`` signature names it).
RefusedAction = Literal["review", "rework", "tech_lead_run"]
#: A finding's stable id: it keys the orchestrator's dedup marker.
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,79}$")]
#: ``<staged input file>#<record id or JSON pointer>``.
SourceRef = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]+(/[A-Za-z0-9._-]+)?\.json#.+$")]
Timestamp = AwareDatetime


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


class EngineTag(_Closed):
    """The engine a finding is about: ``inputs.json``'s ``engine_id`` and
    ``audited_repo``. One run audits one engine; a finding tagged with another
    is rejected, so findings from different engines can never be mixed up."""

    id: NonEmpty
    repo: NonEmpty


class AnomalyKeyRef(_Closed):
    """An audit anomaly's identity, ``Anomaly.key`` in ``audit.json``/``audit-diff.json``."""

    kind: NonEmpty
    subject: NonEmpty
    signature: NonEmpty

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.subject, self.signature)


class Observed(_Closed):
    """One cited record and the claim it supports."""

    at: Timestamp
    kind: EvidenceKind
    source: SourceRef
    supports: SupportedClaim

    @property
    def file(self) -> str:
        return self.source.split("#", 1)[0]

    @property
    def ref(self) -> str:
        return self.source.split("#", 1)[1]


class GradingWindow(_Closed):
    from_: Timestamp | Literal["unknown"] = Field(alias="from")
    to: Timestamp


class RootCause(_Closed):
    owner: NonEmpty
    why: NonEmpty
    same_shape_sites: tuple[NonEmpty, ...]


class Reproduction(_Closed):
    kind: ReproductionKind
    harness: NonEmpty
    #: Required for (and only for) ``kind: exam_case``: a NEW case id.
    case_id: NonEmpty | None = None
    planted_state: NonEmpty
    assertions: tuple[NonEmpty, ...] = Field(min_length=1)
    fails_on: NonEmpty


class Finding(_Closed):
    id: Slug
    engine: EngineTag
    anomaly_keys: tuple[AnomalyKeyRef, ...] = Field(min_length=1)
    present_after_start: Liveness
    recurs_after_start: Liveness
    origin: Origin
    observed: tuple[Observed, ...] = Field(min_length=1)
    grading_window: GradingWindow
    classification: Classification
    tracked_issue: Annotated[int, Field(gt=0)] | None = None
    stall_point: StallPoint
    stall_evidence: tuple[NonEmpty, ...] = ()
    #: ``not_in_charter`` because no action type expresses the remedy: the
    #: kind that would, which must not exist in the effective charter.
    missing_action_kind: NonEmpty | None = None
    #: ``not_in_charter`` because a charter setting holds back an EXISTING
    #: remedy: that remedy's action kind.
    remedy_action_kind: NonEmpty | None = None
    output: Output
    root_cause: RootCause | None = None
    reproduction: Reproduction | None = None
    proposal: NonEmpty | None = None
    missing_evidence: tuple[NonEmpty, ...] = ()


#: What a design finding says is wrong with the system's model of the world.
DesignFindingKind = Literal[
    #: One mechanism carrying two meanings (one label for work-block and merge-gate).
    "conflated_mechanism",
    #: An assumption the system relies on and nothing checks.
    "silent_assumption",
    #: The operator doing by hand what the system should do.
    "manual_operator_step",
    #: A human needed only because a tool or action type is missing.
    "missing_capability",
    #: Something only the operator experiences: approval by label removal, no
    #: single view of what waits on them.
    "operator_friction",
]
#: A quote long enough to be found, not matched by chance.
Quote = Annotated[str, StringConstraints(pattern=r"\S(?:.*\S)?", min_length=12, max_length=2000)]
#: A path relative to the run directory, under ``improver-data/`` or ``toolbox/``.
EvidencePath = Annotated[
    str, StringConstraints(pattern=r"^(improver-data|toolbox)/[^\x00]+$", max_length=1024)
]


class FileCitation(_Closed):
    """Text the improver read in a staged file: ``path:line`` and a verbatim
    quote of it (a log line with its timestamp, a source line, a JSON field)."""

    kind: Literal["file"]
    path: EvidencePath
    line: Annotated[int, Field(gt=0)]
    quote: Quote


class ToolCitation(_Closed):
    """Text a toolbox call answered (an issue body, a review verdict, a query
    result): the call's id, as the toolbox numbered its answer, and a
    verbatim quote of it."""

    kind: Literal["tool"]
    call: Annotated[int, Field(gt=0)]
    quote: Quote


Citation = Annotated[FileCitation | ToolCitation, Field(discriminator="kind")]


class DesignFinding(_Closed):
    """A place where the system's model of the world doesn't match reality
    (#8001): not a stall of the tech lead on an anomaly, but a design that
    makes stalls, or operator work, inevitable. Held to the same evidence
    rule as a stall finding: every claim cites what was read."""

    id: Slug
    engine: EngineTag
    kind: DesignFindingKind
    summary: Stated
    evidence: tuple[Citation, ...] = Field(min_length=1)
    impact: Stated
    proposed_change: Stated
    #: Where the defect lives in the engine source, written as a stall
    #: finding's ``root_cause.owner`` is (``<module>:<function>``); None when
    #: it lives in no one function. A design finding and another finding
    #: with one code site and overlapping evidence are one defect, filed
    #: once (#8700).
    owner: NonEmpty | None = None


class Trend(_Closed):
    exam_scores: TrendValue
    operator_interventions: TrendValue
    notes: str


class DownstreamStall(_Closed):
    """One of a blocked item's ``stalled_work`` entries, accounted for: the
    work its block holds up, what that costs, the finding that grades it,
    and the PR pipeline event that shows the refusal."""

    #: The ``stalled_work`` entry: a ``refused_work`` anomaly of ``audit.json``.
    anomaly_key: AnomalyKeyRef
    #: The finding that keys that anomaly and cites its snapshot.
    finding_id: Slug
    #: The pipeline action the block refuses: the one the anomaly's
    #: ``<action>:<reason>`` signature names.
    refused_action: RefusedAction
    #: ``blocked-items.json#/items/<i>/open_prs/<j>/pipeline_events/<k>``: an
    #: event of the refused PR's pipeline, its retained skip of that action
    #: when there is one. Required when the refused work is an open PR of the
    #: item with retained pipeline events; None otherwise.
    pipeline_event: SourceRef | None = None
    #: What the block holds up and why it cannot proceed (e.g. the published
    #: work on PR #379 can never be reviewed while #364 is blocked).
    impact: Stated


class BlockedItemAccount(_Closed):
    """One staged blocked item (``blocked-items.json``), accounted for.

    The operator's objective is that blocked items get resolved, so every
    run accounts for each one: a ``finding`` (``finding_id``) grades the
    tech lead on it; ``awaiting_operator`` cites (``evidence``) the applied
    tech-lead decision that handed it to the operator.
    """

    number: Annotated[int, Field(gt=0)]
    disposition: BlockedItemDisposition
    finding_id: Slug | None = None
    evidence: tuple[NonEmpty, ...] = ()
    why: NonEmpty
    #: Every one of the item's ``stalled_work`` entries, each exactly once,
    #: whatever the disposition: a hand-over or a grade of the block does not
    #: account for the work the block holds up.
    downstream: tuple[DownstreamStall, ...] = ()


class PromptEdit(_Closed):
    """Replace one exact passage of the improver's prompt."""

    kind: Literal["prompt"]
    #: Text that must occur exactly once in the champion's prompt.
    find: Annotated[str, StringConstraints(min_length=12, max_length=4000)]
    replace: Annotated[str, StringConstraints(max_length=4000)]


class AgentEdit(_Closed):
    """Run the improver on another provider or model."""

    kind: Literal["agent"]
    provider: Literal["claude", "codex"]
    model: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]


class ModeEdit(_Closed):
    kind: Literal["mode"]
    mode: Literal["scripted", "empowered"]


class HeatsEdit(_Closed):
    kind: Literal["heats"]
    heats: Annotated[int, Field(ge=1, le=5)]


class BudgetEdit(_Closed):
    """The empowered investigation's budget, in minutes."""

    kind: Literal["budget_minutes"]
    #: At most what a live run can carry (domain.improver_champion.MAX_BUDGET_MINUTES).
    minutes: Annotated[int, Field(ge=10, le=90)]


#: The only things a challenger may change: never the answer keys, the
#: graders, the scoring or the harness.
ImproverEdit = Annotated[PromptEdit | AgentEdit | ModeEdit | HeatsEdit | BudgetEdit, Field(discriminator="kind")]


class ImproverChange(_Closed):
    """One change to the improver itself, proposed by an invited run (#8001).

    It is a challenger, not a decision: it is tested against the champion on
    frozen snapshots and promoted only on a win and a maintainer's approval.
    """

    edit: ImproverEdit
    why: Stated
    #: What should score better if the change works.
    expected_effect: Stated
    #: The run's own findings (stall or design ids) that motivate it.
    motivated_by: Annotated[tuple[Slug, ...], Field(min_length=1)]


class ImproverFindings(_Closed):
    schema_version: Literal[6]
    engine_commit: NonEmpty
    engine_started_at: Timestamp
    findings: tuple[Finding, ...]
    #: Design findings (#8001); empty when there is none.
    design_findings: tuple[DesignFinding, ...]
    #: Every staged blocked item, each exactly once.
    blocked_items: tuple[BlockedItemAccount, ...]
    trend: Trend
    #: One change to the improver, only when the run was invited to propose one.
    improver_change: ImproverChange | None = None


def stored_findings(raw: str | bytes) -> ImproverFindings:
    """An ACCEPTED run's stored findings file, in the current form.

    A run accepted under schema v4 (before design findings, #8001) or v5
    (before improver changes) may still owe effects; it is read as v6 with
    no design findings or change. A NEW submission
    is never read through here: the validator accepts only the current
    version.
    """
    document = json.loads(raw)
    if isinstance(document, dict) and document.get("schema_version") == 4:
        document = {**document, "schema_version": 5, "design_findings": []}
    if isinstance(document, dict) and document.get("schema_version") == 5:
        # v5 (before invited improver changes) carries none.
        document = {**document, "schema_version": 6}
    return ImproverFindings.model_validate_json(json.dumps(document))


__all__ = [
    "FINDINGS_FILE",
    "AgentEdit",
    "BudgetEdit",
    "HeatsEdit",
    "ImproverChange",
    "ImproverEdit",
    "ModeEdit",
    "PromptEdit",
    "IMPROVER_FINDINGS_SCHEMA_VERSION",
    "AnomalyKeyRef",
    "BlockedItemAccount",
    "Citation",
    "DesignFinding",
    "DesignFindingKind",
    "DownstreamStall",
    "EngineTag",
    "FileCitation",
    "Finding",
    "GradingWindow",
    "ImproverFindings",
    "Observed",
    "Reproduction",
    "RootCause",
    "ToolCitation",
    "Trend",
    "stored_findings",
]
