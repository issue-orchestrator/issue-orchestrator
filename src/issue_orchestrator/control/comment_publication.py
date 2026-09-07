"""Publish durable evidence once, recovering ambiguous remote write outcomes."""

from collections.abc import Callable

from .claim_gate import ClaimLostError
from .reconciliation import ReconciliationRequired
from ..ports.comment_receipt import IssueCommentReceipt


def ensure_comment_published(
    issue_number: int,
    body: str,
    *,
    find_receipt: Callable[[int, str], IssueCommentReceipt | None],
    post_comment: Callable[[int, str], str],
    before_write: Callable[[], None],
) -> IssueCommentReceipt:
    """Require fresh, exact-body, credential-owned evidence before advancing.

    The receipt lookup owns pagination and provenance. A transport or legacy
    write-verification error is ambiguous: consult that same authority before
    deciding whether publication failed. Never retry a write within this call.
    Mutation guards run outside recovery and always retain their authority.
    """
    receipt = find_receipt(issue_number, body)
    if receipt is not None:
        return receipt
    before_write()
    try:
        post_comment(issue_number, body)
    except (ClaimLostError, ReconciliationRequired):
        raise
    except Exception:
        receipt = find_receipt(issue_number, body)
        if receipt is None:
            raise
        return receipt
    receipt = find_receipt(issue_number, body)
    if receipt is None:
        raise RuntimeError("Comment publication could not be verified")
    return receipt
