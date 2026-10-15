"""The live e2e suite's one cleanup owner admits only what a scope proves (#9523).

On 2026-10-14 the suite's cleanup closed 17 real PRs, chosen by workflow
labels real work carries. Every PR close and branch delete in ``tests/e2e``
now goes through ``E2EArtifactCleanup``; these tests drive each of its command
paths in each scope against PRs it must never touch: real work carrying
workflow labels, arbitrary PR numbers, branches whose name alone matches, a
concurrent run's artifacts (however old), and PRs a branch deletion would close
behind the scope's back.
"""

import pytest

from tests.e2e import reconcile
from tests.e2e.fixtures import artifact_cleanup, cleanup
from tests.e2e.fixtures.artifact_cleanup import (
    CleanupRefusedError,
    E2EArtifactCleanup,
    ExamRunScope,
    IdleRepositoryScope,
    RunIssues,
    RunOwnedScope,
)
from tests.e2e.fixtures.github_utils import E2ETargetRepoError

from .e2e_github_fake import FakeE2EGitHub, IncompleteReadError, pull_request

TEST_REPO = "owner/e2e-test-repo"

#: This run created issue #123; a concurrent run created #456.
RUN = RunOwnedScope.of([123], ["agent:e2e-test", "io-e2e-test-data", "needs-code-review"])


