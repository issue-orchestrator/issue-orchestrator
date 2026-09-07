"""Reset freshness policy over observed facts, shared by execution paths."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResetRetryFacts:
    issue_number: int
    issue_state: str
    active_runtime: bool
    blocking: bool

    def stale_reason(self) -> str | None:
        if self.issue_state != "open":
            return f"target issue #{self.issue_number} is {self.issue_state}, not open"
        if self.active_runtime:
            return (f"issue #{self.issue_number} has active runtime (session, review-exchange"
                " pair/job, publish retry, or unresolved validated work); resetting would terminate live work"
                " the proposal did not observe")
        if not self.blocking:
            return (f"issue #{self.issue_number} no longer carries a blocking-class"
                " label; the diagnosed failure appears already recovered")
        return None
