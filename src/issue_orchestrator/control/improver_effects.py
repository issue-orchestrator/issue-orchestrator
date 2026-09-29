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
  names the evidence it lacks.

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

from ..contracts.improver_findings import Finding, ImproverFindings
from ..contracts.improver_run import EffectReceipt, EffectStatus, ImproverRunRecord
from ..ports.engine_audit import OpenIssueLabels

#: Every improver issue carries this label, so an operator can find them all.
IMPROVER_LABEL = "improver"
#: A proposal waits for this decision; the orchestrator never applies one.
OPERATOR_DECISION_LABEL = "needs-operator-decision"
_LABELS: dict[str, tuple[str, ...]] = {
    "exam_case": (IMPROVER_LABEL, "improver:exam-case"),
    "capability_issue": (IMPROVER_LABEL, "improver:capability"),
    "charter_proposal": (IMPROVER_LABEL, OPERATOR_DECISION_LABEL),
    "prompt_proposal": (IMPROVER_LABEL, OPERATOR_DECISION_LABEL),
    "needs_investigation": (IMPROVER_LABEL, "improver:investigation"),
}


def finding_key(finding: Finding, audited_repo: str) -> str:
    """A finding's identity across runs: what it asks for, about which
    anomalies of which engine (two engines' anomalies share keys like #500)."""
    identity = {
        "audited_repo": audited_repo,
        "output": finding.output,
        "anomalies": sorted(list(k.key) for k in finding.anomaly_keys),
        "case_id": finding.reproduction.case_id if finding.reproduction else None,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]


def title_token(key: str) -> str:
    return f"[improver:{key}]"


def finding_marker(key: str) -> str:
    return f"<!-- io-improver-finding:{key} -->"


def planned_effects(findings: ImproverFindings, audited_repo: str) -> tuple[EffectReceipt, ...]:
    """One pending effect per accepted finding."""
    return tuple(
        EffectReceipt(finding_id=f.id, key=finding_key(f, audited_repo), status=EffectStatus.PENDING)
        for f in findings.findings
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
    finding: Finding,
    key: str,
    open_issues: Mapping[int, OpenIssueLabels],
) -> ImproverEffectCommand:
    """The one GitHub effect an accepted finding asks for, deduplicated
    against ``open_issues``."""
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
            body=f"{marker}\n**Improver run `{run.run_id}`** saw this again"
            f" (engine `{run.engine_commit}`): stalled at `{finding.stall_point}`.\n\n"
            f"{_finding_json(finding)}",
        )
    marker = finding_marker(key)
    return FileImproverIssue(
        title=f"{title_token(key)} {_summary(finding)}",
        marker=marker,
        body=f"{marker}\n{issue_body(run, finding)}",
        labels=_LABELS[finding.output],
    )


def _summary(finding: Finding) -> str:
    if finding.output == "exam_case" and finding.reproduction and finding.reproduction.case_id:
        return f"Exam case {finding.reproduction.case_id}: {finding.id}"
    return {
        "capability_issue": f"Capability gap: {finding.id}",
        "charter_proposal": f"Charter proposal (operator decision): {finding.id}",
        "prompt_proposal": f"Prompt proposal (operator decision): {finding.id}",
        "needs_investigation": f"Investigate: {finding.id}",
    }.get(finding.output, finding.id)


def issue_body(run: ImproverRunRecord, finding: Finding) -> str:
    """The issue an accepted finding files: what it asks for, then its JSON."""
    return "\n\n".join(
        (
            f"Filed by the tech-lead improver (#7490), run `{run.run_id}` against"
            f" `{run.audited_repo}` at engine `{run.engine_commit}`.",
            f"**Stalled at:** `{finding.stall_point}`. **Classification:** `{finding.classification}`.",
            _definition_of_done(finding),
            _finding_json(finding),
        )
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
    return "```json\n" + finding.model_dump_json(indent=2, by_alias=True) + "\n```"


__all__ = [
    "IMPROVER_LABEL",
    "OPERATOR_DECISION_LABEL",
    "CommentImproverEvidence",
    "FileImproverIssue",
    "ImproverEffectCommand",
    "TrackedIssueNotOpen",
    "finding_key",
    "finding_marker",
    "issue_body",
    "plan_effect",
    "planned_effects",
    "title_token",
]
