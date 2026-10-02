"""What the orchestrator stages for the improver, file by file (#7490).

``$ISSUE_ORCHESTRATOR_RUN_DIR/improver-data/`` holds exactly the inputs the
improver prompt's Inputs table lists (``examples/prompts/tech-lead-improver.md``).
Each JSON file here has a contract, so the staging writes a typed document and
the findings validator reads the same types back rather than trusting a dict.

Every source that can be incomplete says so in a :class:`Coverage` block:
``complete`` is True only when the records from ``from`` to ``to`` are all
there, and nothing claims coverage it cannot show (an empty ledger proves
nothing about when it started recording, so it covers nothing).
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

IMPROVER_DATA_DIRNAME = "improver-data"

AUDIT_FILE = "audit.json"
AUDIT_PREVIOUS_FILE = "audit-previous.json"
AUDIT_DIFF_FILE = "audit-diff.json"
ENGINE_START_FILE = "engine-start.json"
CHARTER_FILE = "charter.json"
CHARTER_DECISIONS_FILE = "charter-decisions.json"
CASE_FILES_FILE = "case-files.json"
INTERVENTIONS_FILE = "interventions.json"
OPEN_ISSUES_FILE = "open-issues.json"
BLOCKED_ITEMS_FILE = "blocked-items.json"
INPUTS_FILE = "inputs.json"
EXAM_DIRNAME = "exam"
ENGINE_SOURCE_DIRNAME = "engine-source"
#: ``exam/<case id>.json`` is the latest scorecard of a case, and
#: ``exam/<case id>.previous.json`` the one before it.
PREVIOUS_SCORECARD_SUFFIX = ".previous.json"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Coverage(_Closed):
    """Whether a source holds every record from ``from`` to ``to``."""

    #: ``from`` on disk (the prompt's name); ``from_`` in Python.
    from_: AwareDatetime | None = Field(
        validation_alias=AliasChoices("from", "from_"), serialization_alias="from"
    )
    to: AwareDatetime
    complete: bool
    #: Why coverage is what it is, in plain words.
    detail: str

    @model_validator(mode="after")
    def _complete_means_a_span(self) -> "Coverage":
        if self.complete and (self.from_ is None or self.from_ > self.to):
            raise ValueError("complete coverage needs a span with from <= to")
        return self

    def contains(self, start: AwareDatetime, end: AwareDatetime) -> bool:
        """Whether the whole span ``start``..``end`` is complete here."""
        return self.complete and self.from_ is not None and self.from_ <= start and end <= self.to


class EngineStartInput(_Closed):
    started_at: AwareDatetime
    engine_commit: str = Field(min_length=1)
    package_version: str
    repo_head: str | None
    #: The engine's source tree at ``engine_commit``, relative to improver-data.
    source_path: Literal["engine-source"] = ENGINE_SOURCE_DIRNAME


class StagedDecision(_Closed):
    """One charter decision, with when its effect was applied (if it was)."""

    decision_id: str
    run_id: str
    action_id: str
    role: str
    action_kind: str
    binding: str
    outcome: str
    reason_code: str
    reason: str
    #: ``applied``/``withheld``/... or a proposal lifecycle (``TechLeadCharterDecision.effect``).
    effect: str
    execution_reason: str | None
    target_number: int | None
    anchor_issue_number: int
    proposal_issue_number: int | None
    decided_at: AwareDatetime
    #: When its effect was applied; None when it never took effect.
    applied_at: AwareDatetime | None


class CharterDecisionsInput(_Closed):
    coverage: Coverage
    decisions: tuple[StagedDecision, ...]


class CaseFileObservationInput(_Closed):
    observation_id: str
    recorded_at: AwareDatetime


class StagedCaseFile(_Closed):
    #: ``case-file:<signature>``.
    id: str
    signature: str
    issue_number: int
    recorded_at: AwareDatetime
    observation_count: int
    fix_class: str
    area: str
    #: The lifecycle as the COPY holds it: its writer keeps no time, so it may
    #: be later than the cutoff. Context, never evidence of a notice.
    disposition: str
    retirement_pending: bool
    #: The diagnosis the tech lead recorded for it, as of the cutoff.
    body: str
    #: False when an observation after the cutoff may have rewritten it: the
    #: body is then unknown and staged empty.
    body_known: bool
    observations: tuple[CaseFileObservationInput, ...]


class StagedDiagnosis(_Closed):
    """One tech-lead run in the window: what it looked at and what it said."""

    #: ``tech-lead-run:<run_id>:<session_name>``.
    id: str
    run_key: str
    scope_kind: str
    flavor: str
    phase: str
    started_at: AwareDatetime
    ended_at: AwareDatetime | None
    subject_issue_number: int
    subject_title: str
    anchor_issue_number: int
    body: str
    findings: int
    proposals: int


class CaseFilesInput(_Closed):
    #: The case-file ledger's: complete from its first record on.
    coverage: Coverage
    case_files: tuple[StagedCaseFile, ...]
    #: Never complete: the run history is a best-effort receipt (its writer
    #: drops a failed write), so a missing run proves nothing.
    diagnoses_coverage: Coverage
    diagnoses: tuple[StagedDiagnosis, ...]


InterventionKind = Literal["reset_retry", "proposal_approved", "proposal_declined", "operator_pause"]


class Intervention(_Closed):
    at: AwareDatetime
    kind: InterventionKind
    subject: str
    detail: str


class InterventionsInput(_Closed):
    """The operator interventions the engine's own records show.

    Never complete: an operator removing ``needs-human`` on GitHub, or the
    dashboard's retry/dismiss buttons, leave no local record, so a count over
    this file is a floor. ``not_derivable`` names what is missing.
    """

    window_from: AwareDatetime
    window_to: AwareDatetime
    complete: Literal[False] = False
    derived_from: tuple[str, ...]
    not_derivable: tuple[str, ...]
    interventions: tuple[Intervention, ...]


class OpenIssue(_Closed):
    number: int
    title: str
    labels: tuple[str, ...]


class OpenIssuesInput(_Closed):
    """Every open issue of the repository the improver's outputs are filed in."""

    repo: str
    read_at: AwareDatetime
    issues: tuple[OpenIssue, ...]


