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
    mark_attempt: Callable[[], None] = lambda: None,
) -> IssueCommentReceipt:
    """Require fresh, exact-body, credential-owned evidence before advancing.

    The receipt lookup owns pagination and provenance. A transport or legacy
    write-verification error is ambiguous: consult that same authority before
    deciding whether publication failed. Never retry a write within this call.
    Mutation guards run outside recovery and always retain their authority.

    ``mark_attempt`` records durably that a remote write is ABOUT TO BE MADE, and
    it is called here — after the pre-check found no receipt and after the
    mutation guard allowed the write — rather than by the caller beforehand. The
    distinction is not cosmetic. A caller that marked the attempt first turned
    every refusal that happens in between into a permanent deadlock: the guard
    declines (a pause label appeared, the fresh read failed), nothing was ever
    posted, yet the durable state says "publication in flight", and every later
    attempt demands a receipt that cannot exist (#7248 review F6). Marked here,
    an authorized-but-unattempted publication stays resumable and an attempted
    one stays ambiguous, which is exactly the difference recovery needs.
    """
    receipt = find_receipt(issue_number, body)
    if receipt is not None:
        return receipt
    before_write()
    mark_attempt()
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
