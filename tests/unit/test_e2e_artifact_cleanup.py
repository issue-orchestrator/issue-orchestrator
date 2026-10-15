"""The live e2e suite's one cleanup owner admits only what a scope proves (#9523).

On 2026-10-14 the suite's cleanup closed 17 real PRs, chosen by workflow
labels real work carries. Every PR close and branch delete in ``tests/e2e``
now goes through ``E2EArtifactCleanup``; these tests drive each of its command
paths in each scope against PRs it must never touch: real work carrying
workflow labels, arbitrary PR numbers, branches whose name alone matches, and
a concurrent run's artifacts.
"""

from datetime import UTC, datetime, timedelta

import pytest

from tests.e2e.fixtures import artifact_cleanup, cleanup
from tests.e2e.fixtures.artifact_cleanup import (
    E2EArtifactCleanup,
    CleanupRefusedError,
    ExamRunScope,
    MAX_LIVE_RUN_AGE,
    RunOwnedScope,
    StaleArtifactScope,
)
from tests.e2e.fixtures.github_utils import E2ETargetRepoError

from .e2e_github_fake import FakeE2EGitHub, pull_request

TEST_REPO = "owner/e2e-test-repo"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
OLD = (NOW - MAX_LIVE_RUN_AGE - timedelta(hours=1)).isoformat()
YOUNG = (NOW - timedelta(minutes=20)).isoformat()

#: This run created issue #123; a concurrent run created #456.
RUN = RunOwnedScope.of([123], ["agent:e2e-test", "io-e2e-test-data", "needs-code-review"])


def _repository() -> FakeE2EGitHub:
    return FakeE2EGitHub([
        # This run's PR.
        pull_request(10, "123-e2e-artifact", ["io-e2e-test-data", "needs-code-review"],
                     body="Closes #123", created_at=YOUNG),
        # A concurrent run's PR: an e2e artifact, but not this run's.
        pull_request(11, "456-e2e-artifact", ["io-e2e-test-data"],
                     body="Closes #456", created_at=YOUNG),
        # Real work under review (#9523's victims), even naming the run's issue.
        pull_request(9483, "9483-tech-lead-work", ["needs-code-review"],
                     body="Closes #9483", created_at=OLD),
        pull_request(9501, "9501-tech-lead-work", ["code-reviewed"],
                     body="Closes #9501", created_at=OLD),
        pull_request(9296, "123-unrelated-work", ["code-reviewed"],
                     body="Refs #123", created_at=OLD),
        # An earlier run's leftovers: open, and closed with its branch left behind.
        pull_request(7, "5-leftover", ["io-e2e-test-data"], body="Closes #5", created_at=OLD),
        pull_request(8, "6-leftover", ["io-e2e-test-data"], body="Closes #6",
                     state="closed", created_at=OLD),
    ])


@pytest.fixture
def github(live_e2e_run) -> FakeE2EGitHub:
    live_e2e_run(TEST_REPO)
    return _repository()


@pytest.fixture
def owner(github: FakeE2EGitHub) -> E2EArtifactCleanup:
    return E2EArtifactCleanup.for_test_repo(TEST_REPO, github=github)


# -- Construction: only a live run, only the validated test repository ------


def test_the_owner_refuses_a_process_that_has_not_opted_in(live_e2e_run):
    live_e2e_run(TEST_REPO)

    with pytest.raises(CleanupRefusedError, match="IO_E2E=1"):
        E2EArtifactCleanup.for_test_repo(TEST_REPO, github=_repository(), environ={})


def test_the_owner_refuses_an_agent_session_even_opted_in(live_e2e_run):
    live_e2e_run(TEST_REPO)

    with pytest.raises(CleanupRefusedError, match="Agent sessions may never run it"):
        E2EArtifactCleanup.for_test_repo(
            TEST_REPO,
            github=_repository(),
            environ={"IO_E2E": "1", "ISSUE_ORCHESTRATOR_SESSION_ID": "coding-1"},
        )


def test_the_owner_refuses_any_repository_but_the_test_repository(live_e2e_run):
    live_e2e_run(TEST_REPO)

    with pytest.raises(CleanupRefusedError, match="not the validated e2e test repository"):
        E2EArtifactCleanup.for_test_repo("owner/real-work", github=_repository())


