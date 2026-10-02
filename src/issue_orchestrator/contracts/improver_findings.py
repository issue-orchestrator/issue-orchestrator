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

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

#: Bump when a field is added, removed or changes meaning. The prompt's
#: ``schema_version`` must match.
IMPROVER_FINDINGS_SCHEMA_VERSION = 4

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
    #: ``blocked-items.json#/items/<i>/open_prs/<j>/pipeline_events/<k>``: an
    #: event of the refused PR's pipeline. Required when the refused work is
    #: an open PR of the item with retained pipeline events; None otherwise.
    pipeline_event: SourceRef | None = None
    #: What the block holds up and why it cannot proceed (e.g. the published
    #: work on PR #379 can never be reviewed while #364 is blocked).
    impact: NonEmpty


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


class ImproverFindings(_Closed):
    schema_version: Literal[4]
    engine_commit: NonEmpty
    engine_started_at: Timestamp
    findings: tuple[Finding, ...]
    #: Every staged blocked item, each exactly once.
    blocked_items: tuple[BlockedItemAccount, ...]
    trend: Trend


__all__ = [
    "FINDINGS_FILE",
    "IMPROVER_FINDINGS_SCHEMA_VERSION",
    "AnomalyKeyRef",
    "BlockedItemAccount",
    "DownstreamStall",
    "EngineTag",
    "Finding",
    "GradingWindow",
    "ImproverFindings",
    "Observed",
    "Reproduction",
    "RootCause",
    "Trend",
]
