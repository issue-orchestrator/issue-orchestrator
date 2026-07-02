"""Submission-scoped identity for a republish background job.

A republish job id encodes the issue plus a per-submission ``token`` so two
retries for the same issue (e.g. an abandoned one and a fresh one) get distinct
ids the runner tracks independently, and :meth:`PublishRecoveryService.
drain_completed_retries` can correlate each completion back to its exact
submission before touching owner state.
"""

from __future__ import annotations

from dataclasses import dataclass

_PREFIX = "republish:"


@dataclass(frozen=True)
class RepublishJobId:
    """Identity of an in-flight republish submission."""

    issue_number: int
    token: int

    def encode(self) -> str:
        return f"{_PREFIX}{self.issue_number}:{self.token}"

    @classmethod
    def parse(cls, job_id: str) -> "RepublishJobId | None":
        """Decode a job id, or ``None`` for a non-republish / malformed id."""
        if not job_id.startswith(_PREFIX):
            return None
        issue_str, _, token_str = job_id[len(_PREFIX):].partition(":")
        try:
            return cls(issue_number=int(issue_str), token=int(token_str))
        except ValueError:
            return None
