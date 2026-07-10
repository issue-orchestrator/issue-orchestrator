"""Triage session completion planning (ADR-0031).

Single home for what happens when a triage session ends: launch-authority
verification, assignment-driven label policy, and decision-artifact
processing. Extracted from ``completion_action_planner`` so the triage owner
boundary (``triage_session_policy`` / ``TriageLaunchAuthority`` on the
launch side, this module on the completion side) lives in one cohesive seam.

Policy summary:

* The ONLY trusted launch scope is the orchestrator-owned
  :class:`TriageLaunchAuthority` recorded at launch (outside the
  agent-writable worktree). The worktree copies (triage-assignment.json,
  manifest.json) are the agent's reading material; a missing authority
  record, or worktree copies that no longer match it, is a critical failure
  (#6761 re-review finding 1) — never a fail-safe success.
* Only batch-review sessions label PRs (the authority manifest set they were
  launched to audit); failure investigations audit one issue and never touch
  manifest labels (#6768 B4).
* Every COMPLETED triage session (either flavor) must produce a valid
  decision artifact pair — a missing/invalid pair is a contract violation.
  The authoritative classification runs in the completion processing path
  (``triage_decision_processing_error``, called by ``completion_processor``
  BEFORE status recording) so the session's history outcome is FAILED, not
  a quiet success; the action planner re-reads the same validation for its
  planning effects (#6761 finding 3).
* Decision proposals may only target the session's immutable launch scope:
  a failure investigation targets its focus issue only; a batch review
  targets manifest PRs plus the anchor tracking issue
  (``create_issue``/``flag_pattern`` are scope-free) — out-of-scope targets
  are contract violations (#6761 re-review finding 2). A failure
  investigation must additionally publish its diagnosis: >=1
  ``post_comment`` targeting the focus issue (#6761 finding 2).
* ``create_issue`` proposals may not carry protected workflow labels
  (``triage_issue_policy`` owns the protected set, case-insensitively) —
  contract violation (#6761 finding 4).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.models import Session
from ..domain.triage_manifest import TriageManifest
from ..domain.triage_session import TriageLaunchAuthority, TriageSessionFlavor
from ..infra.triage_authority_store import TriageAuthorityStore
from .actions import (
    Action,
    AddCommentAction,
    AddLabelAction,
    CloseIssueAction,
    RemoveLabelAction,
)
from .completion_types import (
    ERROR_PREFIX_TRIAGE_AUTHORITY,
    ERROR_PREFIX_TRIAGE_DECISION,
)
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
from .triage_issue_policy import (
    protected_triage_label_violations,
    resolve_explicit_triage_milestone,
)
from .triage_session_policy import is_triage_session, read_triage_assignment

if TYPE_CHECKING:
    from ..domain.triage_artifacts import TriageDecision
    from ..infra.config import Config
    from ..ports import RepositoryHost
    from .reconciliation import ExpectedState

logger = logging.getLogger(__name__)

# Proposal types whose target_number must fall inside the session's launch
# scope. create_issue / flag_pattern carry no target and are scope-free.
_SCOPED_ACTION_TYPES = frozenset(
    ("post_comment", "escalate_to_human", "reset_retry", "kill_hung_session")
)


def read_triage_manifest(run_dir: Path) -> TriageManifest | None:
    """Read the agent-visible batch PR manifest copy for a session run.

    UNTRUSTED: this is the worktree copy, used only to detect divergence
    from the launch authority (tamper evidence). Completion effects never
    key off it. Fail-safe: a missing run manifest, key, or manifest file
    yields None (with a warning where content is present but unreadable).
    """
    run_manifest_path = run_dir / "manifest.json"
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


def resolve_triage_launch_authority(
    config: "Config",
    *,
    run_dir: Path,
    run_id: str,
    session_name: str,
) -> tuple[TriageLaunchAuthority | None, str | None]:
    """Load the orchestrator-owned launch authority and verify worktree copies.

    Returns ``(authority, error_detail)``. ``error_detail`` is None only when
    the authority record exists AND the agent-visible worktree copies still
    mirror it. A missing record, a deleted/malformed/flipped assignment copy,
    or a manifest whose PR set diverges from the recorded set is tamper
    evidence (#6761 re-review finding 1) — callers must fail the session.
    """
    authority = TriageAuthorityStore.for_repo(config.repo_root).load(
        run_id=run_id, session_name=session_name
    )
    if authority is None:
        return None, (
            "no orchestrator launch-authority record for run"
            f" {run_id}/{session_name}; the worktree triage inputs cannot"
            " be trusted"
        )
    try:
        assignment = read_triage_assignment(run_dir)
    except ValueError as exc:
        return authority, f"worktree triage-assignment.json is malformed: {exc}"
    if assignment is None:
        return authority, (
            "worktree triage-assignment.json is missing (deleted after launch)"
        )
    if not authority.matches_assignment(assignment):
        return authority, (
            "worktree triage-assignment.json"
            f" (flavor={assignment.flavor.value},"
            f" focus={assignment.focus_issue_number}) does not match the"
            f" launch authority (flavor={authority.flavor.value},"
            f" focus={authority.focus_issue_number})"
        )
    if authority.flavor is TriageSessionFlavor.BATCH_REVIEW:
        manifest = read_triage_manifest(run_dir)
        worktree_prs = frozenset(pr.number for pr in manifest.prs) if manifest else frozenset()
        if worktree_prs != frozenset(authority.manifest_pr_numbers):
            return authority, (
                f"worktree manifest PR set {sorted(worktree_prs)} does not"
                " match the launch authority set"
                f" {sorted(authority.manifest_pr_numbers)}"
            )
    return authority, None


def validate_decision_for_authority(
    decision: "TriageDecision",
    authority: TriageLaunchAuthority,
    *,
    config: "Config",
    labels: LabelManager,
) -> str | None:
    """Authority/policy validation beyond the structural artifact contract.

    Returns a human-readable contract-violation detail, or None when valid.
    Enforced here (the completion owner) against the immutable launch
    authority, never against the agent-writable worktree copies:

    * Every targeted proposal must stay inside the launch scope — a failure
      investigation may only address its focus issue; a batch review may
      only address manifest PRs and the anchor tracking issue (#6761
      re-review finding 2).
    * Failure investigations must publish their diagnosis to the originating
      issue — >=1 ``post_comment`` targeting the focus issue (#6761 F2).
    * ``create_issue`` proposals may not carry protected workflow labels
      (#6761 F4). Checked at mapping time so the domain contract stays
      config-free.
    """
    allowed = authority.allowed_targets()
    scope_text = (
        f"the originating issue #{authority.focus_issue_number}"
        if authority.flavor is TriageSessionFlavor.FAILURE_INVESTIGATION
        else "the audited manifest PRs and the tracking issue"
        f" ({', '.join(f'#{n}' for n in sorted(allowed))})"
    )
    for action in decision.proposed_actions:
        if action.action_type not in _SCOPED_ACTION_TYPES:
            continue
        if action.target_number not in allowed:
            return (
                f"proposed action {action.id} ({action.action_type}) targets"
                f" #{action.target_number}, outside this session's launch"
                f" scope: {scope_text}"
            )
    if authority.flavor is TriageSessionFlavor.FAILURE_INVESTIGATION:
        focus = authority.focus_issue_number
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
    authority: TriageLaunchAuthority,
    *,
    config: "Config",
    labels: LabelManager,
) -> TriageArtifactLoadResult:
    """Load the artifact pair and apply authority/policy validation.

    The ONE read both completion seams use: the processing path (authoritative
    outcome, finding 3) and the action planner (planning effects). Never raises.
    """
    result = load_triage_artifact_pair_for_run(run_dir)
    if not result.ok or result.decision is None:
        return result
    detail = validate_decision_for_authority(
        result.decision, authority, config=config, labels=labels
    )
    if detail is not None:
        logger.error("Triage decision contract violation in %s: %s", run_dir, detail)
        return TriageArtifactLoadResult(
            failure=TriageDecisionLoadFailure.CONTRACT_VIOLATION,
            detail=detail,
        )
    return result


def triage_decision_processing_error(
    config: "Config",
    *,
    run_dir: Path,
    run_id: str,
    session_name: str,
) -> str | None:
    """Authoritative scope + pair validation for a COMPLETED triage session.

    Called from the completion processing path BEFORE status recording. A
    missing/tampered launch authority (#6761 re-review F1) or a
    missing/rejected artifact pair (#6761 F3) returns a tagged processing
    error; ``critical_processing_errors`` classifies it critical so history
    records FAILED and the failure labeling path fires.
    """
    authority, tamper = resolve_triage_launch_authority(
        config, run_dir=run_dir, run_id=run_id, session_name=session_name
    )
    if authority is None:
        return f"{ERROR_PREFIX_TRIAGE_AUTHORITY}: missing_authority: {tamper}"
    if tamper is not None:
        return f"{ERROR_PREFIX_TRIAGE_AUTHORITY}: scope_tampered: {tamper}"
    result = load_validated_triage_pair(
        run_dir, authority, config=config, labels=LabelManager(config)
    )
    if result.ok:
        return None
    failure = result.failure.value if result.failure else "unknown"
    return f"{ERROR_PREFIX_TRIAGE_DECISION}: {failure}: {result.detail}"


_TRIAGE_ERROR_PREFIXES = (ERROR_PREFIX_TRIAGE_DECISION, ERROR_PREFIX_TRIAGE_AUTHORITY)


def has_triage_decision_errors(processing_errors: list[str] | None) -> bool:
    """True when processing errors include a rejected pair or tampered scope."""
    return any(
        error.startswith(_TRIAGE_ERROR_PREFIXES)
        for error in processing_errors or ()
    )


def _split_triage_decision_error(processing_errors: list[str]) -> tuple[str, str]:
    """Parse (failure, detail) back out of the recorded processing error."""
    for error in processing_errors:
        for prefix in _TRIAGE_ERROR_PREFIXES:
            if not error.startswith(prefix):
                continue
            remainder = error[len(prefix):].lstrip(": ")
            failure, sep, detail = remainder.partition(": ")
            return (failure or "unknown", detail if sep else "")
    return ("unknown", "")


def _resolve_launch_authority_for_session(
    config: "Config", session: Session
) -> tuple[TriageLaunchAuthority | None, str | None]:
    return resolve_triage_launch_authority(
        config,
        run_dir=session.run_dir,
        run_id=session.run_assets.run_id,
        session_name=session.run_assets.session_name,
    )


def _manifest_label_actions(
    config: "Config",
    authority: TriageLaunchAuthority,
    expected: "ExpectedState",
    *,
    success: bool,
) -> list[Action]:
    """Label the AUTHORITY manifest PRs triage-reviewed/-failed.

    The PR set comes exclusively from the launch authority record — a
    tampered worktree manifest with substituted PR numbers never receives
    labels (#6761 re-review finding 1).
    """
    if not authority.manifest_pr_numbers:
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
        len(authority.manifest_pr_numbers),
    )
    return [
        AddLabelAction(
            issue_number=pr_number,
            label=triage_label,
            reason=reason,
            expected=expected,
        )
        for pr_number in authority.manifest_pr_numbers
    ]


def generate_triage_completion_actions(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
    *,
    completed_ok: bool,
    labels: LabelManager,
    repository_host: "RepositoryHost | None" = None,
) -> list[Action]:
    """Plan all completion effects for a triage session (see module docstring).

    ``repository_host`` resolves ``triage.milestone_strategy.explicit`` at
    this boundary when a decision creates issues; the planner always passes
    it (None is tolerated only for configs without the explicit strategy —
    ``triage_issue_milestone`` fails loudly otherwise).
    """
    actions: list[Action] = []

    if not is_triage_session(config.triage_review_agent, session.issue.agent_type):
        return actions

    authority, tamper = _resolve_launch_authority_for_session(config, session)
    if authority is None or tamper is not None:
        # Belt-and-braces: the processing path classifies this critical
        # BEFORE status recording, so completions normally take the failure
        # routing instead. Never plan success effects from untrusted scope.
        failure = "missing_authority" if authority is None else "scope_tampered"
        detail = tamper or "no launch authority recorded"
        logger.error(
            "[triage] Launch authority rejected for issue #%d (%s): %s",
            session.issue.number,
            failure,
            detail,
        )
        actions.append(
            plan_triage_rejection_action(
                anchor_issue_number=session.issue.number,
                failure=failure,
                detail=detail,
            )
        )
        return actions

    load_result = (
        load_validated_triage_pair(
            session.run_dir, authority, config=config, labels=labels
        )
        if completed_ok
        else None
    )
    succeeded = load_result is not None and load_result.ok

    if authority.flavor is TriageSessionFlavor.BATCH_REVIEW:
        actions.extend(
            _manifest_label_actions(config, authority, expected, success=succeeded)
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
                explicit_milestone_number=_explicit_milestone_for_decision(
                    config, load_result.decision, repository_host
                ),
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

    if succeeded and authority.flavor is TriageSessionFlavor.BATCH_REVIEW:
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


def _explicit_milestone_for_decision(
    config: "Config",
    decision: "TriageDecision",
    repository_host: "RepositoryHost | None",
) -> int | None:
    """Resolve the explicit milestone strategy at this boundary when needed.

    One ``list_milestones`` call, made only when the strategy is explicit AND
    the decision actually creates issues (GitHub API discipline).
    """
    if not config.triage.milestone_strategy.explicit:
        return None
    if not any(
        action.action_type == "create_issue" for action in decision.proposed_actions
    ):
        return None
    if repository_host is None:
        raise ValueError(
            "triage.milestone_strategy.explicit requires a repository_host to"
            " resolve the milestone name at the completion boundary"
        )
    return resolve_explicit_triage_milestone(
        config, repository_host.list_milestones
    )


def generate_triage_decision_failure_actions(
    config: "Config",
    session: Session,
    expected: "ExpectedState",
    *,
    processing_errors: list[str],
    labels: LabelManager,
) -> list[Action]:
    """Completion effects when a COMPLETED triage session was rejected.

    The completion processing path recorded a triage authority/decision
    error (findings 1/3); history is FAILED via the critical-error seam.
    This plans the label/comment effects for every flavor:

    * batch review — the AUTHORITY manifest PRs get the triage-failed label;
    * both flavors — the rejection is surfaced as an event AND durably on the
      session's own issue (blocked-failed label + explanatory comment, the
      operator-facing escalation surface — #6761 finding 6), and the
      in-progress claim is released. The batch tracking issue stays open for
      re-audit.
    """
    failure, detail = _split_triage_decision_error(processing_errors)
    actions: list[Action] = []
    authority, _tamper = _resolve_launch_authority_for_session(config, session)
    if (
        authority is not None
        and authority.flavor is TriageSessionFlavor.BATCH_REVIEW
    ):
        actions.extend(
            _manifest_label_actions(config, authority, expected, success=False)
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
                reason=f"Triage completion rejected ({failure})",
                expected=expected,
            ),
            AddCommentAction(
                number=session.issue.number,
                comment=(
                    "## ❌ Triage completion rejected\n\n"
                    "The triage session completed, but its output was"
                    f" rejected (`{failure}`):\n\n"
                    f"> {detail_text}\n\n"
                    f"- Session: `{session.terminal_id}`\n"
                    f"- Runtime: {session.runtime_minutes:.1f} minutes\n\n"
                    f"The session is recorded as failed and `{labels.blocked_failed}`"
                    " was added. Remove the label to allow reprocessing."
                ),
                reason="Durable operator record of the rejected triage completion",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=session.issue.number,
                label=labels.in_progress,
                reason="Triage completion rejected - releasing claim",
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

    The AUTHORITY manifest PRs get the operator-visible triage-failed label
    and the tracking issue closes after the generic failure diagnosis and
    the PR labels: an open failed tracker would be requeued at restart with
    an empty manifest (its PRs are now candidate-filtered as triage-failed),
    looping forever. Failure investigations produce nothing here — their
    anchor is the original failed work issue. A session without a launch
    authority record produces nothing (the session already failed; closing
    or labeling from untrusted worktree copies would hand the agent
    authority).
    """
    if not is_triage_session(config.triage_review_agent, session.issue.agent_type):
        return []
    authority, _tamper = _resolve_launch_authority_for_session(config, session)
    if authority is None:
        logger.warning(
            "[triage] No launch authority for session %s; "
            "skipping terminal triage effects",
            session.terminal_id,
        )
        return []
    if authority.flavor is not TriageSessionFlavor.BATCH_REVIEW:
        return []
    actions = _manifest_label_actions(config, authority, expected, success=False)
    actions.append(
        CloseIssueAction(
            issue_number=session.issue.number,
            reason="Batch triage review failed - closing tracking issue "
            "(manifest PRs carry triage-failed)",
            expected=expected,
        )
    )
    return actions
