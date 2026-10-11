"""Acceptance policy evaluated before a reviewer approval becomes terminal."""

from __future__ import annotations

from typing import Protocol


class ReviewExchangeApprovalGate(Protocol):
    """Return the rejection reason when current artifacts cannot be approved.

    The review-exchange runner calls this only after the reviewer returns
    ``ok``.  A non-empty result converts that approval into another bounded
    coder rework round; ``None`` accepts the approval. ``upheld_rulings`` is
    the approval's attestation of the issue's standing rulings (#8141): the
    ids in the reviewer's ``decision.upheld_rulings``.
    """

    def rejection_reason(self, *, upheld_rulings: tuple[str, ...]) -> str | None:
        """Judge one ``ok`` verdict."""
        ...

    def cached_approval_reason(self, *, upheld_rulings: tuple[str, ...]) -> str | None:
        """Why an approval cached from an earlier exchange of the same head may not
        be reused now (a standing ruling recorded since, #8141), or None.
        ``upheld_rulings`` is what that approval's own decision attested."""
        ...