class NeedsHumanCauseInput(_Closed):
    """One recorded cause of an item's needs-human block, byte for byte."""

    cause: str
    reason: str


class BlockingLabelInput(_Closed):
    """One blocking label an item carries, and when it was last put on."""

    label: str
    #: When the retained timeline last shows it put on, with no later
    #: removal; None when the retained timeline holds no such event (it may
    #: have been trimmed, or never recorded).
    since_at: AwareDatetime | None
    #: The timeline event that put it on.
    since_event: str | None


class BlockEventInput(_Closed):
    """One timeline event about an item's block (a blocking label put on or
    taken off, a block or needs-human request with its reason or question)."""

    at: AwareDatetime
    event: str
    detail: str


class PipelineEventInput(_Closed):
    """One timeline event of a pull request's pipeline: a review or rework
    queued, started, skipped or finished, a PR label change, a publication."""

    at: AwareDatetime
    event: str
    detail: str


class ItemPullRequestInput(_Closed):
    """One OPEN pull request of a blocked item: what is downstream of the
    block, and whether it is moving."""

    number: int
    draft: bool
    #: Its most recent events on the item's retained timeline, oldest first
    #: (``timeline_coverage`` says what was retained).
    pipeline_events: tuple[PipelineEventInput, ...]
    #: Its latest retained pipeline event; None when none was retained.
    last_event_at: AwareDatetime | None


class StalledWorkRef(_Closed):
    """A ``refused_work`` anomaly of ``audit.json`` about the item or one of
    its pull requests: the engine refusing the item's own downstream work
    again and again (a PR's review dropped on every scan while the issue is
    blocked). Its key is ``(kind, subject, signature)``."""

    kind: str
    subject: str
    signature: str
    detail: str


