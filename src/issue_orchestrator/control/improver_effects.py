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

Deduplication is against OPEN issues: every filed issue's title carries the
finding's identity (``[improver:<key>]``), so a finding an open issue already
carries — filed by an earlier run, or by this one before a crash lost its
receipt — gets its evidence commented there instead of a second issue. A
comment carries a marker for its run and finding, so it is posted once.

A rate limit stops the batch: what is left stays ``pending`` on the run, and
the next application resumes it. Any other failure stops it too, recorded on
the receipt as its ``error`` (the run then exits unavailable). Every body
carries the finding's JSON.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..contracts.improver_findings import DesignFinding, Finding, ImproverFindings
from ..contracts.improver_run import EffectReceipt, EffectStatus, ImproverRunRecord
from ..domain.engine_activity import EngineRef
from ..ports.engine_audit import OpenIssueLabels

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


def title_token(key: str) -> str:
    return f"[improver:{key}]"


def finding_marker(key: str) -> str:
    return f"<!-- io-improver-finding:{key} -->"


def planned_effects(
    findings: ImproverFindings, engine: EngineRef, original_ids: Mapping[str, str] | None = None
) -> tuple[EffectReceipt, ...]:
    """One pending effect per accepted finding and design finding.

    ``original_ids``: a design finding renamed by a multi-heat merge keeps the
    effect key of the id its heat wrote, so it deduplicates the same way
    whatever the run's other findings were (#8001)."""
    originals = original_ids or {}
    return (
        *(
            EffectReceipt(finding_id=f.id, key=finding_key(f, engine), status=EffectStatus.PENDING)
            for f in findings.findings
        ),
        *(
            EffectReceipt(
                finding_id=d.id,
                key=design_finding_key(d.model_copy(update={"id": originals.get(d.id, d.id)}), engine),
                status=EffectStatus.PENDING,
            )
            for d in findings.design_findings
        ),
    )


@dataclass(frozen=True)
class FileImproverIssue:
    """File one new issue for a finding. ``marker`` is in its body, so a
    creation whose result was lost can be proven filed or absent."""

    title: str
    marker: str
    body: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class CommentImproverEvidence:
    """Comment a finding's evidence on an existing issue, once per run."""

    issue_number: int
    marker: str
    body: str


ImproverEffectCommand = FileImproverIssue | CommentImproverEvidence


class TrackedIssueNotOpen(RuntimeError):
    """A tracked finding's issue closed after the inputs were staged."""


def plan_effect(
    run: ImproverRunRecord,
    finding: Finding | DesignFinding,
    key: str,
    open_issues: Mapping[int, OpenIssueLabels],
) -> ImproverEffectCommand:
    """The one GitHub effect an accepted finding asks for, deduplicated
    against ``open_issues``."""
    if isinstance(finding, DesignFinding):
        return _plan_design_effect(run, finding, key, open_issues)
    target = finding.tracked_issue if finding.classification == "tracked" else None
    if target is not None and target not in open_issues:
        raise TrackedIssueNotOpen(
            f"tracked issue #{target} of {finding.id} is no longer open; its evidence is not"
            " commented on a closed issue"
        )
    if target is None:
        token = title_token(key)
        target = next((n for n, i in sorted(open_issues.items()) if token in i.title), None)
    if target is not None:
        marker = f"<!-- io-improver:{run.run_id}:{finding.id} -->"
        return CommentImproverEvidence(
            issue_number=target,
            marker=marker,
            body=f"{marker}\n**Improver run `{run.run_id}`** saw this again on `{run.audited_repo}`"
            f" (engine `{run.engine_id}` at `{run.engine_commit}`): stalled at"
            f" `{finding.stall_point}`." + support_note(run, finding.id) + "\n\n"
            f"{_finding_json(finding)}",
        )
    marker = finding_marker(key)
    route = effect_route(run, finding)
    labels = _LABELS[finding.output]
    return FileImproverIssue(
        title=f"{title_token(key)} {_summary(finding)} ({run.audited_repo})",
        marker=marker,
        body=f"{marker}\n{issue_body(run, finding)}",
        labels=(*labels, TARGET_OPERATOR_LABEL) if route is EffectRoute.TARGET_OPERATOR else labels,
    )


def _plan_design_effect(
    run: ImproverRunRecord, design: DesignFinding, key: str, open_issues: Mapping[int, OpenIssueLabels]
) -> ImproverEffectCommand:
    token = title_token(key)
    filed = next((n for n, i in sorted(open_issues.items()) if token in i.title), None)
    if filed is not None:
        marker = f"<!-- io-improver:{run.run_id}:{design.id} -->"
        return CommentImproverEvidence(
            issue_number=filed,
            marker=marker,
            body=f"{marker}\n**Improver run `{run.run_id}`** found this again on `{run.audited_repo}`"
            f" (engine `{run.engine_id}` at `{run.engine_commit}`)." + support_note(run, design.id)
            + f"\n\n{_design_json(design)}",
        )
    marker = finding_marker(key)
    return FileImproverIssue(
        title=f"{token} Design ({design.kind.replace('_', ' ')}): {design.id} ({run.audited_repo})",
        marker=marker,
        body=f"{marker}\n{design_issue_body(run, design)}",
        labels=DESIGN_LABELS,
    )


def design_issue_body(run: ImproverRunRecord, design: DesignFinding) -> str:
    """The issue an accepted design finding files: the claim, its quoted
    evidence, the proposed change, then its JSON."""
    evidence = "\n".join(
        f"- `{_inert(c.path)}:{c.line}`: \"{_inert(c.quote)}\""
        if c.kind == "file"
        else f"- toolbox call {c.call}: \"{_inert(c.quote)}\""
        for c in design.evidence
    )
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
    heats are stronger evidence than one), and what other heats claimed
    that could not be merged into it, for the operator to resolve (#8001)."""
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
    return note


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
    "CommentImproverEvidence",
    "EffectRoute",
    "FileImproverIssue",
    "ImproverEffectCommand",
    "TrackedIssueNotOpen",
    "design_finding_key",
    "design_issue_body",
    "effect_route",
    "finding_key",
    "finding_marker",
    "issue_body",
    "plan_effect",
    "support_note",
    "planned_effects",
    "title_token",
]
