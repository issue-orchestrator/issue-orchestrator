"""FreshIssueReader port for correctness-critical issue reads.

This port exists because some decisions may NOT be made against cached state:
the reconciliation gate that refuses to mutate a paused issue, the publish-retry
gate, and the session-outcome classification all need what GitHub says right
now. So the one thing this port must never do is make "I could not read the
issue" look like an observation.

The GitHub adapter used to swallow every read failure and return ``[]``, which
is indistinguishable from "this issue genuinely has no labels" — and an empty
label set SATISFIES the expectation the tech-lead mutation gate checks (it
forbids ``io:needs-reconcile`` and requires nothing). A timeout, a rate limit,
or an auth failure therefore let the control plane walk straight through an
explicit operator pause and file issues, comment cross-repo, settle promotions,
reset work, or kill sessions (#6957 round-2 review F4/A4). ADR-0006 requires the
opposite: fail closed when fresh observations cannot be obtained.

So the contract is unambiguous, and every consumer is expected to honor it:

* a successful read returns the observed labels, which MAY be empty;
* a failed read raises :class:`FreshIssueReadError` and never returns.
"""

from dataclasses import dataclass
from typing import Protocol


class FreshIssueReadError(RuntimeError):
    """A fresh issue read did not complete, so its result is UNKNOWN.

    Deliberately not a subclass of anything callers already swallow. Each
    consumer has to decide what "unknown" means for its own decision — pause the
    mutation, reject the retry, fall back to last-known labels — instead of
    inheriting a silent empty list that reads as fact.
    """


@dataclass(frozen=True)
class FreshIssueSnapshot:
    """What GitHub says about ONE issue right now: its labels AND its state.

    Some decisions need more than labels. A reviewed lifecycle plan that records
    a NONTERMINAL disposition is asserting the case file stays open, so it has
    to know whether a human closed it since the review — and a closed issue
    still has perfectly readable labels, so a labels-only read answers "fine"
    (#7248 round 6 review F8).

    The two facts travel together because they are one GitHub read. Asking for
    them separately would double this command's API volume for a guard that runs
    once per outcome, and would let the two answers come from different
    instants.
    """

    number: int
    labels: tuple[str, ...]
    state: str

    def __post_init__(self) -> None:
        if self.state not in ("open", "closed"):
            raise ValueError(
                f"issue #{self.number} state must be 'open' or 'closed',"
                f" got {self.state!r}"
            )


class FreshIssueSnapshotReader(Protocol):
    """Fresh labels AND state, under the same never-degrade contract.

    Deliberately a separate port from :class:`FreshIssueReader` rather than a
    method added to it: almost every consumer needs only labels, and widening
    the common port would make every implementation carry a fact it has no
    reason to know. A reader that can answer both implements both.
    """

    def read_issue_snapshot(self, issue_number: int) -> FreshIssueSnapshot:
        """This issue's labels and open/closed state right now, or raise.

        Raises:
            FreshIssueReadError: the read did not complete. As with
                :meth:`FreshIssueReader.read_issue_labels`, implementations must
                NOT degrade to a default — a guess about whether a case file is
                still open is exactly what this port exists to prevent.
        """
        ...


class FreshIssueReader(Protocol):
    """Protocol for fresh issue reads (no cache, no ETag)."""

    def read_issue_labels(self, issue_number: int) -> list[str]:
        """Labels on *issue_number* right now, bypassing every cache.

        Returns the observed labels, which may legitimately be empty.

        Raises:
            FreshIssueReadError: the read did not complete. Implementations must
                NOT degrade to an empty list — "unknown" is not an observation,
                and the callers of this port act on the difference.
        """
        ...