class BlockedItem(_Closed):
    """One open issue of the audited repository that is blocked."""

    number: int
    title: str
    labels: tuple[str, ...]
    blocking_labels: tuple[BlockingLabelInput, ...] = Field(min_length=1)
    #: When it became blocked: the earliest ``since_at`` of its blocking
    #: labels when every one is known; None otherwise (it may be older).
    blocked_since: AwareDatetime | None
    #: The causes recorded for its needs-human block; None when the claim
    #: store could not be read (``causes_coverage`` says why).
    needs_human_causes: tuple[NeedsHumanCauseInput, ...] | None
    #: The most recent timeline events about its block, oldest first.
    block_events: tuple[BlockEventInput, ...]
    #: What of the item's timeline was read: from its oldest retained event.
    timeline_coverage: Coverage
    #: Every tech-lead charter decision about it (it is the target, or the
    #: anchor of a decision with no target), from the WHOLE ledger as of the
    #: cutoff, not only the observation window.
    decisions: tuple[StagedDecision, ...]
    #: ``case-files.json`` case files whose diagnosis names ``#<number>``.
    case_file_ids: tuple[str, ...]
    #: ``case-files.json`` diagnoses (tech-lead runs) about it: its subject,
    #: or naming ``#<number>``.
    diagnosis_ids: tuple[str, ...]
    #: Its open pull requests (its ``<n>-`` branch, or a closing/``Refs``
    #: link): published work the block may be holding up.
    open_prs: tuple[ItemPullRequestInput, ...]
    #: Every ``refused_work`` anomaly about it or one of its open PRs.
    stalled_work: tuple[StalledWorkRef, ...]


class BlockedItemsInput(_Closed):
    """``blocked-items.json``: every blocked item of the audited engine.

    The operator's objective is that blocked items get resolved, so the
    improver accounts for each one and grades the tech lead on it (#7490).
    Every item here is present after the engine start: the listing was read
    at ``read_at``, after it.
    """

    read_at: AwareDatetime
    #: Which labels make an open issue blocked.
    blocking_rule: str
    #: The claim store's needs-human causes: current rows only, as copied.
    causes_coverage: Coverage
    #: The charter ledger the per-item ``decisions`` come from.
    decisions_coverage: Coverage
    items: tuple[BlockedItem, ...]


class ScorecardHead(BaseModel):
    """The part of a tech-lead exam scorecard (``Scorecard.to_dict``) staging reads."""

    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: Literal[1]
    case_id: str = Field(min_length=1)
    engine_commit: str = Field(min_length=1)
    passed: bool
    failures: tuple[str, ...]


class StagedInput(_Closed):
    """One Inputs-table entry: staged, or why not."""

    name: str
    staged: bool
    detail: str


class InputsManifest(_Closed):
    """``inputs.json``: what was staged, when, for which engine."""

    staged_at: AwareDatetime
    #: The audited engine's Control Center key; every finding's ``engine.id``.
    engine_id: str = Field(min_length=1)
    #: The repository the audited engine works; every finding's ``engine.repo``.
    audited_repo: str = Field(min_length=1)
    outputs_repo: str
    inputs: tuple[StagedInput, ...]
    #: Every exam case id that exists (the registry and every staged
    #: scorecard): a proposed exam case must use a new one.
    existing_exam_case_ids: tuple[str, ...]
    #: Whether ``exam/`` holds a previous scorecard for exactly the cases it
    #: holds a latest one for: only then is an exam trend comparable.
    exam_scores_comparable: bool


def to_json(model: BaseModel) -> str:
    """A staged document as JSON text, with ``from`` spelt as the prompt does."""
    return model.model_dump_json(indent=2, by_alias=True) + "\n"


__all__ = [
    "AUDIT_DIFF_FILE",
    "AUDIT_FILE",
    "AUDIT_PREVIOUS_FILE",
    "BLOCKED_ITEMS_FILE",
    "CASE_FILES_FILE",
    "CHARTER_DECISIONS_FILE",
    "CHARTER_FILE",
    "ENGINE_SOURCE_DIRNAME",
    "ENGINE_START_FILE",
    "EXAM_DIRNAME",
    "IMPROVER_DATA_DIRNAME",
    "INPUTS_FILE",
    "INTERVENTIONS_FILE",
    "OPEN_ISSUES_FILE",
    "PREVIOUS_SCORECARD_SUFFIX",
    "BlockEventInput",
    "BlockedItem",
    "BlockedItemsInput",
    "ItemPullRequestInput",
    "PipelineEventInput",
    "StalledWorkRef",
    "BlockingLabelInput",
    "CaseFileObservationInput",
    "CaseFilesInput",
    "CharterDecisionsInput",
    "Coverage",
    "EngineStartInput",
    "InputsManifest",
    "Intervention",
    "NeedsHumanCauseInput",
    "InterventionsInput",
    "OpenIssue",
    "OpenIssuesInput",
    "ScorecardHead",
    "StagedCaseFile",
    "StagedDecision",
    "StagedDiagnosis",
    "StagedInput",
    "to_json",
]
