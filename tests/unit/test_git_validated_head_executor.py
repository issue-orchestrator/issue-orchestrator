"""Exact executor behavior against real local Git and a recording PR boundary."""

from dataclasses import replace

import pytest

from issue_orchestrator.domain.publication_remote import (
    PublicationPullRequest,
    PublicationPrState,
    PublicationRemoteError,
    publication_marker,
)
from issue_orchestrator.domain.validated_head_publication import (
    PublishValidatedHeadCommand,
    PublicationContent,
    RemoteHeadExpectation,
    compose_publication_outcome,
)
from issue_orchestrator.domain.validated_work import (
    PublishValidatedHeadStatus,
    ValidatedWorkFailure,
)
from issue_orchestrator.execution.git_validated_head_executor import (
    GitValidatedHeadExecutor,
)
from .git_escrow_support import git_rig


class Remote:
    def __init__(self, rig):
        self.rig = rig
        self.prs = []
        self.created = 0
        self.read_error = False
        self.lost_create = False
        self.on_branch_read = None
        self.stale_pr_head = None

    def accepts_push_destination(self, command, destination):
        return destination.endpoint == str(self.rig.remote)

    def read_branch(self, command):
        if self.read_error:
            raise PublicationRemoteError("offline")
        result = self.rig.git.run(
            self.rig.remote,
            ["rev-parse", "--verify", f"refs/heads/{command.branch_name}"],
            check=False,
        )
        head = result.stdout.strip() if result.returncode == 0 else None
        if self.on_branch_read:
            callback, self.on_branch_read = self.on_branch_read, None
            callback()
        return head

    def read_pr(self, command, number):
        return next(
            (self.current(command, pr) for pr in self.prs if pr.number == number), None
        )

    def list_prs(self, command):
        return tuple(
            self.current(command, pr)
            for pr in self.prs
            if pr.state is PublicationPrState.OPEN
        )

    def current(self, command, pr):
        return replace(
            pr, head_sha=self.stale_pr_head or self.read_branch(command) or pr.head_sha
        )

    def create_pr(self, command):
        self.created += 1
        pr = self.add_pr(
            command, body=publication_marker(command.issue_number, command.branch_name)
        )
        if self.lost_create:
            raise PublicationRemoteError("create accepted; response lost")
        return pr

    def add_pr(self, command, **kwargs):
        pr = PublicationPullRequest(
            len(self.prs) + 1,
            "https://example/pull/1",
            command.repo_slug,
            command.repo_slug,
            command.branch_name,
            command.pr_base_branch,
            self.read_branch(command) or command.target_head_sha,
            PublicationPrState.OPEN,
            publication_marker(command.issue_number, command.branch_name),
        )
        pr = replace(pr, **kwargs)
        self.prs.append(pr)
        return pr


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    rig = git_rig(tmp_path)
    remote = Remote(rig)
    executor = GitValidatedHeadExecutor(rig.working, remote)
    command = PublishValidatedHeadCommand(
        1,
        "owner/repo",
        "feature",
        rig.target,
        RemoteHeadExpectation.EXACT,
        rig.base,
        rig.root,
        None,
        "main",
        PublicationContent("#1: Feature", "Closes #1\n\nImplementation details", True),
    )
    return rig, remote, executor, command


def test_existing_pr_at_old_commit_does_not_skip_exact_push(setup):
    rig, remote, executor, command = setup
    remote.add_pr(command)
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.PUBLISHED
    assert outcome.pr_head_sha == rig.target
    assert remote.read_branch(command) == rig.target
    assert rig.run("rev-parse", "HEAD") == rig.tip
    assert remote.created == 0


def test_already_target_missing_pr_creates_once(setup):
    rig, remote, executor, command = setup
    rig.run("push", "origin", f"{rig.target}:refs/heads/feature")
    for _ in range(2):
        outcome = executor.publish_or_reconcile(command)
        assert outcome.status is PublishValidatedHeadStatus.ALREADY_AT_TARGET
        assert outcome.push_outcome is None
    assert remote.created == 1


@pytest.mark.parametrize("absent", [False, True])
def test_remote_mutation_after_observation_rejects_atomic_lease(setup, absent):
    rig, remote, executor, command = setup
    if absent:
        command = replace(
            command,
            branch_name="new",
            expectation=RemoteHeadExpectation.ABSENT,
            expected_remote_head_sha=None,
        )
    remote.on_branch_read = lambda: rig.run(
        "push", "origin", f"{rig.tip}:refs/heads/{command.branch_name}"
    )
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.DIVERGED
    assert remote.read_branch(command) == rig.tip
    assert remote.created == 0


def test_failed_read_is_never_absence(setup):
    _, remote, executor, command = setup
    remote.read_error = True
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.TRANSIENT_FAILURE
    assert outcome.push_outcome is None
    assert remote.created == 0


