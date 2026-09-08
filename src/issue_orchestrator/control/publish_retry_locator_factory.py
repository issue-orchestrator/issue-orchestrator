"""Manual retry custody is constructed only from the completed run's inputs."""

from ..domain.completion_intake import CompletionIntakeReceipt
from ..domain.models import Session
from ..domain.publish_retry import PublishRetryLocators


class PublishRetryLocatorFactory:
    """Preserve provenance without deriving validation or publication authority."""

    def for_failed_session(
        self, session: Session, *, intake_receipt: CompletionIntakeReceipt | None,
        review_exchange_completed: bool, review_exchange_halted: bool,
    ) -> PublishRetryLocators:
        return PublishRetryLocators(
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            session_key=session.key.stable_id(),
            worktree_path=str(session.worktree_path),
            branch_name=session.branch_name,
            completion_path=session.completion_path,
            run_assets=session.run_assets,
            intake_receipt=intake_receipt,
            agent_label=session.agent_label,
            pr_number=session.pr_number,
            skip_review=session.agent_config.skip_review,
            review_exchange_completed=review_exchange_completed,
            review_exchange_halted=review_exchange_halted,
        )