def test_the_owner_refuses_this_checkouts_own_repository(live_e2e_run):
    live_e2e_run("Owner/Production", production_repo="owner/production")

    with pytest.raises(E2ETargetRepoError, match="this checkout's own repository"):
        E2EArtifactCleanup.for_test_repo("Owner/Production", github=_repository())


def test_the_owner_refuses_when_the_checkouts_origin_is_unreadable(monkeypatch, live_e2e_run):
    """Unknown is refused rather than guessed: nothing proves the target is
    not production."""
    from tests.e2e.fixtures import github_utils

    live_e2e_run(TEST_REPO)

    def unreadable() -> str:
        raise E2ETargetRepoError("Cannot read a GitHub origin remote for this checkout")

    monkeypatch.setattr(github_utils, "get_repo_from_git", unreadable)

    with pytest.raises(E2ETargetRepoError, match="Cannot read a GitHub origin"):
        E2EArtifactCleanup.for_test_repo(TEST_REPO, github=_repository())


# -- RunOwnedScope: a run's own teardown ------------------------------------


def test_run_teardown_closes_only_the_runs_own_e2e_pr(owner, github):
    closed = owner.sweep(RUN)

    assert closed == 1
    assert github.writes == ([10], ["123-e2e-artifact"])


def test_closing_the_runs_own_pr_deletes_its_branch(owner, github):
    owner.close_pr(RUN, 10)

    assert github.writes == ([10], ["123-e2e-artifact"])


@pytest.mark.parametrize(
    ("pr_number", "why"),
    [
        (9483, "real work carrying a workflow label"),
        (9501, "real work carrying a workflow label"),
        (9296, "real work naming the run's issue, without an e2e label"),
        (11, "a concurrent run's e2e PR"),
        (7, "an earlier run's e2e PR"),
        (424242, "an arbitrary PR number that cannot be read"),
    ],
)
def test_run_teardown_refuses_any_other_pr(owner, github, pr_number, why):
    with pytest.raises(CleanupRefusedError):
        owner.close_pr(RUN, pr_number)

    assert github.writes == ([], []), why


def test_run_teardown_never_deletes_a_branch_by_name(owner, github):
    """``123-unrelated-work`` matches the run's issue number by name only."""
    for branch in ("123-unrelated-work", "123-e2e-artifact"):
        with pytest.raises(CleanupRefusedError):
            owner.delete_branch(RUN, branch)

    assert github.writes == ([], [])


def test_a_run_with_no_e2e_marker_lists_nothing(owner, github):
    unmarked = RunOwnedScope.of([123], ["needs-code-review", "code-reviewed"])

    assert owner.sweep(unmarked) == 0
    assert github.writes == ([], [])


# -- StaleArtifactScope: reconciliation of earlier runs' leftovers ----------


def test_stale_reconciliation_removes_only_old_e2e_artifacts(owner, github):
    closed = owner.sweep(StaleArtifactScope.as_of(NOW))

    assert closed == 1
    assert github.closed == [7]
    assert sorted(github.deleted_branches) == ["5-leftover", "6-leftover"]


@pytest.mark.parametrize(
    ("pr_number", "why"),
    [
        (10, "this run's live PR"),
        (11, "a concurrent run's live PR"),
        (9483, "real work carrying a workflow label"),
        (9296, "real work naming an e2e issue"),
        (424242, "an arbitrary PR number that cannot be read"),
    ],
)
def test_stale_reconciliation_refuses_any_other_pr(owner, github, pr_number, why):
    with pytest.raises(CleanupRefusedError):
        owner.close_pr(StaleArtifactScope.as_of(NOW), pr_number)

    assert github.writes == ([], []), why


@pytest.mark.parametrize("created_at", [None, "", "not-a-time", "2026-01-01T00:00:00"])
def test_an_e2e_pr_whose_age_is_unproven_is_never_stale(live_e2e_run, created_at):
    live_e2e_run(TEST_REPO)
    github = FakeE2EGitHub([
        pull_request(7, "5-leftover", ["io-e2e-test-data"], created_at=created_at),
    ])
    owner = E2EArtifactCleanup.for_test_repo(TEST_REPO, github=github)

    assert owner.sweep(StaleArtifactScope.as_of(NOW)) == 0
    assert github.writes == ([], [])