def _repository() -> FakeE2EGitHub:
    return FakeE2EGitHub([
        # This run's PR.
        pull_request(10, "123-e2e-artifact", ["io-e2e-test-data", "needs-code-review"],
                     body="Closes #123"),
        # A concurrent run's PR: an e2e artifact, but not this run's. How old
        # it is proves nothing: its run may still be going.
        pull_request(11, "456-e2e-artifact", ["io-e2e-test-data"], body="Closes #456"),
        # Real work under review (#9523's victims), even naming the run's issue.
        pull_request(9483, "9483-tech-lead-work", ["needs-code-review"], body="Closes #9483"),
        pull_request(9501, "9501-tech-lead-work", ["code-reviewed"], body="Closes #9501"),
        pull_request(9296, "123-unrelated-work", ["code-reviewed"], body="Refs #123"),
        # An earlier run's leftovers: open, and closed with its branch left behind.
        pull_request(7, "5-leftover", ["io-e2e-test-data"], body="Closes #5"),
        pull_request(8, "6-leftover", ["io-e2e-test-data"], body="Closes #6", state="closed"),
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


def test_the_owner_cannot_be_built_without_validation():
    with pytest.raises(TypeError, match="for_test_repo"):
        E2EArtifactCleanup(TEST_REPO, _repository(), validated=object())


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


# -- Deleting a branch closes the PRs it heads and bases: read completely ---


@pytest.mark.parametrize(
    "hidden",
    [
        # Real work sharing the run's PR's head.
        pull_request(9600, "123-e2e-artifact", ["needs-code-review"], body="Closes #9600"),
        # Real work stacked on the run's PR's branch: deleting it closes this too.
        pull_request(9601, "9601-stacked-work", ["code-reviewed"], body="Closes #9601",
                     base="123-e2e-artifact"),
    ],
    ids=["shared-head", "stacked-on-base"],
)
def test_a_pr_the_branch_deletion_would_close_is_judged_from_complete_reads(live_e2e_run, hidden):
    """A cached per-branch read answers with the run's PR alone; the complete
    reads find the other one, and nothing is written (#9523 review F3)."""
    live_e2e_run(TEST_REPO)
    runs_pr = pull_request(10, "123-e2e-artifact", ["io-e2e-test-data"], body="Closes #123")
    github = FakeE2EGitHub([runs_pr, hidden])
    github.cached_for_branch["123-e2e-artifact"] = [runs_pr]
    owner = E2EArtifactCleanup.for_test_repo(TEST_REPO, github=github)
    run_branch = ExamRunScope(frozenset({123}), run_branches=frozenset({"123-e2e-artifact"}))

    with pytest.raises(CleanupRefusedError, match=f"PR #{hidden.number}"):
        owner.close_pr(RUN, 10)
    assert owner.sweep(RUN) == 0
    if hidden.number == 9601:
        with pytest.raises(CleanupRefusedError, match="PR #9601"):
            owner.delete_branch(run_branch, "123-e2e-artifact")

    assert github.writes == ([], [])


def test_an_incomplete_read_refuses_the_close_and_the_delete(live_e2e_run):
    live_e2e_run(TEST_REPO)
    github = FakeE2EGitHub([
        pull_request(10, "123-e2e-artifact", ["io-e2e-test-data"], body="Closes #123"),
    ])
    github.incomplete_reads = True
    owner = E2EArtifactCleanup.for_test_repo(TEST_REPO, github=github)
    run_branch = ExamRunScope(frozenset(), run_branches=frozenset({"123-e2e-artifact"}))

    with pytest.raises(IncompleteReadError):
        owner.close_pr(RUN, 10)
    with pytest.raises(IncompleteReadError):
        owner.delete_branch(run_branch, "123-e2e-artifact")
    assert owner.sweep(RUN) == 0

    assert github.writes == ([], [])


# -- The session: its own PRs at the end, other runs' only reported ---------


def test_every_e2e_issue_is_recorded_as_the_runs_when_github_answers(monkeypatch):
    """Recorded before the visibility wait, so an issue whose create succeeded
    is the run's even if a later step fails."""
    from tests.e2e.fixtures import data_factory

    run_issues = RunIssues()
    monkeypatch.setattr(data_factory, "RUN_ISSUES", run_issues)

    def create_issue(repo, title, labels, body, *, on_created, ensure_labels):
        on_created(77)
        raise TimeoutError("issue #77 never became visible")

    monkeypatch.setattr(data_factory, "create_issue", create_issue)
    seen: list[int] = []

    with pytest.raises(TimeoutError):
        data_factory.create_run_issue("o/r", "t", ["agent:e2e-test", "io-e2e-test-data"],
                                      on_created=seen.append)

    assert seen == [77]
    assert run_issues.scope() == RunOwnedScope(frozenset({77}), frozenset({"io-e2e-test-data"}))


def test_the_session_removes_the_prs_of_the_issues_it_recorded(monkeypatch, github):
    run_issues = RunIssues()
    run_issues.record(123, ["agent:e2e-test", "io-e2e-test-data"])
    monkeypatch.setattr(artifact_cleanup, "_github_adapter", lambda _repo: github)

    assert cleanup.cleanup_run_prs(TEST_REPO, run_issues) == 1
    assert github.writes == ([10], ["123-e2e-artifact"])


def test_a_session_that_recorded_nothing_removes_nothing(monkeypatch, github):
    monkeypatch.setattr(artifact_cleanup, "_github_adapter", lambda _repo: github)

    assert cleanup.cleanup_run_prs(TEST_REPO, RunIssues()) == 0
    assert github.writes == ([], [])


def test_the_session_only_reports_other_runs_prs_however_old(monkeypatch, github, caplog):
    """No fixed age proves a run has ended: a still-live run's PR (#11), from
    however long ago, is reported and never written to (#9523 review F4)."""
    monkeypatch.setattr(artifact_cleanup, "_github_adapter", lambda _repo: github)

    with caplog.at_level("WARNING"):
        assert cleanup.report_leftover_prs(TEST_REPO) == 3

    assert github.writes == ([], [])
    assert "python -m tests.e2e.reconcile --no-live-run-against owner/e2e-test-repo" in caplog.text


# -- IdleRepositoryScope: only on an operator's explicit word ---------------


def test_the_operator_reconciliation_removes_every_e2e_pr(github):
    assert reconcile.reconcile(TEST_REPO, github=github) == 3

    assert sorted(github.closed) == [7, 10, 11]
    assert sorted(github.deleted_branches) == [
        "123-e2e-artifact", "456-e2e-artifact", "5-leftover", "6-leftover",
    ]


def test_the_operator_must_name_the_test_repository(github):
    with pytest.raises(CleanupRefusedError, match="not the validated e2e test repository"):
        reconcile.reconcile("owner/real-work", github=github)

    assert github.writes == ([], [])


def test_the_operator_command_requires_the_confirmation(capsys):
    with pytest.raises(SystemExit):
        reconcile.main([])

    assert "--no-live-run-against" in capsys.readouterr().err


def test_an_idle_repository_confirmation_never_admits_real_work(owner, github):
    for pr_number in (9483, 9501, 9296):
        with pytest.raises(CleanupRefusedError):
            owner.close_pr(IdleRepositoryScope(repo=TEST_REPO), pr_number)

    assert github.writes == ([], [])


def test_an_idle_confirmation_for_another_repository_admits_nothing(owner, github):
    assert owner.sweep(IdleRepositoryScope(repo="owner/other-repo")) == 0
    assert github.writes == ([], [])


# -- ExamRunScope: an exam run's strict teardown ----------------------------


EXAM = ExamRunScope(issue_numbers=frozenset({7}), run_branches=frozenset({"seed-exam-a"}))


def _exam_repository() -> FakeE2EGitHub:
    return FakeE2EGitHub(
        [
            # A recovery PR: no e2e label, its body links the run's issue.
            pull_request(70, "7-exam-work", [], body="Closes #7"),
            pull_request(71, "seed-exam-a", [], body="seeded"),
            # Named after the run's issue, but nothing records it as the run's.
            pull_request(72, "7-unrelated", [], body="human work"),
            # Another exam run's PR, and real work.
            pull_request(80, "8-exam-work", ["io-e2e-test-data"], body="Closes #8"),
            pull_request(9483, "9483-tech-lead-work", ["needs-code-review"], body="Closes #9483"),
        ],
        branches=["70-7-lookalike", "7-no-pr", "8-no-pr"],
    )


@pytest.fixture
def exam_github(live_e2e_run) -> FakeE2EGitHub:
    live_e2e_run(TEST_REPO)
    return _exam_repository()


@pytest.fixture
def exam_owner(exam_github: FakeE2EGitHub) -> E2EArtifactCleanup:
    return E2EArtifactCleanup.for_test_repo(TEST_REPO, github=exam_github)


def test_exam_teardown_removes_the_runs_pr_and_recorded_branch(exam_owner, exam_github):
    exam_owner.close_pr(EXAM, 70)
    exam_owner.delete_branch(EXAM, "seed-exam-a")

    assert exam_github.closed == [70, 71]
    assert exam_github.deleted_branches == ["7-exam-work", "seed-exam-a"]


@pytest.mark.parametrize("pr_number", [72, 80, 9483, 424242])
def test_exam_teardown_refuses_unrecorded_prs_another_runs_real_work_and_arbitrary_numbers(
    exam_owner, exam_github, pr_number
):
    with pytest.raises(CleanupRefusedError):
        exam_owner.close_pr(EXAM, pr_number)

    assert exam_github.writes == ([], [])


@pytest.mark.parametrize(
    "branch",
    ["7-unrelated", "7-no-pr", "70-7-lookalike", "8-exam-work", "9483-tech-lead-work", "main"],
)
def test_exam_teardown_refuses_branches_it_did_not_record(exam_owner, exam_github, branch):
    """Owned issue #7 makes ``7-unrelated`` match by name; that is not provenance
    (#9523 review F2)."""
    with pytest.raises(CleanupRefusedError):
        exam_owner.delete_branch(EXAM, branch)

    assert exam_github.writes == ([], [])
