"""Triage session completion planning (ADR-0031).

Single home for what happens when a triage session ends: assignment-driven
label policy plus decision-artifact processing. Extracted from
``completion_action_planner`` so the triage owner boundary
(``triage_session_policy`` / ``TriageAssignment`` on the launch side, this
module on the completion side) lives in one cohesive seam.

Policy summary:

* Only batch-review sessions label PRs (the manifest set they audited);
  failure investigations audit one issue and never touch manifest labels
  (#6768 B4).
* Every COMPLETED triage session (either flavor) must produce a valid
  decision artifact pair — a missing/invalid pair is a contract violation.
  The authoritative classification runs in the completion processing path
  (``triage_decision_processing_error``, called by ``completion_processor``
  BEFORE status recording) so the session's history outcome is FAILED, not
  a quiet success; the action planner re-reads the same validation for its
  planning effects (#6761 finding 3).
* A failure-investigation decision must publish its diagnosis to the
  originating issue: at least one ``post_comment`` proposal targeting
  ``assignment.focus_issue_number``. Anything else — zero actions, or
  comments aimed elsewhere — is a contract violation (#6761 finding 2).
* ``create_issue`` proposals may not carry protected workflow labels
  (``triage_issue_policy`` owns the protected set) — contract violation
  (#6761 finding 4).
* No assignment (pre-upgrade session) fails safe: no labels, no decision
  processing; watch-labeled PRs simply re-enter the next batch.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.models import Session
from ..domain.triage_manifest import TriageManifest
from ..domain.triage_session import TriageAssignment, TriageSessionFlavor
from .actions import (
    Action,
    AddCommentAction,
    AddLabelAction,
    CloseIssueAction,
    RemoveLabelAction,
)
from .completion_types import ERROR_PREFIX_TRIAGE_DECISION
from .label_manager import LabelManager
from .triage_decision_actions import (
    plan_triage_decision_actions,
    plan_triage_rejection_action,
)
from .triage_decision_loader import (
    TriageArtifactLoadResult,
    TriageDecisionLoadFailure,
    load_triage_artifact_pair_for_run,
)
from .triage_issue_policy import protected_triage_label_violations
from .triage_session_policy import is_triage_session, read_triage_assignment

if TYPE_CHECKING:
    from ..domain.triage_artifacts import TriageDecision
    from ..infra.config import Config
    from .reconciliation import ExpectedState

logger = logging.getLogger(__name__)


def read_triage_manifest(session: Session) -> TriageManifest | None:
    """Read the batch PR manifest recorded in the session's run manifest.

    The triage_manifest path is stored in ``session.run_dir / "manifest.json"``
    during launch via ``ctx.update_manifest({"triage_manifest": path})``.
    Reads only the session's typed ``run_dir`` — never sibling run dirs, so a
    stale previous run in the same worktree cannot leak into this completion.

    Fail-safe: a missing run manifest, key, or manifest file yields None
    (with a warning where content is present but unreadable).
    """
    run_manifest_path = session.run_dir / "manifest.json"
    if not run_manifest_path.exists():
        return None
    try:
        run_manifest = json.loads(run_manifest_path.read_text())
    except Exception as exc:
        logger.warning(
            "[triage] Failed to read run manifest %s: %s",
            run_manifest_path,
            exc,
            exc_info=True,
        )
        return None
    triage_manifest_path = run_manifest.get("triage_manifest")
    if not triage_manifest_path:
        return None
    manifest_path = Path(triage_manifest_path)
    if not manifest_path.exists():
        logger.warning(
            "[triage] Manifest path in run manifest doesn't exist: %s",
            manifest_path,
        )
        return None
    try:
        return TriageManifest.read(manifest_path)
    except Exception as exc:
        logger.warning(
            "[triage] Failed to read manifest from %s: %s",
            manifest_path,
            exc,
            exc_info=True,
        )
        return None


def read_triage_assignment_for_session(session: Session) -> TriageAssignment | None:
    """Read the launch-time triage assignment for a session's run.

    The assignment lives at the fixed
    ``session.run_dir / "triage-data" / TRIAGE_ASSIGNMENT_FILENAME`` and is
    read through the ADR-0031 policy owner. Malformed content is fail-safe:
    warn and report no assignment rather than acting on a guess.
    """
    try:
        return read_triage_assignment(session.run_dir)
    except ValueError as exc:
        logger.warning(
            "[triage] Malformed triage assignment in %s: %s", session.run_dir, exc
        )
        return None


def validate_decision_for_assignment(
    decision: "TriageDecision",
    assignment: TriageAssignment,
    *,
    config: "Config",
    labels: LabelManager,
) -> str | None:
    """Assignment/policy validation beyond the structural artifact contract.

    Returns a human-readable contract-violation detail, or None when valid.
    Enforced here (the completion owner) rather than by prompt convention:

    * Failure investigations must publish their diagnosis to the originating
      issue — >=1 ``post_comment`` targeting ``focus_issue_number`` (#6761 F2).
    * ``create_issue`` proposals may not carry protected workflow labels
      (#6761 F4). Checked at mapping time so the domain contract stays
      config-free.
    """
    if assignment.flavor is TriageSessionFlavor.FAILURE_INVESTIGATION:
        focus = assignment.focus_issue_number
        has_focus_comment = any(
            action.action_type == "post_comment" and action.target_number == focus
            for action in decision.proposed_actions
        )
        if not has_focus_comment:
            return (
                "failure investigation decision must propose at least one"
                f" post_comment targeting the originating issue #{focus}"
                " (the diagnosis has no channel otherwise)"
            )
    for action in decision.proposed_actions:
        if action.action_type != "create_issue":
            continue
        violations = protected_triage_label_violations(
            action.labels, config=config, labels=labels
        )
        if violations:
            return (
                f"proposed action {action.id} (create_issue) carries protected"
                f" workflow labels: {', '.join(violations)}; agent-proposed"
                " labels may not touch orchestrator label truth"
            )
    return None


def load_validated_triage_pair(
    run_dir: Path,
    assignment: TriageAssignment,
    *,
    config: "Config",
    labels: LabelManager,
) -> TriageArtifactLoadResult:
    """Load the artifact pair and apply assignment/policy validation.

    The ONE read both completion seams use: the processing path (authoritative
    outcome, finding 3) and the action planner (planning effects). Never raises.
    """
    result = load_triage_artifact_pair_for_run(run_dir)
    if not result.ok or result.decision is None:
        return result
    detail = validate_decision_for_assignment(
        result.decision, assignment, config=config, labels=labels
    )
    if detail is not None:
        logger.error("Triage decision contract violation in %s: %s", run_dir, detail)
        return TriageArtifactLoadResult(
            failure=TriageDecisionLoadFailure.CONTRACT_VIOLATION,
            detail=detail,
        )
    return result


def triage_decision_processing_error(config: "Config", run_dir: Path) -> str | None:
    """Authoritative pair validation for a COMPLETED triage session (#6761 F3).

    Called from the completion processing path BEFORE status recording. A
    missing/rejected pair returns an ``ERROR_PREFIX_TRIAGE_DECISION``-tagged
    processing error; ``critical_processing_errors`` classifies it critical so
    history records FAILED and the failure labeling path fires. Sessions
    without an assignment fail safe (None), matching the planner.
    """
    try:
        assignment = read_triage_assignment(run_dir)
    except ValueError as exc:
        logger.warning("[triage] Malformed triage assignment in %s: %s", run_dir, exc)
        return None
    if assignment is None:
        return None
    result = load_validated_triage_pair(
        run_dir, assignment, config=config, labels=LabelManager(config)
    )
    if result.ok:
        return None
    failure = result.failure.value if result.failure else "unknown"
    return f"{ERROR_PREFIX_TRIAGE_DECISION}: {failure}: {result.detail}"


def has_triage_decision_errors(processing_errors: list[str] | None) -> bool:
    """True when processing errors include a rejected triage decision pair."""
    return any(
        error.startswith(ERROR_PREFIX_TRIAGE_DECISION)
        for error in processing_errors or ()
    )


def _split_triage_decision_error(processing_errors: list[str]) -> tuple[str, str]:
    """Parse (failure, detail) back out of the recorded processing error."""
    for error in processing_errors:
        if not error.startswith(ERROR_PREFIX_TRIAGE_DECISION):
            continue
        remainder = error[len(ERROR_PREFIX_TRIAGE_DECISION):].lstrip(": ")
        failure, sep, detail = remainder.partition(": ")
        return (failure or "unknown", detail if sep else "")
    return ("unknown", "")


def _manifest_label_actions(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
    *,
    success: bool,
) -> list[Action]:
    """Label all manifest PRs triage-reviewed (success) or triage-failed."""
    triage_manifest = read_triage_manifest(session)
    if not triage_manifest or not triage_manifest.prs:
        return []
    if success:
        triage_label = config.triage_reviewed_label or "triage-reviewed"
        reason = "Triage completed successfully"
    else:
        triage_label = config.triage_failed_label or "triage-failed"
        reason = "Triage session failed"
    logger.info(
        "[triage] Adding '%s' label to %d PRs",
        triage_label,
        len(triage_manifest.prs),
    )
    return [
        AddLabelAction(
            issue_number=pr.number,
            label=triage_label,
            reason=reason,
            expected=expected,
        )
        for pr in triage_manifest.prs
    ]


def generate_triage_completion_actions(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
    *,
    completed_ok: bool,
    labels: LabelManager,
) -> list[Action]:
    """Plan all completion effects for a triage session (see module docstring)."""
    actions: list[Action] = []

    if not is_triage_session(config.triage_review_agent, session.issue.agent_type):
        return actions

    assignment = read_triage_assignment_for_session(session)
    if assignment is None:
        # Fail-safe for pre-upgrade or assignment-less sessions: skip
        # labels and decision processing so PRs stay watch-labeled and
        # re-enter the next batch, rather than reproducing the old
        # cross-variant mislabeling.
        logger.warning(
            "[triage] No triage assignment for session %s; "
            "skipping triage completion effects (PRs re-enter the next batch)",
            session.terminal_id,
        )
        return actions

    load_result = (
        load_validated_triage_pair(
            session.run_dir, assignment, config=config, labels=labels
        )
        if completed_ok
        else None
    )
    succeeded = load_result is not None and load_result.ok

    if assignment.flavor is TriageSessionFlavor.BATCH_REVIEW:
        actions.extend(
            _manifest_label_actions(config, session, expected, success=succeeded)
        )

    if load_result is None:
        return actions
    if load_result.decision is not None:
        actions.extend(
            plan_triage_decision_actions(
                load_result.decision,
                config,
                labels,
                anchor_issue=session.issue,
                expected=expected,
            )
        )
    else:
        # Belt-and-braces: the processing path (finding 3) should already have
        # classified this session FAILED before the planner sees it; still
        # surface the rejection when a rejected pair reaches this seam.
        failure = load_result.failure.value if load_result.failure else "unknown"
        logger.warning(
            "[triage] Decision artifact rejected for issue #%d (%s): %s",
            session.issue.number,
            failure,
            load_result.detail,
        )
        actions.append(
            plan_triage_rejection_action(
                anchor_issue_number=session.issue.number,
                failure=failure,
                detail=load_result.detail,
            )
        )

    if succeeded and assignment.flavor is TriageSessionFlavor.BATCH_REVIEW:
        # Terminal transition (#6768 round 4): the open+agent-labeled tracking
        # issue is what startup recovery requeues and what
        # _find_existing_triage_issue treats as the active batch. Ordered last
        # so a mid-apply crash leaves the batch open and re-auditable. No
        # comment: triage prompts promise the orchestrator posts none here.
        actions.append(
            CloseIssueAction(
                issue_number=session.issue.number,
                reason="Batch triage review completed - closing tracking issue",
                expected=expected,
            )
        )
    return actions


def generate_triage_decision_failure_actions(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
    *,
    processing_errors: list[str],
    labels: LabelManager,
) -> list[Action]:
    """Completion effects when a COMPLETED triage session's pair was rejected.

    The completion processing path recorded an ``ERROR_PREFIX_TRIAGE_DECISION``
    error (finding 3); history is FAILED via the critical-error seam. This
    plans the label/comment effects for every flavor:

    * batch review — manifest PRs get the triage-failed label;
    * both flavors — the rejection is surfaced as an event AND durably on the
      session's own issue (blocked-failed label + explanatory comment, the
      operator-facing escalation surface — #6761 finding 6), and the
      in-progress claim is released. The batch tracking issue stays open for
      re-audit.
    """
    failure, detail = _split_triage_decision_error(processing_errors)
    actions: list[Action] = []
    assignment = read_triage_assignment_for_session(session)
    if assignment is not None and assignment.flavor is TriageSessionFlavor.BATCH_REVIEW:
        actions.extend(
            _manifest_label_actions(config, session, expected, success=False)
        )
    actions.append(
        plan_triage_rejection_action(
            anchor_issue_number=session.issue.number,
            failure=failure,
            detail=detail,
        )
    )
    detail_text = detail or "no detail recorded"
    actions.extend(
        (
            AddLabelAction(
                issue_number=session.issue.number,
                label=labels.blocked_failed,
                reason=f"Triage decision artifact rejected ({failure})",
                expected=expected,
            ),
            AddCommentAction(
                number=session.issue.number,
                comment=(
                    "## ❌ Triage decision rejected\n\n"
                    "The triage session completed, but its decision artifact"
                    " pair (`triage-decision.json` + `triage-report.md`) was"
                    f" missing or invalid (`{failure}`):\n\n"
                    f"> {detail_text}\n\n"
                    f"- Session: `{session.terminal_id}`\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n\n"
                    f"The session is recorded as failed and `{labels.blocked_failed}`"
                    " was added. Remove the label to allow reprocessing."
                ),
                reason="Durable operator record of the rejected triage decision",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=session.issue.number,
                label=labels.in_progress,
                reason="Triage decision rejected - releasing claim",
                expected=expected,
            ),
        )
    )
    return actions


def generate_triage_failure_actions(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
) -> list[Action]:
    """Batch FAILED/TIMED_OUT terminal effects (#6768 round 5).

    Manifest PRs get the operator-visible triage-failed label and the
    tracking issue closes after the generic failure diagnosis and the PR
    labels: an open failed tracker would be requeued at restart with an
    empty manifest (its PRs are now candidate-filtered as triage-failed),
    looping forever. Failure investigations and assignment-less sessions
    produce nothing here - their anchor is the original failed work issue.
    """
    if not is_triage_session(config.triage_review_agent, session.issue.agent_type):
        return []
    assignment = read_triage_assignment_for_session(session)
    if assignment is None:
        logger.warning(
            "[triage] No triage assignment for session %s; "
            "skipping terminal triage effects",
            session.terminal_id,
        )
        return []
    if assignment.flavor is not TriageSessionFlavor.BATCH_REVIEW:
        return []
    actions = _manifest_label_actions(config, session, expected, success=False)
    actions.append(
        CloseIssueAction(
            issue_number=session.issue.number,
            reason="Batch triage review failed - closing tracking issue "
            "(manifest PRs carry triage-failed)",
            expected=expected,
        )
    )
    return actions
