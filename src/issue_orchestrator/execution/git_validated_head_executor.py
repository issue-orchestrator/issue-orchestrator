"""Exact remote execution without admission, review, or lifecycle policy."""

from ..domain.exact_git import ExactPushOutcome
from ..domain.publication_remote import (
    PublicationPullRequest,
    PublicationPrState,
    PublicationRemoteError,
    publication_marker,
)
from ..domain.validated_head_publication import (
    BranchWriteOutcome,
    BranchWriteStatus,
    PrEnsureOutcome,
    PrEnsureStatus,
    PublishValidatedHeadCommand,
    PublishValidatedHeadOutcome,
    RemoteHeadExpectation,
    compose_publication_outcome,
)
from ..domain.validated_work import ValidatedWorkFailure
from ..domain.validated_work_store import AncestryRelation
from ..ports.exact_git import ExactGit
from ..ports.git import GitError
from ..ports.publication_remote import PublicationRemote


class GitValidatedHeadExecutor:
    def __init__(
        self, git: ExactGit, remote: PublicationRemote, *, remote_name: str = "origin"
    ) -> None:
        self._git = git
        self._remote = remote
        self._remote_name = remote_name

    def push_validated_head(
        self, command: PublishValidatedHeadCommand
    ) -> BranchWriteOutcome:
        try:
            destination = self._git.resolve_push_destination(
                command.source_workspace, remote=self._remote_name
            )
            if self._remote.accepts_push_destination(command, destination) is not True:
                return BranchWriteOutcome(
                    BranchWriteStatus.REJECTED,
                    None,
                    None,
                    ValidatedWorkFailure.WORKSPACE_INTEGRITY,
                    "Push destination does not match repository authority",
                )
        except (ValueError, GitError, OSError, PublicationRemoteError) as exc:
            return BranchWriteOutcome(
                BranchWriteStatus.REJECTED,
                None,
                None,
                ValidatedWorkFailure.WORKSPACE_INTEGRITY,
                str(exc),
            )
        try:
            observed = self._remote.read_branch(command)
        except PublicationRemoteError as exc:
            return BranchWriteOutcome(
                BranchWriteStatus.TRANSIENT_FAILURE,
                None,
                None,
                ValidatedWorkFailure.REMOTE_UNREADABLE,
                str(exc),
            )
        if observed == command.target_head_sha:
            return BranchWriteOutcome(
                BranchWriteStatus.ALREADY_AT_TARGET,
                observed,
                None,
                None,
                "Remote branch is already at target",
            )
        try:
            refusal = self._branch_refusal(command, observed)
        except (GitError, OSError, TimeoutError) as exc:
            return BranchWriteOutcome(
                BranchWriteStatus.TRANSIENT_FAILURE,
                observed,
                None,
                ValidatedWorkFailure.WORKSPACE_INTEGRITY,
                str(exc),
            )
        if refusal is not None:
            return BranchWriteOutcome(
                BranchWriteStatus.DIVERGED
                if refusal
                in {
                    ValidatedWorkFailure.REMOTE_DIVERGED,
                    ValidatedWorkFailure.REMOTE_HEAD_CHANGED,
                }
                else BranchWriteStatus.REJECTED,
                observed,
                None,
                refusal,
                "Remote branch does not satisfy publication expectation",
            )
        try:
            result = self._git.push_exact(
                command.source_workspace,
                remote=self._remote_name,
                branch=command.branch_name,
                target_sha=command.target_head_sha,
                expected_sha=observed,
                destination=destination,
            )
        except (GitError, OSError, TimeoutError) as exc:
            return BranchWriteOutcome(
                BranchWriteStatus.TRANSIENT_FAILURE,
                observed,
                None,
                ValidatedWorkFailure.PUSH_FAILED,
                str(exc),
            )
        except ValueError as exc:
            return BranchWriteOutcome(
                BranchWriteStatus.REJECTED,
                observed,
                None,
                ValidatedWorkFailure.WORKSPACE_INTEGRITY,
                str(exc),
            )
        if result.outcome is ExactPushOutcome.PUSHED:
            return BranchWriteOutcome(
                BranchWriteStatus.PUSHED,
                command.target_head_sha,
                result.outcome,
                None,
                "Exact target pushed",
            )
        diverged = result.outcome in {
            ExactPushOutcome.LEASE_REJECTED,
            ExactPushOutcome.NOT_FAST_FORWARD,
        }
        return BranchWriteOutcome(
            BranchWriteStatus.DIVERGED
            if diverged
            else BranchWriteStatus.TRANSIENT_FAILURE,
            observed,
            result.outcome,
            ValidatedWorkFailure.REMOTE_DIVERGED
            if diverged
            else ValidatedWorkFailure.PUSH_FAILED,
            result.detail,
        )

    def _branch_refusal(
        self, command: PublishValidatedHeadCommand, observed: str | None
    ) -> ValidatedWorkFailure | None:
        if (
            command.expectation is RemoteHeadExpectation.EXACT
            and observed != command.expected_remote_head_sha
        ):
            return ValidatedWorkFailure.REMOTE_HEAD_CHANGED
        if command.expectation is RemoteHeadExpectation.ABSENT and observed is not None:
            return ValidatedWorkFailure.REMOTE_HEAD_CHANGED
        relation = self._git.compare_commits(
            command.source_workspace,
            left=observed or command.target_head_sha,
            right=command.target_head_sha,
        )
        if relation in {
            AncestryRelation.RIGHT_UNREACHABLE,
            AncestryRelation.BOTH_UNREACHABLE,
        }:
            return ValidatedWorkFailure.VALIDATION_SHA_MISMATCH
        if relation is AncestryRelation.LEFT_UNREACHABLE:
            return ValidatedWorkFailure.REMOTE_BASELINE_UNPROVEN
        if relation not in {AncestryRelation.ANCESTOR, AncestryRelation.EQUAL}:
            return ValidatedWorkFailure.REMOTE_DIVERGED
        return None

    def ensure_pull_request(
        self, command: PublishValidatedHeadCommand
    ) -> PrEnsureOutcome:
        try:
            if self._remote.read_branch(command) != command.target_head_sha:
                return self._pr_failure(
                    ValidatedWorkFailure.REMOTE_HEAD_CHANGED,
                    "Branch moved before PR ensure",
                )
            candidates = self._remote.list_prs(command)
            scoped = tuple(pr for pr in candidates if pr.branch == command.branch_name)
            if len(scoped) > 1:
                return self._pr_failure(
                    ValidatedWorkFailure.DUPLICATE_OPEN_PR,
                    "Multiple PRs for publication branch",
                )
            if command.pr_number is not None:
                recorded = self._remote.read_pr(command, command.pr_number)
                if recorded is None:
                    return self._pr_failure(
                        ValidatedWorkFailure.PR_BRANCH_MISMATCH,
                        "Recorded PR does not exist",
                    )
                if scoped and scoped[0].number != recorded.number:
                    return self._pr_failure(
                        ValidatedWorkFailure.DUPLICATE_OPEN_PR,
                        "Recorded PR conflicts with open candidate",
                    )
                return self._checked_pr(command, recorded, PrEnsureStatus.RECONCILED)
            if scoped:
                return self._adopt_candidate(command, scoped[0])
            return self._create_or_recover(command)
        except PublicationRemoteError as exc:
            return self._pr_failure(
                ValidatedWorkFailure.REMOTE_UNREADABLE, str(exc), transient=True
            )

    def _create_or_recover(
        self, command: PublishValidatedHeadCommand
    ) -> PrEnsureOutcome:
        try:
            created = self._remote.create_pr(command)
        except PublicationRemoteError:
            # A lost response is not proof of no effect. Adopt ONLY this operation's marker.
            candidates = tuple(
                pr
                for pr in self._remote.list_prs(command)
                if pr.branch == command.branch_name
            )
            if len(candidates) > 1:
                return self._pr_failure(
                    ValidatedWorkFailure.DUPLICATE_OPEN_PR, "Multiple PRs after create"
                )
            if len(candidates) != 1:
                raise
            return self._adopt_candidate(command, candidates[0])
        return self._confirm_created(command, created)

    def _adopt_candidate(
        self, command: PublishValidatedHeadCommand, pr: PublicationPullRequest
    ) -> PrEnsureOutcome:
        if (
            publication_marker(command.issue_number, command.branch_name)
            not in pr.body.splitlines()
        ):
            return self._pr_failure(
                ValidatedWorkFailure.PR_BRANCH_MISMATCH,
                "Unrecorded PR lacks this operation's exact marker",
                observed=pr,
            )
        return self._checked_pr(command, pr, PrEnsureStatus.ADOPTED)

    def _confirm_created(
        self, command: PublishValidatedHeadCommand, created: PublicationPullRequest
    ) -> PrEnsureOutcome:
        try:
            candidates = tuple(
                pr
                for pr in self._remote.list_prs(command)
                if pr.branch == command.branch_name
            )
            if len(candidates) > 1:
                return self._pr_failure(
                    ValidatedWorkFailure.DUPLICATE_OPEN_PR,
                    "Multiple PRs after create",
                    observed=created,
                )
            observed = self._remote.read_pr(command, created.number)
            if observed is None:
                return self._pr_failure(
                    ValidatedWorkFailure.REMOTE_UNREADABLE,
                    "Created PR cannot be observed",
                    transient=True,
                    observed=created,
                )
            return self._checked_pr(command, observed, PrEnsureStatus.CREATED)
        except PublicationRemoteError as exc:
            return self._pr_failure(
                ValidatedWorkFailure.REMOTE_UNREADABLE,
                str(exc),
                transient=True,
                observed=created,
            )

    def _checked_pr(
        self,
        command: PublishValidatedHeadCommand,
        pr: PublicationPullRequest,
        status: PrEnsureStatus,
    ) -> PrEnsureOutcome:
        if pr.state is not PublicationPrState.OPEN:
            failure = ValidatedWorkFailure.PR_CLOSED_OR_MERGED
        elif (pr.head_repo, pr.base_repo, pr.branch, pr.base_branch) != (
            command.repo_slug,
            command.repo_slug,
            command.branch_name,
            command.pr_base_branch,
        ):
            failure = ValidatedWorkFailure.PR_BRANCH_MISMATCH
        else:
            try:
                remote_head = self._remote.read_branch(command)
            except PublicationRemoteError as exc:
                return self._pr_failure(
                    ValidatedWorkFailure.REMOTE_UNREADABLE,
                    str(exc),
                    transient=True,
                    observed=pr,
                )
            if (
                pr.head_sha != command.target_head_sha
                or remote_head != command.target_head_sha
            ):
                return self._pr_failure(
                    ValidatedWorkFailure.PUBLISH_TARGET_MISMATCH,
                    "PR and remote must both identify target",
                    transient=True,
                    observed=pr,
                )
            return PrEnsureOutcome(
                status, pr.number, pr.url, pr.head_sha, None, "Exact target PR ensured"
            )
        return PrEnsureOutcome(
            PrEnsureStatus.REFUSED,
            pr.number,
            pr.url,
            pr.head_sha,
            failure,
            "PR is not usable",
        )

    @staticmethod
    def _pr_failure(
        failure: ValidatedWorkFailure,
        message: str,
        *,
        transient: bool = False,
        observed: PublicationPullRequest | None = None,
    ) -> PrEnsureOutcome:
        return PrEnsureOutcome(
            PrEnsureStatus.TRANSIENT_FAILURE if transient else PrEnsureStatus.REFUSED,
            observed.number if observed else None,
            observed.url if observed else None,
            observed.head_sha if observed else None,
            failure,
            message,
        )

    def publish_or_reconcile(
        self, command: PublishValidatedHeadCommand
    ) -> PublishValidatedHeadOutcome:
        branch = self.push_validated_head(command)
        pr = self.ensure_pull_request(command) if branch.at_target else None
        return compose_publication_outcome(branch, pr)
