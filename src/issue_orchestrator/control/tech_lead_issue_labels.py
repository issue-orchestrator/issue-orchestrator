"""Shared gate and label provisioning for durable tech-lead issue creation."""

from __future__ import annotations
from typing import Callable
from ..ports import RepositoryHost
from ..domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL
from .actions import (
    CreateTechLeadProposalIssueAction,
    CreateTechLeadCaseFileIssueAction,
)
from .claim_gate import ClaimLostError
from .reconciliation import ReconciliationRequired
from .label_manager import tech_lead_issue_label_metadata


def required_label_provisioning_error(
    action: CreateTechLeadProposalIssueAction | CreateTechLeadCaseFileIssueAction,
    *,
    repository_host: "RepositoryHost",
    before_write: Callable[[], None],
) -> str | None:
    """Guarantee action labels before issue creation, or return the reason."""
    try:
        existing = {
            name.casefold()
            for entry in repository_host.list_labels()
            if isinstance(entry, dict) and isinstance((name := entry.get("name")), str)
        }
    except Exception as exc:
        if isinstance(action, CreateTechLeadProposalIssueAction):
            return (
                f"could not verify the {PROPOSED_TECH_LEAD_LABEL!r} gate label is"
                f" provisioned; refusing to create an ungated proposal: {exc}"
            )
        return (
            "could not verify required pattern case-file labels; refusing to"
            f" create an issue: {exc}"
        )

    if (
        isinstance(action, CreateTechLeadProposalIssueAction)
        and PROPOSED_TECH_LEAD_LABEL.casefold() not in existing
    ):
        return (
            f"the {PROPOSED_TECH_LEAD_LABEL!r} gate label is not provisioned in"
            " this repository; run `issue-orchestrator init` to create it."
            " Refusing to create an ungated tech_lead proposal (#6779 R3)"
        )

    for label in action.labels:
        folded = label.casefold()
        if folded in existing:
            continue
        color, description = tech_lead_issue_label_metadata(label)
        try:
            # RepositoryHost.create_label verifies the write. Doing this before
            # create_issue prevents GitHub from silently dropping an unknown
            # label and leaving an orphaned, schedulable issue.
            before_write()
            repository_host.create_label(
                label,
                color=color,
                description=description,
            )
        except (ReconciliationRequired, ClaimLostError):
            raise
        except Exception as exc:
            issue_kind = (
                "tech_lead proposal"
                if isinstance(action, CreateTechLeadProposalIssueAction)
                else "pattern case file"
            )
            return (
                f"could not provision required label {label!r} for {issue_kind};"
                f" refusing to create an issue: {exc}"
            )
        existing.add(folded)
    return None
