"""Read back what the durable run ledger recorded for one exact run."""

from typing import Protocol

from ..domain.issue_run_evidence import IssueRunRecord
from ..domain.session_run import SessionRunAssets


class RecordedRunReader(Protocol):
    def recorded_run(self, run: SessionRunAssets) -> IssueRunRecord:
        """The ledger row for exactly these run assets.

        Raises ``IssueRunEvidenceUnavailable`` when the run was never
        registered or its row is unreadable; never returns a partial record.
        """
        ...
