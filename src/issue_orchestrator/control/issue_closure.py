"""Own ordinary closure and evidence-fold explanation ordering."""

from collections.abc import Callable
import hashlib
import logging

from .actions import ActionResult, CloseIssueAction, FoldCaseFileIssueAction
from .claim_gate import ClaimLostError

logger = logging.getLogger(__name__)


def apply_issue_closure(
    action: CloseIssueAction, *,
    comment_marker_present: Callable[[int, str], bool],
    post_comment: Callable[[int, str], str],
    set_issue_state: Callable[[int, str], None],
    before_write: Callable[[], None],
) -> ActionResult:
    """An evidence fold owes a recorded explanation before it may close.

    An authoritative paginated marker lookup recovers a successful comment whose response was lost,
    and retries after a failed close. Ordinary close actions retain their
    existing best-effort post-close comment contract.
    """
    try:
        required = isinstance(action, FoldCaseFileIssueAction)
        if required:
            digest = hashlib.sha256(action.comment.encode("utf-8")).hexdigest()
            marker = f"<!-- tech-lead-case-file-fold:{digest} -->"
            if not comment_marker_present(action.issue_number, marker):
                before_write()
                post_comment(action.issue_number, f"{action.comment}\n\n{marker}")
        before_write()
        set_issue_state(action.issue_number, "closed")
        if action.comment and not required:
            try:
                before_write()
                post_comment(action.issue_number, action.comment)
            except ClaimLostError:
                raise
            except Exception:
                logger.warning("Failed to post close comment for #%s", action.issue_number, exc_info=True)
        return ActionResult.ok(action, issue_number=action.issue_number, state="closed")
    except ClaimLostError:
        raise
    except Exception as error:
        logger.error("Failed to close issue #%s: %s", action.issue_number, error)
        return ActionResult.fail(action, str(error), issue_number=action.issue_number)
