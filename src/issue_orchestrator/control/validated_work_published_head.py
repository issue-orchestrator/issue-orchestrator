"""Read the proof that a PR, not recovery, already published a validated head.

Three routes, one owner: the issue's OPEN PR carries the head
(`carried_by_open_pull_request`), or a MERGED PR of the branch landed it
(`landed_via_merged_pull_request`) - proven by the PR's head at merge, which a
squash merge leaves nowhere on the base - or, when the work was republished
on ANOTHER branch, an open or merged PR of the same issue carries it
(`carried_by_issue_pull_request`, #8137). Content is the proof, never the
record's branch name. This module gathers the facts.
Capture and the recovery drain's scope sweep both ask through it, so they
cannot disagree on what "already published" means, and both record it through
the store's one lineage owner (`record_pr_publication`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..domain.branch_naming import extract_issue_number_from_branch
from ..domain.publication_remote import PublicationPullRequest, PublicationRemoteError
from ..domain.validated_work import ValidatedWorkState
from ..domain.validated_work_capture import ValidatedWorkRemoteFacts, ValidatedWorkRemoteRequest
from ..domain.validated_work_remote_authority import (
    PullRequestPublication, carried_by_issue_pull_request, carried_by_open_pull_request,
    landed_via_merged_pull_request,
)
from ..domain.validated_work_store import ValidatedWorkRecord
from ..ports.validated_work_capture_observer import ValidatedWorkCaptureObserver
from ..ports.validated_work_preservation import ValidatedWorkAdmissionStore
from ..ports.working_copy import WorkingCopy

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PullRequestCarriage:
    """Read the remote and prove a PR published the validated head, or None."""

    git: WorkingCopy
    observer: ValidatedWorkCaptureObserver

    def proof(
        self,
        request: ValidatedWorkRemoteRequest,
        *,
        validated_head_sha: str,
        repository: Path,
        facts: ValidatedWorkRemoteFacts | None = None,
    ) -> PullRequestPublication | None:
        """The proof, or None. ``repository`` must hold the validated object.

        ``facts`` is the caller's own uncached branch/open-PR read, when it
        already made one; otherwise it is read here. The branch's open PR is
        asked first; only a head no open PR carries costs the merged-PR read,
        and only a head no PR of its own branch carries costs the read of the
        issue's PRs on other branches. Any unreadable remote proves nothing.
        """
        try:
            observed = self.observer.observe(request) if facts is None else facts
            opened = self._open(observed, request, validated_head_sha, repository)
            if opened is not None:
                return opened
            merged = self.observer.merged_pull_requests(request)
        except PublicationRemoteError as error:
            return self._unproven(request, error)
        landed = self._merged(merged, request, validated_head_sha, repository)
        if landed is not None:
            return landed
        try:
            elsewhere = self.observer.issue_pull_requests(request)
        except PublicationRemoteError as error:
            return self._unproven(request, error)
        return self._elsewhere(elsewhere, request, validated_head_sha, repository)

    @staticmethod
    def _unproven(request: ValidatedWorkRemoteRequest, error: PublicationRemoteError) -> None:
        logger.info(
            "[VALIDATED_WORK] Issue #%d branch %s: remote unreadable, publication unproven: %s",
            request.issue_number, request.branch_name, error,
        )
        return None

    def _open(
        self, facts: ValidatedWorkRemoteFacts, request: ValidatedWorkRemoteRequest,
        validated_head_sha: str, repository: Path,
    ) -> PullRequestPublication | None:
        # The branch is fetched fresh - a cached tracking ref of a branch since
        # force-pushed would judge the wrong history - and only when an open PR
        # is named at all.
        if not facts.pull_requests or facts.branch_head_sha is None:
            return None
        fetched = self.git.fetch_remote_branch_head(repository, request.branch_name)
        relation = None if fetched is None else self.git.compare_commits(
            repository, left=validated_head_sha, right=fetched,
        )
        return carried_by_open_pull_request(
            facts, repo_slug=request.repo_slug, branch_name=request.branch_name,
            fetched_head_sha=fetched, relation=relation,
        )

    def _merged(
        self, merged: tuple[PublicationPullRequest, ...], request: ValidatedWorkRemoteRequest,
        validated_head_sha: str, repository: Path,
    ) -> PullRequestPublication | None:
        # Newest first: a reused branch's latest landing is the one to name.
        for pr in sorted(merged, key=lambda candidate: candidate.number, reverse=True):
            if pr.head_repo != request.repo_slug or pr.branch != request.branch_name:
                continue  # never fetch a ref the proof would refuse anyway
            fetched = self.git.fetch_pull_request_head(repository, pr.number)
            relation = None if fetched is None else self.git.compare_commits(
                repository, left=validated_head_sha, right=fetched,
            )
            landed = landed_via_merged_pull_request(
                pr, repo_slug=request.repo_slug, branch_name=request.branch_name,
                fetched_head_sha=fetched, relation=relation,
            )
            if landed is not None:
                return landed
        return None

    def _elsewhere(
        self, pulls: tuple[PublicationPullRequest, ...], request: ValidatedWorkRemoteRequest,
        validated_head_sha: str, repository: Path,
    ) -> PullRequestPublication | None:
        # Newest first. The record's own branch was judged above under rules a
        # PR elsewhere must not bypass, so it is never fetched again here.
        for pr in sorted(pulls, key=lambda candidate: candidate.number, reverse=True):
            if (pr.branch == request.branch_name or pr.head_repo != request.repo_slug
                    or extract_issue_number_from_branch(pr.branch) != request.issue_number):
                continue  # never fetch a ref the proof would refuse anyway
            fetched = self.git.fetch_pull_request_head(repository, pr.number)
            relation = None if fetched is None else self.git.compare_commits(
                repository, left=validated_head_sha, right=fetched,
            )
            carried = carried_by_issue_pull_request(
                pr, repo_slug=request.repo_slug, issue_number=request.issue_number,
                branch_name=request.branch_name,
                fetched_head_sha=fetched, relation=relation,
            )
            if carried is not None:
                return carried
        return None


@dataclass(frozen=True, slots=True)
class PullRequestPublicationRecorder:
    """The scope sweep's repair for a record a PR published after it was captured."""

    carriage: PullRequestCarriage
    store: ValidatedWorkAdmissionStore
    # The repository that holds every record's pinned validated commit.
    repository: Path
    now: Callable[[], str]

    def record(self, record: ValidatedWorkRecord) -> bool:
        """Record a PR's publication of this head; whether that resolved it.

        A PUBLISHING record is skipped without a remote read: its in-flight
        publication owns the lineage (the store would refuse anyway).
        """
        if record.disposition.state is ValidatedWorkState.PUBLISHING:
            return False
        key = record.disposition.key
        # Stamped before the remote is read: the store orders open-PR proofs
        # by when they were observed, not by when they arrive (#8137 r3).
        observed_at = self.now()
        published = self.carriage.proof(
            ValidatedWorkRemoteRequest(key.repo_slug, key.issue_number, key.branch_name),
            validated_head_sha=key.validated_head_sha, repository=self.repository,
        )
        if published is None:
            return False
        status = self.store.record_pr_publication(key, published=published, observed_at=observed_at)
        # The route that resolved it is the durable record's answer: another
        # owner may have resolved it meanwhile, and that is not this publication.
        resolved = any(
            d.published_by_its_pr for d in self.store.for_issue(key.issue_number).dispositions
            if d.record_id == record.disposition.record_id
        )
        logger.info(
            "[VALIDATED_WORK] Record %s of issue #%d: %s; lineage publication %s; resolved=%s",
            record.disposition.record_id, key.issue_number,
            published.describe(key.validated_head_sha), status.value, resolved,
        )
        return resolved
