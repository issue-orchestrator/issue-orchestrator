"""Read remote facts before a recovery attempt and after durable finalization."""

from ..domain.publication_remote import PublicationRemoteError
from ..domain.publication_verification import (
    PublicationVerification, baseline_matches, require_attributable_pr,
)
from ..domain.validated_head_publication import PublishValidatedHeadCommand
from ..domain.validated_work import DispositionPhase, ValidatedWorkFailure as Failure
from ..ports.publication_remote import PublicationRemote


class RemotePublicationVerifier:
    def __init__(self, remote: PublicationRemote) -> None:
        self._remote = remote

    def before_publication(self, command: PublishValidatedHeadCommand,
                           phase: DispositionPhase) -> PublicationVerification:
        observed = self._read(command)
        if not observed.verified:
            return observed
        if not baseline_matches(command, observed.branch_head, phase):
            return PublicationVerification(observed.branch_head, observed.pull_request,
                Failure.REMOTE_HEAD_CHANGED, "Remote no longer matches the admitted baseline")
        return observed

    def confirm_target(self, command: PublishValidatedHeadCommand) -> PublicationVerification:
        observed = self._read(command)
        if not observed.verified:
            return observed
        if observed.pull_request is None or observed.branch_head != command.target_head_sha:
            return PublicationVerification(observed.branch_head, observed.pull_request,
                Failure.PUBLISH_TARGET_MISMATCH, "Exact target requires an open attributable PR and branch")
        return observed

    def _read(self, command: PublishValidatedHeadCommand) -> PublicationVerification:
        try:
            before = self._remote.read_branch(command)
            candidates = tuple(pr for pr in self._remote.list_prs(command) if pr.branch == command.branch_name)
            if len(candidates) > 1:
                return PublicationVerification(before, None, Failure.DUPLICATE_OPEN_PR, "Multiple publication PRs")
            recorded = self._remote.read_pr(command, command.pr_number) if command.pr_number is not None else None
            if command.pr_number is not None and recorded is None:
                return PublicationVerification(before, None, Failure.PR_BRANCH_MISMATCH, "Recorded PR cannot be found")
            if recorded is not None and candidates and recorded.number != candidates[0].number:
                return PublicationVerification(before, recorded, Failure.DUPLICATE_OPEN_PR, "Recorded and current PR differ")
            pr = recorded if recorded is not None else next(iter(candidates), None)
            failure = require_attributable_pr(command, pr) if pr is not None else None
            if failure is not None:
                return PublicationVerification(before, pr, failure, "Publication PR identity refused")
            after = self._remote.read_branch(command)
            if before != after or (pr is not None and pr.head_sha != after):
                return PublicationVerification(after, pr, Failure.PUBLISH_TARGET_MISMATCH, "Remote observations disagree")
            return PublicationVerification(after, pr, None, "Fresh publication facts verified")
        except PublicationRemoteError as error:
            return PublicationVerification(None, None, Failure.REMOTE_UNREADABLE, str(error))
