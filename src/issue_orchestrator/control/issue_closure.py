"""Own ordinary closure and evidence-fold explanation ordering."""

from collections.abc import Callable
import hashlib
import logging

from .actions import ActionResult, CloseIssueAction, FoldCaseFileIssueAction
from .claim_gate import ClaimLostError
from .comment_publication import ensure_comment_published
from .reconciliation import ReconciliationRequired
from ..ports.comment_receipt import IssueCommentReceipt

logger = logging.getLogger(__name__)


def apply_issue_closure(
    action: CloseIssueAction, *,
    find_comment_receipt: Callable[[int, str], IssueCommentReceipt | None],
    post_comment: Callable[[int, str], str],
    set_issue_state: Callable[[int, str], None],
    before_write: Callable[[], None],
) -> ActionResult:
    """An evidence fold owes a recorded explanation before it may close.

    A fresh exact-body, credential-owned receipt recovers a successful comment whose response was lost,
    and retries after a failed close. Ordinary close actions retain their
    existing best-effort post-close comment contract.
    """
    try:
        required = isinstance(action, FoldCaseFileIssueAction)
        if required:
            digest = hashlib.sha256(action.comment.encode("utf-8")).hexdigest()
            marker = f"<!-- tech-lead-case-file-fold:{digest} -->"
            body = f"{action.comment}\n\n{marker}"
            ensure_comment_published(
                action.issue_number, body, find_receipt=find_comment_receipt,
                post_comment=post_comment, before_write=before_write,
            )
        before_write()
        set_issue_state(action.issue_number, "closed")
        if action.comment and not required:
            try:
                before_write()
                post_comment(action.issue_number, action.comment)
            except (ClaimLostError, ReconciliationRequired):
                raise
            except Exception:
                logger.warning("Failed to post close comment for #%s", action.issue_number, exc_info=True)
        return ActionResult.ok(action, issue_number=action.issue_number, state="closed")
    except (ClaimLostError, ReconciliationRequired):
        raise
    except Exception as error:
        logger.error("Failed to close issue #%s: %s", action.issue_number, error)
        return ActionResult.fail(action, str(error), issue_number=action.issue_number)