@pytest.mark.parametrize(
    "change,failure",
    [
        (
            {"state": PublicationPrState.CLOSED},
            ValidatedWorkFailure.PR_CLOSED_OR_MERGED,
        ),
        (
            {"state": PublicationPrState.MERGED},
            ValidatedWorkFailure.PR_CLOSED_OR_MERGED,
        ),
        ({"branch": "old-attempt"}, ValidatedWorkFailure.PR_BRANCH_MISMATCH),
        ({"base_branch": "other"}, ValidatedWorkFailure.PR_BRANCH_MISMATCH),
        ({"head_repo": "foreign/repo"}, ValidatedWorkFailure.PR_BRANCH_MISMATCH),
    ],
)
def test_recorded_unusable_pr_refused(setup, change, failure):
    _, remote, executor, command = setup
    pr = remote.add_pr(command, **change)
    outcome = executor.publish_or_reconcile(replace(command, pr_number=pr.number))
    assert outcome.status is PublishValidatedHeadStatus.REJECTED
    assert outcome.failure is failure
    assert remote.created == 0


def test_duplicate_candidate_refusal(setup):
    _, remote, executor, command = setup
    remote.add_pr(command)
    remote.add_pr(command)
    outcome = executor.publish_or_reconcile(command)
    assert outcome.failure is ValidatedWorkFailure.DUPLICATE_OPEN_PR
    assert remote.created == 0


def test_prior_attempt_is_excluded(setup):
    _, remote, executor, command = setup
    remote.add_pr(command, branch="old-attempt")
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.PUBLISHED
    assert outcome.pr_number == 2
    assert remote.created == 1


def test_lost_create_response_recovers_attributable_pr(setup):
    _, remote, executor, command = setup
    remote.lost_create = True
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.PUBLISHED
    assert remote.created == 1
    assert executor.publish_or_reconcile(command).pr_number == outcome.pr_number
    assert remote.created == 1


def test_partial_push_success_survives_stale_pr_read(setup):
    rig, remote, executor, command = setup
    remote.stale_pr_head = rig.base
    remote.add_pr(command)
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.TRANSIENT_FAILURE
    assert outcome.observed_remote_head_sha == rig.target
    assert outcome.push_outcome.value == "pushed"
    remote.stale_pr_head = None
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.ALREADY_AT_TARGET
    assert outcome.pr_head_sha == rig.target


def test_split_and_combined_results_share_composer(setup):
    _, _, executor, command = setup
    branch = executor.push_validated_head(command)
    pr = executor.ensure_pull_request(command)
    expected = compose_publication_outcome(branch, pr)
    assert expected.status is PublishValidatedHeadStatus.PUBLISHED
    combined = executor.publish_or_reconcile(command)
    assert replace(expected, status=combined.status, push_outcome=None) == combined


@pytest.mark.parametrize("target,success", [("target", True), ("divergent", False)])
def test_manual_unconstrained_requires_positive_fast_forward(setup, target, success):
    rig, remote, executor, command = setup
    rig.run("push", "origin", f"{rig.target}:refs/heads/feature")
    command = replace(
        command,
        target_head_sha=getattr(rig, target),
        expectation=RemoteHeadExpectation.UNCONSTRAINED,
        expected_remote_head_sha=None,
    )
    outcome = executor.publish_or_reconcile(command)
    assert (outcome.status is PublishValidatedHeadStatus.ALREADY_AT_TARGET) is success
    assert remote.read_branch(command) == rig.target


def test_missing_target_is_rejected_without_remote_effect(setup):
    rig, remote, executor, command = setup
    outcome = executor.publish_or_reconcile(replace(command, target_head_sha="f" * 40))
    assert outcome.status is PublishValidatedHeadStatus.REJECTED
    assert outcome.failure is ValidatedWorkFailure.VALIDATION_SHA_MISMATCH
    assert outcome.push_outcome is None
    assert remote.read_branch(command) == rig.base
    assert remote.created == 0


def test_remote_move_between_stages_preserves_branch_effect_without_creating_pr(setup):
    rig, remote, executor, command = setup
    branch = executor.push_validated_head(command)
    rig.run("push", "origin", f"{rig.tip}:refs/heads/feature")
    pr = executor.ensure_pull_request(command)
    outcome = compose_publication_outcome(branch, pr)
    assert outcome.status is PublishValidatedHeadStatus.REJECTED
    assert outcome.observed_remote_head_sha == rig.target
    assert outcome.push_outcome.value == "pushed"
    assert remote.read_branch(command) == rig.tip
    assert remote.created == 0


def test_lost_create_response_cannot_adopt_unattributable_racing_pr(setup):
    rig, _, _, command = setup

    class UnrelatedCreate(Remote):
        def create_pr(self, command):
            self.add_pr(command, body="Created by someone else")
            raise PublicationRemoteError("create conflict")

    remote = UnrelatedCreate(rig)
    executor = GitValidatedHeadExecutor(rig.working, remote)
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.REJECTED
    assert outcome.pr_number == 1
    assert outcome.observed_remote_head_sha == rig.target
    assert (
        executor.publish_or_reconcile(command).status
        is PublishValidatedHeadStatus.REJECTED
    )


