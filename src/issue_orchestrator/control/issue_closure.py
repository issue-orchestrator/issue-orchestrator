"""Own ordinary closure and evidence-fold explanation ordering."""

from collections.abc import Callable, Mapping, Sequence
import logging

from .actions import ActionResult, CloseIssueAction, FoldCaseFileIssueAction

logger = logging.getLogger(__name__)


def apply_issue_closure(
    action: CloseIssueAction, *,
    read_comments: Callable[[int], Sequence[Mapping[str, object]]],
    post_comment: Callable[[int, str], str],
    set_issue_state: Callable[[int, str], None],
    before_write: Callable[[], None],
) -> ActionResult:
    """An evidence fold owes a recorded explanation before it may close.

    Exact-body recovery handles a successful comment whose response was lost,
    and retries after a failed close. Ordinary close actions retain their
    existing best-effort post-close comment contract.
    """
    try:
        required = isinstance(action, FoldCaseFileIssueAction)
        if required:
            comments = read_comments(action.issue_number)
            if not any(comment.get("body") == action.comment for comment in comments):
                before_write()
                post_comment(action.issue_number, action.comment)
        before_write()
        set_issue_state(action.issue_number, "closed")
        if action.comment and not required:
            try:
                before_write()
                post_comment(action.issue_number, action.comment)
            except Exception:
                logger.warning("Failed to post close comment for #%s", action.issue_number, exc_info=True)
        return ActionResult.ok(action, issue_number=action.issue_number, state="closed")
    except Exception as error:
        logger.error("Failed to close issue #%s: %s", action.issue_number, error)
        return ActionResult.fail(action, str(error), issue_number=action.issue_number)
