"""Read the proof that a validated head is already published on its open PR.

The rule itself is `validated_work_remote_authority.carried_by_open_pull_request`;
this module gathers its facts. Capture and the recovery drain's scope sweep
both ask through it, so the two cannot disagree on what "already published"
means, and both record it through the store's one lineage owner
(`record_open_pr_publication`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..domain.publication_remote import PublicationRemoteError
from ..domain.validated_work import ValidatedWorkState
from ..domain.validated_work_capture import ValidatedWorkRemoteFacts, ValidatedWorkRemoteRequest
from ..domain.validated_work_remote_authority import (
    PublishedOnOpenPullRequest, carried_by_open_pull_request,
)
from ..domain.validated_work_store import ValidatedWorkRecord
from ..ports.validated_work_capture_observer import ValidatedWorkCaptureObserver
from ..ports.validated_work_preservation import ValidatedWorkAdmissionStore
from ..ports.working_copy import WorkingCopy

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OpenPullRequestCarriage:
    """Fetch the PR branch head now and compare the validated head with it."""

    git: WorkingCopy

    def carried(
        self,
        facts: ValidatedWorkRemoteFacts,
        *,
        repo_slug: str,
        branch_name: str,
        validated_head_sha: str,
        repository: Path,
    ) -> PublishedOnOpenPullRequest | None:
        """The proof, or None. ``repository`` must hold the validated object.

        The branch is fetched fresh - a cached tracking ref of a branch since
        force-pushed would judge the wrong history - and only when the remote
        facts name an open PR at all, so a head with no PR costs no fetch.
        """
        if not facts.pull_requests or facts.branch_head_sha is None:
            return None
        fetched = self.git.fetch_remote_branch_head(repository, branch_name)
        relation = None if fetched is None else self.git.compare_commits(
            repository, left=validated_head_sha, right=fetched,
        )
        return carried_by_open_pull_request(
            facts, repo_slug=repo_slug, branch_name=branch_name,
            fetched_head_sha=fetched, relation=relation,
        )


@dataclass(frozen=True, slots=True)
class OpenPullRequestPublication:
    """The scope sweep's repair for a record captured before capture recorded publication."""

    observer: ValidatedWorkCaptureObserver
    carriage: OpenPullRequestCarriage
    store: ValidatedWorkAdmissionStore
    # The repository that holds every record's pinned validated commit.
    repository: Path
    now: Callable[[], str]

    def record(self, record: ValidatedWorkRecord) -> bool:
        """Record the open PR's publication of this head; whether that resolved it.

        A PUBLISHING record is skipped without a remote read: its in-flight
        publication owns the lineage (the store would refuse anyway). An
        unreadable remote, or an open PR that does not carry the head, proves
        nothing and changes nothing.
        """
        if record.disposition.state is ValidatedWorkState.PUBLISHING:
            return False
        key = record.disposition.key
        try:
            facts = self.observer.observe(
                ValidatedWorkRemoteRequest(key.repo_slug, key.issue_number, key.branch_name)
            )
        except PublicationRemoteError as error:
            logger.info(
                "[VALIDATED_WORK] Record %s: remote unreadable, publication unproven: %s",
                record.disposition.record_id, error,
            )
            return False
        published = self.carriage.carried(
            facts, repo_slug=key.repo_slug, branch_name=key.branch_name,
            validated_head_sha=key.validated_head_sha, repository=self.repository,
        )
        if published is None:
            return False
        status = self.store.record_open_pr_publication(key, published=published, observed_at=self.now())
        # The route that resolved it is the durable record's answer: another
        # owner may have resolved it meanwhile, and that is not this publication.
        resolved = any(
            d.published_by_open_pr for d in self.store.for_issue(key.issue_number).dispositions
            if d.record_id == record.disposition.record_id
        )
        logger.info(
            "[VALIDATED_WORK] Record %s of issue #%d: %s; lineage publication %s; resolved=%s",
            record.disposition.record_id, key.issue_number,
            published.describe(key.validated_head_sha), status.value, resolved,
        )
        return resolved
