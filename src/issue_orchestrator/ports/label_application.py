"""Port: when a label standing on an issue now was put on (#8688, #8731).

The evidence a block's EPISODE is bound to. Kept apart from the approval
evidence (:mod:`.approval_evidence`): an approval is declined by closing its
proposal, so a close ends that run, while a block's label kept on through a
close and reopen is still the same application.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from ..domain.tech_lead_approval import LabelEvent


class LabelApplicationReader(Protocol):
    """Read-only: the ``labeled`` event behind a label that stands now."""

    def label_application(self, issue_number: int, label: str) -> LabelEvent | None:
        """The event that applied ``label`` (case-insensitive), the first of
        its standing run: every ``labeled`` event since it was last removed.

        ``None`` means the complete event history shows the label not standing.
        A read that cannot prove completeness raises instead.
        """
        ...

    def label_applications(
        self, issue_number: int, labels: Sequence[str]
    ) -> Mapping[str, LabelEvent | None]:
        """:meth:`label_application` of every one of ``labels`` from ONE read
        of the issue's events, keyed by casefolded label name. Raises as it
        does: an incomplete read answers for no label."""
        ...
