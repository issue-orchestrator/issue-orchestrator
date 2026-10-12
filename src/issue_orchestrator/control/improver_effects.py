"""What the orchestrator does on GitHub for the improver's accepted findings (#7490).

The improver only writes intent. This is the policy that turns each accepted
finding into exactly one GitHub effect (:func:`plan_effect`); the applier that
executes it through the repository-host port and records the receipt on the
run is :mod:`..execution.improver_effect_applier`:

* a ``tracked`` finding comments its evidence on the open tracked issue;
* any other finding files one issue, labelled by what it asks for:
  ``exam_case`` and ``capability_issue`` carry the reproduce-first definition
  of done, a ``charter_proposal`` or ``prompt_proposal`` waits for the
  operator's decision (nothing is ever applied), a ``needs_investigation``
  names the evidence it lacks;
* a design finding (#8001) files one issue for the operator's decision:
  what the model of the world gets wrong, its quoted evidence, and the
  proposed change. Nothing is applied.

Every effect is filed in io (the outputs repository), never in the audited
engine's repository: the improver writes nothing to a target repository. What
a finding is ABOUT decides who acts on it (:func:`effect_route`): a finding
about io's code or prompts is io's; a ``charter_proposal`` from another
repository's engine concerns that repository's own configuration (its
tech-lead charter), so it is labelled :data:`TARGET_OPERATOR_LABEL` and names
the repository whose operator decides. Every title and body names the engine
and repository the finding came from.

Untrusted text never carries a marker: every body and comment is built from
the improver's text through :func:`_inert`, so a finding cannot plant the
marker by which another finding's issue is later recognized (#8001 r1 F3).

One defect is filed once (#8700): :class:`EffectIdentity` is the one
identity of findings, their effect keys and the profile by which a run's
merge folds a design finding into another finding about the same defect;
the folded finding's claim is shown on that finding's issue
(:func:`support_note`).

Deduplication is against OPEN issues (:class:`OpenIssues`): every filed
issue's title carries the finding's identity (``[improver:<key>]``), so a
finding an open issue already carries — filed by an earlier run, or by this
one before a crash lost its receipt — gets its evidence commented there
instead of a second issue. A comment carries a marker for its run and
finding, so it is posted once.

A key is not the whole identity across runs (#8986): the next run words a
design finding's slug anew, and a stall finding and a design finding about
one defect never share a key. So every filed issue's first line also records
its finding's defect profile (:func:`defect_marker`), and a finding about the
same defect as an open issue an EARLIER run filed (one code site and
overlapping evidence, :func:`~..domain.improver_defects.same_defect`) gets its
evidence commented there. Within one run the merge already decided which
findings are one defect (#8700), so a run's own issues are never matched.

A rate limit stops the batch: what is left stays ``pending`` on the run, and
the next application resumes it. Any other failure stops it too, recorded on
the receipt as its ``error`` (the run then exits unavailable). Every body
carries the finding's JSON.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..contracts.improver_findings import (
    AgentEdit,
    BudgetEdit,
    DesignFinding,
    Finding,
    HeatsEdit,
    ImproverChange,
    ImproverFindings,
    ModeEdit,
    PromptEdit,
)
from ..contracts.improver_run import (
    EffectReceipt,
    EffectStatus,
    FiledDefect,
    ImproverRunRecord,
)
from ..domain.engine_activity import EngineRef
from ..domain.improver_champion import CHANGE_ID
from ..domain.improver_defects import (
    DefectProfile,
    EngineSource,
    design_profile,
    profile_record,
    recorded_profile,
    same_defect,
    stall_profile,
)
from ..ports.engine_audit import OpenIssueBody, OpenIssueLabels

#: Every improver issue carries this label, so an operator can find them all.
IMPROVER_LABEL = "improver"
#: A proposal waits for this decision; the orchestrator never applies one.
OPERATOR_DECISION_LABEL = "needs-operator-decision"
#: A proposal about a target repository's own configuration, for ITS operator.
TARGET_OPERATOR_LABEL = "improver:target-operator"
_LABELS: dict[str, tuple[str, ...]] = {
    "exam_case": (IMPROVER_LABEL, "improver:exam-case"),
    "capability_issue": (IMPROVER_LABEL, "improver:capability"),
    "charter_proposal": (IMPROVER_LABEL, OPERATOR_DECISION_LABEL),
    "prompt_proposal": (IMPROVER_LABEL, OPERATOR_DECISION_LABEL),
    "needs_investigation": (IMPROVER_LABEL, "improver:investigation"),
}
#: A design finding: the operator decides whether the model changes.
DESIGN_LABELS: tuple[str, ...] = (IMPROVER_LABEL, OPERATOR_DECISION_LABEL, "improver:design")
#: A challenger to the improver itself: tried, then a maintainer approves it.
CHALLENGER_LABELS: tuple[str, ...] = (IMPROVER_LABEL, OPERATOR_DECISION_LABEL, "improver:challenger")
#: How an improver issue's first line records the defect it is about (#8986).
DEFECT_MARKER = "<!-- io-improver-defect:"


class EffectRoute(StrEnum):
    #: About io's code, prompts or exam: io's own work.
    IO = "io"
    #: About the audited repository's own configuration: a proposal for its operator.
    TARGET_OPERATOR = "target_operator"


def effect_route(run: ImproverRunRecord, finding: Finding) -> EffectRoute:
    """Who acts on a finding. Only a charter proposal is about a repository's
    own configuration (the effective charter is its config's); for io's own
    engine that operator is io's, so only another repository's engine routes
    one to its operator."""
    if finding.output == "charter_proposal" and run.audited_repo != run.outputs_repo:
        return EffectRoute.TARGET_OPERATOR
    return EffectRoute.IO


def finding_key(finding: Finding, engine: EngineRef) -> str:
    """A finding's identity across runs: what it asks for, about which
    anomalies of which engine (two engines' anomalies share keys like #500,
    and two engines may work one repository)."""
    identity = {
        "engine_id": engine.engine_id,
        "audited_repo": engine.repo,
        "output": finding.output,
        "anomalies": sorted(list(k.key) for k in finding.anomaly_keys),
        "case_id": finding.reproduction.case_id if finding.reproduction else None,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]


def design_finding_key(design: DesignFinding, engine: EngineRef) -> str:
    """A design finding's identity across runs: its kind and slug, on which engine."""
    identity = {
        "engine_id": engine.engine_id,
        "audited_repo": engine.repo,
        "design": design.kind,
        "id": design.id,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class EffectIdentity:
    """The one identity of one engine's findings (a
    :class:`~..domain.improver_heats.FindingIdentity`): the effect keys
    their issues are deduplicated by, and where each defect lives in the
    engine's staged source, which relates findings of different keys."""

    engine: EngineRef
    source: EngineSource

    def stall_key(self, finding: Finding) -> str:
        return finding_key(finding, self.engine)

    def design_key(self, design: DesignFinding) -> str:
        return design_finding_key(design, self.engine)

    def stall_profile(self, finding: Finding) -> DefectProfile:
        return stall_profile(finding, self.source)

    def design_profile(self, design: DesignFinding) -> DefectProfile:
        return design_profile(design, self.source)


def change_key(change: ImproverChange, base: str) -> str:
    """A proposed change's identity: what it changes, in which champion."""
    identity = {"champion": base, "edit": change.edit.model_dump(mode="json")}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]


def title_token(key: str) -> str:
    return f"[improver:{key}]"


def finding_marker(key: str) -> str:
    return f"<!-- io-improver-finding:{key} -->"


def comment_marker(run: ImproverRunRecord, finding_id: str) -> str:
    """A run's comment for one finding: posted once, whatever issue it is on."""
    return f"<!-- io-improver:{run.run_id}:{finding_id} -->"


def planned_effects(
    findings: ImproverFindings,
    identity: EffectIdentity,
    original_ids: Mapping[str, str] | None = None,
    *,
    change_base: str | None = None,
) -> tuple[EffectReceipt, ...]:
    """One pending effect per accepted finding and design finding, each with
    its defect's profile as a later run compares it (#8986), and one for the
    change to the improver an invited run proposed (``change_base``: the
    champion it was invited to challenge).

    ``original_ids``: a design finding renamed by a multi-heat merge keeps the
    effect key of the id its heat wrote, so it deduplicates the same way
    whatever the run's other findings were (#8001)."""
    originals = original_ids or {}
    return (
        *(
            EffectReceipt(
                finding_id=f.id,
                key=identity.stall_key(f),
                profile=profile_record(identity.stall_profile(f)),
                status=EffectStatus.PENDING,
            )
            for f in findings.findings
        ),
        *(
            EffectReceipt(
                finding_id=d.id,
                key=identity.design_key(d.model_copy(update={"id": originals.get(d.id, d.id)})),
                profile=profile_record(identity.design_profile(d)),
                status=EffectStatus.PENDING,
            )
            for d in findings.design_findings
        ),
        *(
            (EffectReceipt(finding_id=CHANGE_ID, key=change_key(change, change_base), status=EffectStatus.PENDING),)
            if (change := findings.improver_change) is not None and change_base is not None
            else ()
        ),
    )


@dataclass(frozen=True)
class FileImproverIssue:
    """File one new issue for a finding. ``marker`` is in its body, so a
    creation whose result was lost can be proven filed or absent; so is
    ``defect``, the defect the issue is about (None for a proposed change)."""

    title: str
    marker: str
    body: str
    labels: tuple[str, ...]
    defect: FiledDefect | None = None


@dataclass(frozen=True)
class CommentImproverEvidence:
    """Comment a finding's evidence on an existing issue, once per run."""

    issue_number: int
    marker: str
    body: str


ImproverEffectCommand = FileImproverIssue | CommentImproverEvidence


class TrackedIssueNotOpen(RuntimeError):
    """A tracked finding's issue closed after the inputs were staged."""


def defect_marker(defect: FiledDefect) -> str:
    """The marker by which a later run knows the defect an issue is about:
    its JSON, in which no text can end the comment (``<`` and ``>`` are
    escaped, as JSON allows)."""
    text = defect.model_dump_json().replace("<", "\\u003c").replace(">", "\\u003e")
    return f"{DEFECT_MARKER}{text} -->"


def filed_defect(body: str) -> FiledDefect | None:
    """The defect an issue's FIRST line records: only that line is the
    orchestrator's own, the rest is built from untrusted text made inert.
    None when it records none; a marker that is not a whole defect raises,
    since an issue would then be filed again for a defect it already carries."""
    first = body.split("\n", 1)[0]
    start = first.find(DEFECT_MARKER)
    if start < 0:
        return None
    end = first.find("-->", start)
    if end < 0:
        raise ValueError("an improver issue's defect marker is not closed on its first line")
    return FiledDefect.model_validate_json(first[start + len(DEFECT_MARKER) : end])


class OpenIssues:
    """What the effects are deduplicated against, read once per application:
    every open issue's title and labels, and the defect each one's body
    records (#8986). An application adds each issue it files."""

    def __init__(self, listed: Iterable[OpenIssueLabels], defects: Mapping[int, FiledDefect]) -> None:
        self._listed = {i.number: i for i in listed}
        self._defects = dict(defects)

    @classmethod
    def read(cls, listed: Iterable[OpenIssueLabels], bodies: Iterable[OpenIssueBody]) -> OpenIssues:
        """The open issues as the host lists them, with the defect each
        body records (the bodies holding :data:`DEFECT_MARKER`)."""
        defects: dict[int, FiledDefect] = {}
        for issue in bodies:
            try:
                defect = filed_defect(issue.body)
            except ValueError as error:
                raise ValueError(f"open issue #{issue.number}'s defect marker records no defect: {error}") from error
            if defect is not None:
                defects[issue.number] = defect
        return cls(listed, defects)

    def is_open(self, number: int) -> bool:
        return number in self._listed

    def carrying(self, key: str) -> int | None:
        """The open issue whose title carries the finding ``key``."""
        token = title_token(key)
        return next((n for n, i in sorted(self._listed.items()) if token in i.title), None)

    def same_defect(self, run: ImproverRunRecord, receipt: EffectReceipt) -> tuple[int, ...]:
        """The open issues an EARLIER run filed about the receipt's defect on
        the run's engine, oldest first.

        Two engines' items share numbers and their logs share paths, so only
        one engine's issues are compared. Several may match (two runs filed
        one defect before this rule did): all of them are the same defect, so
        a new issue would duplicate each, and would itself match the next
        run's finding; the finding is evidence for the oldest."""
        if receipt.profile is None:
            return ()
        mine = recorded_profile(receipt.profile, receipt.finding_id)
        return tuple(
            number
            for number, filed in sorted(self._defects.items())
            if filed.run_id != run.run_id
            and (filed.engine_id, filed.audited_repo) == (run.engine_id, run.audited_repo)
            and same_defect(mine, recorded_profile(filed.profile, f"#{number}"))
        )

    def filed(self, issue: OpenIssueLabels, defect: FiledDefect | None) -> None:
        """An issue this application filed, or proved filed and open, is open now."""
        self._listed[issue.number] = issue
        if defect is not None:
            self._defects[issue.number] = defect

    def with_issue(self, issue: OpenIssueLabels) -> OpenIssues:
        """These open issues and ``issue``, proven open by its body marker."""
        return OpenIssues((*self._listed.values(), issue), self._defects)


def plan_effect(
    run: ImproverRunRecord,
    finding: Finding | DesignFinding | ImproverChange,
    receipt: EffectReceipt,
    open_issues: OpenIssues,
) -> ImproverEffectCommand:
    """The one GitHub effect an accepted finding asks for, deduplicated
    against ``open_issues``: by its key, then by its defect (#8986)."""
    if isinstance(finding, ImproverChange):
        return _plan_change_effect(run, finding, receipt.key, open_issues)
    if isinstance(finding, DesignFinding):
        return _plan_design_effect(run, finding, receipt, open_issues)
    target = finding.tracked_issue if finding.classification == "tracked" else None
    if target is not None and not open_issues.is_open(target):
        raise TrackedIssueNotOpen(
            f"tracked issue #{target} of {finding.id} is no longer open; its evidence is not"
            " commented on a closed issue"
        )
    if target is None:
        target = open_issues.carrying(receipt.key)
    if target is not None:
        marker = comment_marker(run, finding.id)
        return CommentImproverEvidence(
            issue_number=target,
            marker=marker,
            body=f"{marker}\n**Improver run `{run.run_id}`** saw this again on `{run.audited_repo}`"
            f" (engine `{run.engine_id}` at `{run.engine_commit}`): stalled at"
            f" `{finding.stall_point}`." + support_note(run, finding.id) + "\n\n"
            f"{_finding_json(finding)}",
        )
    if same := open_issues.same_defect(run, receipt):
        return _same_defect_comment(run, finding, same)
    marker = finding_marker(receipt.key)
    defect = _filed_defect(run, receipt)
    route = effect_route(run, finding)
    labels = _LABELS[finding.output]
    return FileImproverIssue(
        title=f"{title_token(receipt.key)} {_summary(finding)} ({run.audited_repo})",
        marker=marker,
        body=f"{_first_line(marker, defect)}\n{issue_body(run, finding)}",
        labels=(*labels, TARGET_OPERATOR_LABEL) if route is EffectRoute.TARGET_OPERATOR else labels,
        defect=defect,
    )


def _plan_design_effect(
    run: ImproverRunRecord, design: DesignFinding, receipt: EffectReceipt, open_issues: OpenIssues
) -> ImproverEffectCommand:
    token = title_token(receipt.key)
    filed = open_issues.carrying(receipt.key)
    if filed is not None:
        marker = comment_marker(run, design.id)
        return CommentImproverEvidence(
            issue_number=filed,
            marker=marker,
            body=f"{marker}\n**Improver run `{run.run_id}`** found this again on `{run.audited_repo}`"
            f" (engine `{run.engine_id}` at `{run.engine_commit}`)." + support_note(run, design.id)
            + f"\n\n{_design_json(design)}",
        )
    if same := open_issues.same_defect(run, receipt):
        return _same_defect_comment(run, design, same)
    marker = finding_marker(receipt.key)
    defect = _filed_defect(run, receipt)
    return FileImproverIssue(
        title=f"{token} Design ({design.kind.replace('_', ' ')}): {design.id} ({run.audited_repo})",
        marker=marker,
        body=f"{_first_line(marker, defect)}\n{design_issue_body(run, design)}",
        labels=DESIGN_LABELS,
        defect=defect,
    )


def _filed_defect(run: ImproverRunRecord, receipt: EffectReceipt) -> FiledDefect | None:
    if receipt.profile is None:
        return None
    return FiledDefect(
        run_id=run.run_id, engine_id=run.engine_id, audited_repo=run.audited_repo, profile=receipt.profile
    )


def _first_line(marker: str, defect: FiledDefect | None) -> str:
    """A filed body's first line: the orchestrator's markers, and nothing else."""
    return marker if defect is None else f"{marker} {defect_marker(defect)}"


def _same_defect_comment(
    run: ImproverRunRecord, finding: Finding | DesignFinding, issues: tuple[int, ...]
) -> CommentImproverEvidence:
    """A finding about the defect of open issues an earlier run filed: its
    evidence on the oldest, naming the others."""
    target, *others = issues
    marker = comment_marker(run, finding.id)
    if isinstance(finding, DesignFinding):
        claim = f"**Design finding `{finding.id}` (`{finding.kind}`):** {_inert(finding.summary)}"
        document = _design_json(finding)
    else:
        claim = (
            f"**Finding `{finding.id}` (`{finding.output}`):** stalled at `{finding.stall_point}`."
            f" **Classification:** `{finding.classification}`."
        )
        document = _finding_json(finding)
    also = f" It is the same defect as {', '.join(f'#{n}' for n in others)} too." if others else ""
    return CommentImproverEvidence(
        issue_number=target,
        marker=marker,
        body=f"{marker}\n**Improver run `{run.run_id}`** found this issue's defect again on"
        f" `{run.audited_repo}` (engine `{run.engine_id}` at `{run.engine_commit}`), worded as another"
        " finding: one code site and overlapping evidence, so it is evidence here, not a new issue."
        + also + support_note(run, finding.id) + f"\n\n{claim}\n\n{document}",
    )


def _plan_change_effect(
    run: ImproverRunRecord, change: ImproverChange, key: str, open_issues: OpenIssues
) -> ImproverEffectCommand:
    token = title_token(key)
    filed = open_issues.carrying(key)
    if filed is not None:
        marker = comment_marker(run, CHANGE_ID)
        return CommentImproverEvidence(
            issue_number=filed, marker=marker,
            body=f"{marker}\n**Improver run `{run.run_id}`** proposed this change again.\n\n{_change_json(change)}",
        )
    marker = finding_marker(key)
    return FileImproverIssue(
        title=f"{token} Improver challenger ({change.edit.kind.replace('_', ' ')}) from run {run.run_id}",
        marker=marker,
        body=f"{marker}\n{change_issue_body(run, change)}",
        labels=CHALLENGER_LABELS,
    )


def change_issue_body(run: ImproverRunRecord, change: ImproverChange) -> str:
    """The issue a proposed change to the improver files: what it changes,
    why, and how it is tried and decided. Nothing is applied by filing it."""
    tournament = "python -m issue_orchestrator.entrypoints.cli_tools.improver_tournament"
    return "\n\n".join(
        (
            f"Proposed by tech-lead improver run `{run.run_id}` (#8001), invited to propose ONE change to"
            f" the improver's champion `{run.change_invitation}`." + support_note(run, CHANGE_ID),
            f"**Change:** {_inert(_describe_edit(change))}",
            f"**Why:** {_inert(change.why)}",
            f"**Expected effect:** {_inert(change.expected_effect)}",
            "**Motivated by the run's findings:** " + ", ".join(f"`{_inert(i)}`" for i in change.motivated_by),
            "**How it is decided (nothing is applied by this issue):**\n"
            f"1. Try it against the champion on frozen snapshots: `{tournament} challenge --run {run.run_id}"
            " --snapshot <id>`.\n"
            "2. It replaces the champion only if it is told apart ABOVE the champion on every snapshot (the"
            " tournament's noise band and heat test) AND a maintainer labels this issue `approved`.\n"
            f"3. Then: `{tournament} promote --challenge <challenge id>`.",
            _change_json(change),
        )
    )


def _describe_edit(change: ImproverChange) -> str:
    match change.edit:
        case PromptEdit(find=find, replace=replace):
            return f"in the prompt, replace {find!r} with {replace!r}"
        case AgentEdit(provider=provider, model=model):
            return f"run on {provider}:{model}"
        case ModeEdit(mode=mode):
            return f"investigate in {mode} mode"
        case HeatsEdit(heats=heats):
            return f"send {heats} heat(s) per run"
        case BudgetEdit(minutes=minutes):
            return f"give the empowered investigation {minutes} minutes"


def _change_json(change: ImproverChange) -> str:
    return "```json\n" + _inert_json(change.model_dump_json(indent=2)) + "\n```"


def design_issue_body(run: ImproverRunRecord, design: DesignFinding) -> str:
    """The issue an accepted design finding files: the claim, its quoted
    evidence, the proposed change, then its JSON."""
    evidence = _evidence_lines(design)
    return "\n\n".join(
        (
            f"Filed by the tech-lead improver (#7490, #8001), run `{run.run_id}` against"
            f" `{run.audited_repo}` (engine `{run.engine_id}` at `{run.engine_commit}`)."
            + support_note(run, design.id),
            f"**Design finding (`{design.kind}`):** {_inert(design.summary)}",
            f"**Evidence** (every quote checked against the run's evidence):\n{evidence}",
            f"**Impact:** {_inert(design.impact)}",
            "**Proposed change (operator decision required; nothing is applied):**"
            f" {_inert(design.proposed_change)}",
            _design_json(design),
        )
    )


def _evidence_lines(design: DesignFinding, *, indent: str = "") -> str:
    return "\n".join(
        f"{indent}- `{_inert(c.path)}:{c.line}`: \"{_inert(c.quote)}\""
        if c.kind == "file"
        else f"{indent}- toolbox call {c.call}: \"{_inert(c.quote)}\""
        for c in design.evidence
    )


def _design_json(design: DesignFinding) -> str:
    return "```json\n" + _inert_json(design.model_dump_json(indent=2)) + "\n```"


def _inert_title(text: str) -> str:
    """Untrusted title text that cannot pose as an ``[improver:<key>]`` token."""
    return text.replace("[", "(").replace("]", ")")


def _inert(text: str) -> str:
    """Untrusted markdown with no HTML comment: no marker, hidden or not."""
    return text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def _inert_json(text: str) -> str:
    """Untrusted JSON, still valid JSON, with no HTML comment opener in it."""
    return text.replace("<!--", "<\\u0021--")


def _summary(finding: Finding) -> str:
    if finding.output == "exam_case" and finding.reproduction and finding.reproduction.case_id:
        # The case id is the improver's text: it must not carry a title token.
        return f"Exam case {_inert_title(finding.reproduction.case_id)}: {finding.id}"
    return {
        "capability_issue": f"Capability gap: {finding.id}",
        "charter_proposal": f"Charter proposal (operator decision): {finding.id}",
        "prompt_proposal": f"Prompt proposal (operator decision): {finding.id}",
        "needs_investigation": f"Investigate: {finding.id}",
    }.get(finding.output, finding.id)


def support_note(run: ImproverRunRecord, finding_id: str) -> str:
    """How many of the run's heats found the finding (several independent
    heats are stronger evidence than one), what other heats claimed that
    could not be merged into it, for the operator to resolve (#8001), and
    the design findings folded into it as the same defect (#8700)."""
    if not run.heats:
        return ""
    found = next((s.heats for s in run.finding_support if s.finding_id == finding_id), ())
    note = f" **Found by {len(found)} of {len(run.heats)} independent heat(s)**" + (
        f" ({', '.join(map(str, found))})." if found else "."
    )
    conflicts = [c for c in run.heat_conflicts if c.finding_id == finding_id]
    if conflicts:
        note += "\n\n**Not merged, to resolve:**\n" + "\n".join(
            f"- heat {c.heat}: {_inert(c.reason)}: {_inert(c.claim)}" for c in conflicts
        )
    return note + same_defect_note(run, finding_id)


def same_defect_note(run: ImproverRunRecord, finding_id: str) -> str:
    """The design findings the run folded into this finding as the same
    defect (#8700): each one's claim and evidence, so nothing it said is
    lost, and an operator who judges it a different defect can file it."""
    folded = [s for s in run.same_defects if s.finding_id == finding_id]
    if not folded:
        return ""
    return "\n\n**Also found as the same defect (one code site, overlapping evidence), filed here once:**\n" + (
        "\n".join(_folded_design(s.design, s.heats) for s in folded)
    )


def _folded_design(design: DesignFinding, heats: tuple[int, ...]) -> str:
    """All a folded design finding claims, as its own issue would say it."""
    owner = "" if design.owner is None else f" **Owner:** `{_inert(design.owner)}`."
    return (
        f"- design finding `{_inert(design.id)}` ({design.kind.replace('_', ' ')}), heat(s)"
        f" {', '.join(map(str, heats))}: {_inert(design.summary)}{owner}\n"
        f"  **Impact:** {_inert(design.impact)}\n"
        f"  **Proposed change:** {_inert(design.proposed_change)}\n"
        + _evidence_lines(design, indent="  ")
    )


def issue_body(run: ImproverRunRecord, finding: Finding) -> str:
    """The issue an accepted finding files: what it asks for, then its JSON."""
    return "\n\n".join(
        (
            f"Filed by the tech-lead improver (#7490), run `{run.run_id}` against"
            f" `{run.audited_repo}` (engine `{run.engine_id}` at `{run.engine_commit}`)."
            + support_note(run, finding.id),
            *_route_note(run, finding),
            f"**Stalled at:** `{finding.stall_point}`. **Classification:** `{finding.classification}`.",
            _inert(_definition_of_done(finding)),
            _finding_json(finding),
        )
    )


def _route_note(run: ImproverRunRecord, finding: Finding) -> tuple[str, ...]:
    if effect_route(run, finding) is not EffectRoute.TARGET_OPERATOR:
        return ()
    return (
        f"**For the operator of `{run.audited_repo}`.** This proposal concerns that"
        " repository's own configuration (its tech-lead charter), not io's code. It is"
        " filed here because the improver never writes to a target repository; that"
        " repository's operator decides, and applies it there.",
    )


def _definition_of_done(finding: Finding) -> str:
    if finding.output == "needs_investigation":
        missing = "\n".join(f"- {m}" for m in finding.missing_evidence)
        return (
            "**Needs investigation.** No reproducible root cause yet. Done when the missing"
            f" evidence below is gathered and the anomaly is re-graded:\n{missing}"
        )
    repro = finding.reproduction
    if repro is None:
        raise ValueError(f"accepted finding {finding.id} has no reproduction")
    steps = (
        f"1. **Implement the reproduction first** (`{repro.kind}` in `{repro.harness}`"
        + (f", case id `{repro.case_id}`" if repro.case_id else "")
        + "): plant the state and assert the outcomes below, independent of any fix.\n"
        f"2. **Review confirms it FAILS on `{repro.fails_on}`** before any fix is accepted.\n"
        f"3. **If it passes on `{repro.fails_on}`, close this issue as `not_reproduced`**: the"
        " finding was wrong and counts against the improver.\n"
    )
    if finding.output == "exam_case":
        steps += (
            "4. Exam cases are add-only: never change, remove or loosen an existing case or grader.\n"
        )
    elif finding.output in ("charter_proposal", "prompt_proposal"):
        steps = (
            "**Operator decision required.** This is a proposal: the orchestrator applies"
            " nothing. If the operator accepts it:\n" + steps
        )
    else:
        steps += "4. Then fix the class, not the instance (see the same-shape sites).\n"
    return "**Definition of done**\n" + steps + "\n**Proposal:** " + (finding.proposal or "")


def _finding_json(finding: Finding) -> str:
    return "```json\n" + _inert_json(finding.model_dump_json(indent=2, by_alias=True)) + "\n```"


__all__ = [
    "IMPROVER_LABEL",
    "OPERATOR_DECISION_LABEL",
    "TARGET_OPERATOR_LABEL",
    "DESIGN_LABELS",
    "DEFECT_MARKER",
    "CommentImproverEvidence",
    "EffectIdentity",
    "EffectRoute",
    "FileImproverIssue",
    "ImproverEffectCommand",
    "OpenIssues",
    "TrackedIssueNotOpen",
    "comment_marker",
    "defect_marker",
    "design_finding_key",
    "design_issue_body",
    "effect_route",
    "filed_defect",
    "finding_key",
    "finding_marker",
    "issue_body",
    "plan_effect",
    "same_defect_note",
    "support_note",
    "planned_effects",
    "title_token",
]
