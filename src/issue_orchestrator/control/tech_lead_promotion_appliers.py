"""The finding-promotion lane's mutating boundaries (#6957).

Split from ``tech_lead_finding_promotion`` along the line the lane already
draws: that module is PURE policy over the durable ledgers (eligibility,
routing, composition, planning — no IO at all), while everything here writes.
Three boundaries, each with an ordering that a crash must survive:

* :func:`apply_promote_tech_lead_finding` — file the issue in its routed repo,
  then record the ledger row create-once.
* :func:`apply_report_promoted_finding_evidence` — comment later evidence onto
  the one promoted issue, then advance its high-water mark.
* :func:`apply_settle_tech_lead_promotion` — close the loop in the SOURCE repo
  (shipped-fix memory, case-file comment/close, terminal ledger state).

Re-exported from ``tech_lead_finding_promotion`` so callers keep one import
site.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from ..domain.tech_lead_findings import (
    CASE_FILE_DECLINED,
    CASE_FILE_SHIPPED,
    PROMOTION_STATE_DECLINED,
    PROMOTION_STATE_SHIPPED,
    CaseFileLifecycleTransition,
)
from .actions import (
    Action,
    ActionResult,
    PromoteTechLeadFindingAction,
    ReportPromotedFindingEvidenceAction,
    SettleTechLeadPromotionAction,
)
from .tech_lead_promotion_filing import PromotionFilingOwner
from .tech_lead_case_file_lifecycle import PatternCaseFileLifecycleOwner

if TYPE_CHECKING:
    from ..ports import RepositoryHost
    from ..ports.pattern_registry import PatternCaseFileRegistry
    from ..ports.promotion_target import PromotionTargetHost
    from ..ports.tech_lead_authority import TechLeadAuthorityStore

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_promote_tech_lead_finding(
    action: Action,
    *,
    target: "PromotionTargetHost | None",
    authority: "TechLeadAuthorityStore | None",
    now_iso: str | None = None,
) -> ActionResult:
    """File a promoted finding issue and record its ledger row create-once.

    The whole filing transaction belongs to :class:`PromotionFilingOwner`:
    durable intent, then the remote create (or recovery of an interrupted one),
    then the ledger row. A filing that raises leaves no promotion row, so the
    next tick retries cleanly; a recorded row makes the signature permanently
    ineligible, so it must only exist for an issue that actually exists — and
    its evidence watermark must be the one the FILED BODY documents, never the
    retrying action's live count (#6957 round-3 review F11).
    """
    assert isinstance(action, PromoteTechLeadFindingAction)
    if target is None or authority is None:
        return ActionResult.fail(
            action,
            "tech_lead finding promotion requires a PromotionTargetHost and the"
            " TechLeadAuthorityStore wired into this applier",
        )
    filing = PromotionFilingOwner(authority=authority, target=target)
    existing = authority.load_promotion(signature=action.signature)
    if existing is not None:
        # Belt-and-braces against a stale plan: the ledger is the authority on
        # at-most-once, not the planning snapshot it was derived from. An intent
        # left behind by a crash after the row landed is inert; retire it.
        filing.forget_pending(action.signature)
        return ActionResult.ok(
            action,
            issue_number=existing.target_issue_number,
            deduplicated=True,
        )
    try:
        filed = filing.file(action, recorded_at=now_iso or _utc_now_iso())
    except Exception as exc:
        logger.exception(
            "Failed to file promoted tech_lead finding %r in %s",
            action.signature,
            action.target_repo,
        )
        return ActionResult.fail(action, str(exc))
    logger.info(
        "[tech_lead] Promoted finding %r -> %s#%d (case file #%d)%s",
        action.signature,
        filed.target_repo,
        filed.issue_number,
        action.case_file_issue_number,
        " [recovered an interrupted filing]" if filed.recovered else "",
    )
    return ActionResult.ok(
        action,
        issue_number=filed.issue_number,
        # Where it ACTUALLY landed: a filing interrupted before its ledger write
        # keeps the route it was filed against, even if the route has since
        # moved (the owner reconciles that, #6957 round-4 review F12).
        target_repo=filed.target_repo,
        url=filed.url,
        recovered=filed.recovered,
    )


def apply_report_promoted_finding_evidence(
    action: Action,
    *,
    target: "PromotionTargetHost | None",
    authority: "TechLeadAuthorityStore | None",
) -> ActionResult:
    """Comment later evidence on the one promoted issue, then mark it reported.

    The write order fails toward a duplicate comment after a crash, never toward
    silently losing evidence: only a successful target comment advances the
    durable high-water mark. The apply-time ledger read also makes a stale plan
    harmless after another tick already reported or settled the promotion.
    """
    assert isinstance(action, ReportPromotedFindingEvidenceAction)
    if target is None or authority is None:
        return ActionResult.fail(
            action,
            "reporting promoted finding evidence requires a PromotionTargetHost"
            " and the TechLeadAuthorityStore wired into this applier",
        )
    promotion = authority.load_promotion(signature=action.signature)
    if promotion is None:
        return ActionResult.fail(
            action,
            f"no promotion is recorded for signature {action.signature!r}",
        )
    if (
        promotion.target_repo != action.target_repo
        or promotion.target_issue_number != action.target_issue_number
    ):
        return ActionResult.fail(
            action,
            f"promotion target changed for signature {action.signature!r}:"
            f" ledger has {promotion.target_repo}#{promotion.target_issue_number},"
            f" action has {action.target_repo}#{action.target_issue_number}",
        )
    if not promotion.is_open or (
        promotion.reported_observations >= action.observation_count
    ):
        return ActionResult.ok(
            action,
            issue_number=promotion.target_issue_number,
            deduplicated=True,
        )
    try:
        target.add_comment(
            repo=action.target_repo,
            issue_number=action.target_issue_number,
            body=action.comment,
        )
        authority.note_promotion_reported(
            signature=action.signature,
            observations=action.observation_count,
        )
    except Exception as exc:
        logger.exception(
            "Failed to report later evidence for promoted finding %r",
            action.signature,
        )
        return ActionResult.fail(action, str(exc))
    return ActionResult.ok(
        action,
        issue_number=action.target_issue_number,
        observation_count=action.observation_count,
    )


def _promotion_lifecycle_transition(
    action: SettleTechLeadPromotionAction, *, recorded_at: str
) -> CaseFileLifecycleTransition:
    target = f"{action.target_repo}#{action.target_issue_number}"
    if action.shipped:
        reason = (
            f"Promoted issue {target} closed with a merged pull request; the"
            " case-file history is retained after closure."
        )
        evidence = (target, action.merged_pr_url)
        disposition = CASE_FILE_SHIPPED
    else:
        reason = (
            f"Promoted issue {target} was deliberately closed without a merged"
            " fix; the operator decline is terminal and prevents refiling."
        )
        evidence = (target,)
        disposition = CASE_FILE_DECLINED
    return CaseFileLifecycleTransition(
        transition_id=(
            f"promotion:{action.signature}:{action.target_repo}:"
            f"{action.target_issue_number}:{disposition}"
        ),
        disposition=disposition,
        reason=reason,
        evidence=tuple(item for item in evidence if item),
        recorded_at=recorded_at,
    )


def apply_settle_tech_lead_promotion(
    action: Action,
    *,
    repository_host: "RepositoryHost | None",
    authority: "TechLeadAuthorityStore | None",
    pattern_registry: "PatternCaseFileRegistry | None" = None,
    before_write: Callable[[], None] = lambda: None,
    now_iso: str | None = None,
) -> ActionResult:
    """Close the loop for a terminal promotion (the ONE settlement boundary).

    All writes here are IN the source repo (case-file comment/close, shipped-fix
    memory, ledger state) — only the READ that produced this fact crossed repos.

    Both terminal outcomes retire the case file through the lifecycle owner: a
    merged fix as ``shipped``, an operator decline as ``declined`` (#7240's named
    retirement rules). The decline half used to comment and leave the issue open;
    see :class:`~.actions.SettleTechLeadPromotionAction` for why closing loses no
    evidence.

    Ordering makes a crash mid-settlement self-healing: the durable ledger state
    is written LAST, so an interrupted settlement is re-planned next tick and the
    comment/close/record steps are individually idempotent (``record_shipped_fix``
    is create-once, closing a closed issue is a no-op, and a duplicate comment is
    cosmetic — far cheaper than losing the ``tech_lead_shipped_fixes`` row this
    whole lane exists to produce).
    """
    assert isinstance(action, SettleTechLeadPromotionAction)
    if repository_host is None or authority is None or pattern_registry is None:
        return ActionResult.fail(
            action,
            "tech_lead promotion settlement requires repository_host and the"
            " TechLeadAuthorityStore and PatternCaseFileRegistry wired into this"
            " applier",
        )
    try:
        transition = _promotion_lifecycle_transition(
            action, recorded_at=now_iso or _utc_now_iso()
        )
        PatternCaseFileLifecycleOwner(
            registry=pattern_registry,
            repository_host=repository_host,
            before_write=before_write,
        ).retire(
            signature=action.signature,
            transition=transition,
            # The SAME issue every ``before_write`` gate authorizes
            # (``reconciliation_subject()``). Passing it through makes shared
            # authority reject a registry that names a different canonical case
            # file, so the gate and the mutation can never address two issues
            # (#7247 review F2).
            issue_number=action.case_file_issue_number,
        )
        if action.shipped:
            before_write()
            authority.record_shipped_fix(
                issue_number=action.case_file_issue_number,
                title=action.title or action.signature,
                pr_url=action.merged_pr_url,
                area=action.area,
            )
        before_write()
        authority.settle_promotion(
            signature=action.signature,
            state=PROMOTION_STATE_SHIPPED
            if action.shipped
            else PROMOTION_STATE_DECLINED,
            shipped_pr_url=action.merged_pr_url,
        )
    except Exception as exc:
        logger.exception(
            "Failed to settle promoted tech_lead finding %r", action.signature
        )
        return ActionResult.fail(action, str(exc))
    return ActionResult.ok(
        action,
        issue_number=action.case_file_issue_number,
        shipped=action.shipped,
    )