def test_stale_reconciliation_never_deletes_a_branch_a_live_pr_still_heads(live_e2e_run):
    """Deleting the old PR's branch would close the young PR it also heads."""
    live_e2e_run(TEST_REPO)
    github = FakeE2EGitHub([
        pull_request(7, "5-shared", ["io-e2e-test-data"], state="closed", created_at=OLD),
        pull_request(12, "5-shared", ["io-e2e-test-data"], created_at=YOUNG),
    ])
    owner = E2EArtifactCleanup.for_test_repo(TEST_REPO, github=github)

    assert owner.sweep(StaleArtifactScope.as_of(NOW)) == 0
    with pytest.raises(CleanupRefusedError, match="PR #12"):
        owner.close_pr(StaleArtifactScope.as_of(NOW), 7)
    assert github.writes == ([], [])


def test_the_owner_cannot_be_built_without_validation():
    with pytest.raises(TypeError, match="for_test_repo"):
        E2EArtifactCleanup(TEST_REPO, _repository(), validated=object())


def test_stale_reconciliation_never_deletes_a_branch_by_name(owner, github):
    with pytest.raises(CleanupRefusedError):
        owner.delete_branch(StaleArtifactScope.as_of(NOW), "5-leftover")

    assert github.writes == ([], [])


def test_session_reconciliation_runs_in_the_stale_scope(monkeypatch, live_e2e_run):
    """The e2e conftest's reconciliation step, end to end: the real clock puts
    a concurrent run's PR inside the live-run window."""
    live_e2e_run(TEST_REPO)
    now = datetime.now(UTC)
    old = (now - MAX_LIVE_RUN_AGE - timedelta(hours=1)).isoformat()
    young = (now - timedelta(minutes=5)).isoformat()
    repository = FakeE2EGitHub([
        pull_request(7, "5-leftover", ["io-e2e-test-data"], created_at=old),
        pull_request(11, "456-e2e-artifact", ["io-e2e-test-data"], created_at=young),
        pull_request(9483, "9483-tech-lead-work", ["needs-code-review"], created_at=old),
    ])
    monkeypatch.setattr(artifact_cleanup, "_github_adapter", lambda _repo: repository)

    assert cleanup.reconcile_stale_prs(TEST_REPO) == 1
    assert repository.writes == ([7], ["5-leftover"])


# -- ExamRunScope: an exam run's strict teardown ----------------------------


EXAM = ExamRunScope(issue_numbers=frozenset({7}), pushed_branches=frozenset({"seed-exam-a"}))


def _exam_repository() -> FakeE2EGitHub:
    return FakeE2EGitHub(
        [
            # A recovery PR: no e2e label, linked to the run's issue.
            pull_request(70, "7-exam-work", [], body="Closes #7"),
            pull_request(71, "seed-exam-a", [], body="seeded"),
            # Another exam run's PR, and real work.
            pull_request(80, "8-exam-work", ["io-e2e-test-data"], body="Closes #8"),
            pull_request(9483, "9483-tech-lead-work", ["needs-code-review"], body="Closes #9483"),
        ],
        branches=["70-7-lookalike", "8-no-pr"],
    )


@pytest.fixture
def exam_github(live_e2e_run) -> FakeE2EGitHub:
    live_e2e_run(TEST_REPO)
    return _exam_repository()


@pytest.fixture
def exam_owner(exam_github: FakeE2EGitHub) -> E2EArtifactCleanup:
    return E2EArtifactCleanup.for_test_repo(TEST_REPO, github=exam_github)


def test_exam_teardown_removes_the_runs_pr_and_pushed_branch(exam_owner, exam_github):
    exam_owner.close_pr(EXAM, 70)
    exam_owner.delete_branch(EXAM, "seed-exam-a")

    assert exam_github.closed == [70, 71]
    assert exam_github.deleted_branches == ["7-exam-work", "seed-exam-a"]


@pytest.mark.parametrize("pr_number", [80, 9483, 424242])
def test_exam_teardown_refuses_another_runs_pr_real_work_and_arbitrary_numbers(
    exam_owner, exam_github, pr_number
):
    with pytest.raises(CleanupRefusedError):
        exam_owner.close_pr(EXAM, pr_number)

    assert exam_github.writes == ([], [])


@pytest.mark.parametrize(
    "branch",
    ["70-7-lookalike", "8-exam-work", "8-no-pr", "9483-tech-lead-work", "main"],
)
def test_exam_teardown_refuses_branches_it_has_no_provenance_for(exam_owner, exam_github, branch):
    with pytest.raises(CleanupRefusedError):
        exam_owner.delete_branch(EXAM, branch)

    assert exam_github.writes == ([], [])