def test_duplicate_created_during_create_is_refused(setup):
    rig, _, _, command = setup

    class DuplicateCreate(Remote):
        def create_pr(self, command):
            self.add_pr(command)
            return self.add_pr(command)

    executor = GitValidatedHeadExecutor(rig.working, DuplicateCreate(rig))
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.REJECTED
    assert outcome.failure is ValidatedWorkFailure.DUPLICATE_OPEN_PR


@pytest.mark.parametrize("after_create", [False, True])
def test_failed_read_keeps_already_observed_pr_facts(setup, after_create):
    rig, _, _, command = setup

    class FailAfterPr(Remote):
        def list_prs(self, command):
            result = super().list_prs(command)
            if not after_create:
                self.read_error = True
            return result

        def create_pr(self, command):
            result = super().create_pr(command)
            self.read_error = True
            return result

    remote = FailAfterPr(rig)
    if not after_create:
        remote.add_pr(command)
    executor = GitValidatedHeadExecutor(rig.working, remote)
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.TRANSIENT_FAILURE
    assert outcome.pr_number == 1
    assert outcome.pr_url == "https://example/pull/1"
    assert outcome.pr_head_sha == rig.target
    assert outcome.observed_remote_head_sha == rig.target


def test_wrong_issue_marker_is_refused_on_every_retry(setup):
    _, remote, executor, command = setup
    remote.add_pr(command, body=publication_marker(99, command.branch_name))
    for _ in range(2):
        outcome = executor.publish_or_reconcile(command)
        assert outcome.status is PublishValidatedHeadStatus.REJECTED
        assert outcome.failure is ValidatedWorkFailure.PR_BRANCH_MISMATCH
    assert remote.created == 0


def test_recorded_pr_has_explicit_authority_without_marker(setup):
    _, remote, executor, command = setup
    pr = remote.add_pr(command, body="Unmarked but explicitly recorded")
    outcome = executor.publish_or_reconcile(replace(command, pr_number=pr.number))
    assert outcome.status is PublishValidatedHeadStatus.PUBLISHED


@pytest.mark.parametrize("already_target", [False, True])
def test_wrong_push_destination_is_rejected_before_any_remote_effect(
    setup, tmp_path, already_target
):
    rig, remote, executor, command = setup
    if already_target:
        rig.run("push", "origin", f"{rig.target}:refs/heads/feature")
    other = tmp_path / "wrong.git"
    other.mkdir()
    rig.git.run(other, ["init", "--bare"])
    rig.run("remote", "set-url", "--push", "origin", str(other))
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.REJECTED
    assert remote.read_branch(command) == (rig.target if already_target else rig.base)
    assert rig.git.run(other, ["show-ref"], check=False).stdout == ""
    assert remote.created == 0


def test_pushurl_change_after_capture_is_refused(setup, tmp_path):
    rig, remote, executor, command = setup
    other = tmp_path / "wrong.git"
    other.mkdir()
    rig.git.run(other, ["init", "--bare"])
    remote.on_branch_read = lambda: rig.run(
        "remote", "set-url", "--push", "origin", str(other)
    )
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.REJECTED
    assert remote.read_branch(command) == rig.base
    assert rig.git.run(other, ["show-ref"], check=False).stdout == ""


@pytest.mark.parametrize("rewrite", ["insteadOf", "pushInsteadOf"])
@pytest.mark.parametrize("after_capture", [False, True])
def test_url_rewriting_is_refused_without_remote_writes(
    setup, tmp_path, rewrite, after_capture
):
    rig, remote, executor, command = setup
    other = tmp_path / "wrong.git"
    other.mkdir()
    rig.git.run(other, ["init", "--bare"])

    def configure():
        rig.run("config", f"url.{other}.{rewrite}", str(rig.remote))

    if after_capture:
        remote.on_branch_read = configure
    else:
        configure()
    outcome = executor.publish_or_reconcile(command)
    assert outcome.status is PublishValidatedHeadStatus.REJECTED
    assert remote.read_branch(command) == rig.base
    assert rig.git.run(other, ["show-ref"], check=False).stdout == ""


def test_bound_push_preserves_original_pre_push_hook(setup):
    rig, remote, executor, command = setup
    marker = rig.root / "hook-ran"
    hook = rig.root / ".git" / "hooks" / "pre-push"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n')
    hook.chmod(0o755)
    assert (
        executor.publish_or_reconcile(command).status
        is PublishValidatedHeadStatus.PUBLISHED
    )
    assert marker.exists()
    assert remote.read_branch(command) == rig.target
