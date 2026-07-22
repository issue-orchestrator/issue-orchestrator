"""Shared retry/unblock policy for UI-driven actions."""

from __future__ import annotations

from typing import Sequence

from .label_manager import LabelManager


def labels_to_remove_for_retry(labels: Sequence[str], lm: LabelManager) -> list[str]:
    """Return labels that must be removed before an issue can be retried.

    Retry should clear:
    - all blocking labels
    - tech_lead needs-human provenance paired with a cleared needs-human label
    - pr-pending (scheduler-gating lifecycle label)
    """
    labels_to_remove = set(lm.get_blocking(labels)) | (
        {lm.tech_lead_needs_human} & set(labels)
    )
    if lm.is_pr_pending(labels):
        labels_to_remove.add(lm.pr_pending)
    return sorted(labels_to_remove)
