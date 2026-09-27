"""The exam harness's test-only seams: the config overlay and the fault shim.

The shim is run for real in a scratch git repository with recording
``coding-done`` / ``reviewer-done`` stand-ins first on PATH — the same way the
engine puts its own completion wrappers first on an agent's PATH.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e.exam.agents import SHIM, shim_command
from tests.e2e.exam.run_identity import RunIdentity
from tests.e2e.fixtures.orchestrator_process import merge_config_overlay


def test_overlay_merges_mappings_and_replaces_values() -> None:
    base = {
        "review": {"default": "agent:r", "exchange": {"mode": "via-draft-pr"}},
        "agents": {"a": {"model": "sonnet"}},
    }
    overlay = {
        "review": {"exchange": {"mode": "via-local-loop", "loop": {"max_rounds": 3}}},
        "tech_lead": {"authority": {"reset_retry": "execute"}},
        "agents": {"a": "replaced"},
    }

    merged = merge_config_overlay(base, overlay)

    assert merged == {
        "review": {
            "default": "agent:r",
            "exchange": {"mode": "via-local-loop", "loop": {"max_rounds": 3}},
        },
        "agents": {"a": "replaced"},
        "tech_lead": {"authority": {"reset_retry": "execute"}},
    }
    assert base["review"]["exchange"] == {"mode": "via-draft-pr"}  # not mutated


def test_shim_command_has_no_template_placeholders() -> None:
    """The engine ``str.format``s agent commands; a stray brace would crash launch."""
    command = shim_command("reviewer", exchange_fault="exit-silently")
    assert "{" not in command and "}" not in command
    assert str(SHIM) in command and "--exchange-fault exit-silently" in command


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "exam@example.com"],
        ["git", "config", "user.name", "Exam"],
        ["git", "commit", "-q", "--allow-empty", "-m", "root"],
    ):
        subprocess.run(argv, cwd=repo, check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    for name in ("coding-done", "reviewer-done"):
        tool = bin_dir / name
        tool.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\n', encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return {"repo": repo, "bin": bin_dir, "calls": calls}


def _run_shim(
    sandbox: dict[str, Path], *args: str, exchange: bool = False, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{sandbox['bin']}{os.pathsep}{os.environ['PATH']}",
        "E2E_PR_LABELS": "io:e2e:exam-a-1,io-e2e-test-data",
    }
    env.pop("ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE", None)
    if exchange:
        env["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"] = str(sandbox["repo"] / "response.json")
    return subprocess.run(
        [sys.executable, str(SHIM), *args],
        cwd=sandbox["repo"],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _calls(sandbox: dict[str, Path]) -> list[str]:
    path = sandbox["calls"]
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _commits(sandbox: dict[str, Path]) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=sandbox["repo"], capture_output=True, text=True, check=True
    )
    return int(out.stdout.strip())


def test_initial_coder_commits_and_completes_with_the_pr_labels(sandbox: dict[str, Path]) -> None:
    result = _run_shim(sandbox, "--role", "coder")

    assert result.returncode == 0, result.stderr
    assert _commits(sandbox) == 2
    assert _calls(sandbox) == [
        "coding-done completed --implementation Exam work item committed --problems None"
        " --pr-labels io:e2e:exam-a-1 io-e2e-test-data"
    ]


def test_exchange_coder_never_moves_the_validated_head(sandbox: dict[str, Path]) -> None:
    result = _run_shim(sandbox, "--role", "coder", exchange=True, stdin="review round 1\n")

    assert result.returncode == 0, result.stderr
    assert _commits(sandbox) == 1
    assert _calls(sandbox) == []


def test_planted_reviewer_fault_exits_without_a_verdict(sandbox: dict[str, Path]) -> None:
    result = _run_shim(
        sandbox, "--role", "reviewer", "--exchange-fault", "exit-silently", exchange=True
    )

    assert result.returncode == 0, result.stderr
    assert _calls(sandbox) == []
    assert not (sandbox["repo"] / "response.json").exists()


def test_post_publish_review_approves(sandbox: dict[str, Path]) -> None:
    result = _run_shim(sandbox, "--role", "reviewer", "--exchange-fault", "exit-silently")

    assert result.returncode == 0, result.stderr
    assert _calls(sandbox) == ["reviewer-done approved --summary Exam reviewer: approved --risk low"]


def test_unplanned_exchange_review_fails_loudly(sandbox: dict[str, Path]) -> None:
    result = _run_shim(sandbox, "--role", "reviewer", exchange=True)

    assert result.returncode != 0
    assert "without a planted fault" in result.stderr
    assert _calls(sandbox) == []


def test_cleanup_runs_every_step_then_raises_every_failure() -> None:
    from tests.e2e.exam.cleanup_steps import run_all_steps

    ran: list[str] = []

    def fail(name: str) -> None:
        ran.append(name)
        raise RuntimeError(f"{name} failed")

    with pytest.raises(ExceptionGroup) as caught:
        run_all_steps(
            "exam cleanup",
            [
                ("close PRs", lambda: fail("close PRs")),
                ("close issues", lambda: ran.append("close issues")),
                ("close labelled", lambda: fail("close labelled")),
            ],
        )

    assert ran == ["close PRs", "close issues", "close labelled"]
    assert [str(e) for e in caught.value.exceptions] == ["close PRs failed", "close labelled failed"]


def test_cleanup_that_succeeds_raises_nothing() -> None:
    from tests.e2e.exam.cleanup_steps import run_all_steps

    ran: list[str] = []
    run_all_steps("exam cleanup", [("a", lambda: ran.append("a")), ("b", lambda: ran.append("b"))])
    assert ran == ["a", "b"]


class _FakeAdapter:
    """The slice of GitHubAdapter seeding and branch cleanup use."""

    def __init__(self, *, fail_create: bool = False) -> None:
        self.fail_create = fail_create
        self.branches: set[str] = set()
        self.closed: list[int] = []
        self.deleted: list[str] = []
        self.prs: dict[str, list] = {}

    def create_pr(self, **_kwargs):
        if self.fail_create:
            raise RuntimeError("create_pr failed")
        raise AssertionError("not reached in these tests")

    def get_prs_for_branch(self, branch: str):
        return self.prs.get(branch, [])

    def close_pr(self, number: int) -> None:
        self.closed.append(number)

    def branch_exists(self, branch: str) -> bool:
        return branch in self.branches

    def delete_branch(self, branch: str) -> None:
        self.deleted.append(branch)
        self.branches.discard(branch)


def test_a_seed_branch_is_registered_before_its_pr_can_fail(monkeypatch, tmp_path) -> None:
    """Round 1 F6: push lands, create_pr fails — cleanup must still own the branch."""
    from tests.e2e.exam import seeding

    adapter = _FakeAdapter(fail_create=True)
    pushed: list[str] = []

    remotes: list[str] = []

    def fake_git(_cwd, *argv, env=None, stdin=None):
        if argv[0] in ("fetch", "push"):
            remotes.extend(arg for arg in argv if arg.startswith(("https://", "origin")))
        if argv[0] == "push":
            branch = argv[-1].split("refs/heads/")[1]
            pushed.append(branch)
            adapter.branches.add(branch)
        return "0" * 40 if argv[0] != "var" else "Exam <e@x> 1 +0000"

    monkeypatch.setattr(seeding, "_git", fake_git)
    monkeypatch.setattr(seeding, "_github_adapter", lambda _repo: adapter)
    registered: list[str] = []

    with pytest.raises(RuntimeError, match="create_pr failed"):
        seeding.seed_pull_request(
            repo="o/r", repo_root=tmp_path, issue_number=7, slug="exam", labels=[], draft=True,
            register_branch=registered.append,
        )

    assert registered == pushed == ["7-exam"]
    # Round 9 F1: fetch and push address the RUN's repository, never an origin.
    assert remotes == ["https://github.com/o/r.git", "https://github.com/o/r.git"]
    from tests.e2e.exam import cleanup

    monkeypatch.setattr(cleanup, "_github_adapter", lambda _repo: adapter)
    cleanup.delete_registered_branches("o/r", registered)
    assert adapter.deleted == ["7-exam"] and adapter.closed == []


def test_branch_cleanup_closes_the_open_pr_then_deletes_the_branch(monkeypatch) -> None:
    from types import SimpleNamespace

    from tests.e2e.exam import cleanup

    adapter = _FakeAdapter()
    adapter.branches = {"8-exam"}
    adapter.prs = {"8-exam": [SimpleNamespace(number=80, state="open")]}
    monkeypatch.setattr(cleanup, "_github_adapter", lambda _repo: adapter)

    cleanup.delete_registered_branches("o/r", ["8-exam", "9-gone"])

    assert adapter.closed == [80]
    assert adapter.deleted == ["8-exam"]


@pytest.mark.parametrize("failing_step", ["clone", "fetch"])
def test_a_failed_engine_checkout_leaves_nothing_behind(monkeypatch, tmp_path, failing_step) -> None:
    """Round 2 F4: the clone itself (not only the steps after it) is inside the cleanup."""
    from tests.e2e.exam import engine_checkout

    harness = tmp_path / "harness"
    (harness / ".venv").mkdir(parents=True)
    parent = tmp_path / "engines"
    parent.mkdir()

    def fake_git(cwd, *argv):
        if argv[0] == "rev-parse":
            return "a" * 40
        if argv[:2] == ("remote", "get-url"):
            return "https://example.invalid/o/r.git"
        if argv[0] == "clone":
            dest = Path(argv[-1])
            (dest / ".git").mkdir(parents=True, exist_ok=True)  # git creates it, then fails
        if argv[0] == failing_step:
            raise RuntimeError(f"git {argv[0]} failed")
        return ""

    monkeypatch.setattr(engine_checkout, "_git", fake_git)

    with pytest.raises(RuntimeError, match=f"git {failing_step} failed"):
        engine_checkout.EngineCheckout.create(harness_root=harness, ref="HEAD", identity=RunIdentity.new("A"), repo="o/r", parent=parent)

    assert list(parent.iterdir()) == []
    assert (harness / ".venv").is_dir()  # the harness venv is never followed into


def test_a_built_checkout_removes_cleanly_without_touching_the_harness_venv(monkeypatch, tmp_path) -> None:
    from tests.e2e.exam import engine_checkout

    harness = tmp_path / "harness"
    (harness / ".venv" / "bin").mkdir(parents=True)
    parent = tmp_path / "engines"
    parent.mkdir()
    monkeypatch.setattr(
        engine_checkout, "_git", lambda cwd, *argv: "b" * 40 if argv[0] == "rev-parse" else ""
    )

    checkout = engine_checkout.EngineCheckout.create(harness_root=harness, ref="HEAD", identity=RunIdentity.new("B"), repo="o/r", parent=parent)

    assert (checkout.root / ".venv").is_symlink()
    checkout.remove()
    assert list(parent.iterdir()) == []
    assert (harness / ".venv" / "bin").is_dir()


def _pr_info(number: int, issue: int, state: str = "open"):
    from issue_orchestrator.ports.pull_request_tracker import PRInfo

    return PRInfo(
        number=number, title=f"#{issue}: x", url="u", branch=f"{issue}-x", body=f"Closes #{issue}",
        state=state, labels=[],
    )


class _PagedPulls:
    """The adapter's two complete PR reads over a fixed PR set."""

    def __init__(self, prs):
        self.prs = sorted(prs, key=lambda pr: -pr.number)

    def list_prs_numbered_above(self, floor: int):
        return [pr for pr in self.prs if pr.number > floor]

    def list_open_prs_complete(self):
        return [pr for pr in self.prs if pr.state == "open"]


def test_an_items_closed_pr_behind_many_newer_prs_is_observed(monkeypatch) -> None:
    """Round 2 F3 / round 3 F3: seen however many newer PRs exist, never guessed."""
    from tests.e2e.exam import observe

    old_attempt = _pr_info(1001, 1000, state="closed")
    current = _pr_info(1250, 1000)
    newer = [_pr_info(1300 + i, 1299 + i) for i in range(150)]
    monkeypatch.setattr(observe, "_github_adapter", lambda _repo: _PagedPulls([old_attempt, current, *newer]))

    assert [pr.number for pr in observe.linked_pull_requests("o/r", 1000, state="all")] == [1250, 1001]


def test_an_unsupported_pr_state_is_refused(monkeypatch) -> None:
    from tests.e2e.exam import observe

    monkeypatch.setattr(observe, "_github_adapter", lambda _repo: _PagedPulls([]))
    with pytest.raises(ValueError, match="unsupported PR state"):
        observe.linked_pull_requests("o/r", 1000, state="closed")


def test_open_prs_come_from_the_complete_walk_not_one_page(monkeypatch) -> None:
    """Teardown must find the item's open PR however many newer PRs exist."""
    from tests.e2e.exam import observe

    item_pr = _pr_info(1001, 1000)
    newer = [_pr_info(2000 + i, 1500 + i) for i in range(105)]
    monkeypatch.setattr(observe, "_github_adapter", lambda _repo: _PagedPulls([item_pr, *newer]))

    assert [pr.number for pr in observe.linked_pull_requests("o/r", 1000, state="open")] == [1001]



def test_two_runs_of_one_case_in_the_same_second_never_share_an_identity(monkeypatch) -> None:
    """Round 6 F1: cleanup selects by label, so a shared label lets one run
    close another's live issues and PRs."""
    import time as time_module

    monkeypatch.setattr(time_module, "time", lambda: 1_790_000_000.0)
    first, second = RunIdentity.new("A-case"), RunIdentity.new("A-case")

    assert first.label != second.label
    assert first.checkout_name("a" * 40) != second.checkout_name("a" * 40)
    assert first.label.startswith("io:e2e:exam-a-")  # the prefix real engines exclude


class _StrictFake(_FakeAdapter):
    """Branch deletion that can silently fail, and a PR the exam can close."""

    def __init__(self, *, delete_works: bool) -> None:
        super().__init__()
        self.delete_works = delete_works
        self.pr_state = {70: "open"}
        self.issues: list = []

    def delete_branch(self, branch: str) -> None:
        self.deleted.append(branch)
        if self.delete_works:
            self.branches.discard(branch)

    def get_pr(self, number: int):
        from types import SimpleNamespace

        return SimpleNamespace(number=number, state=self.pr_state[number], branch="7-exam")

    def close_pr(self, number: int) -> None:
        self.closed.append(number)
        self.pr_state[number] = "closed"

    def list_issues(self, labels, state):
        return self.issues


def test_a_pr_whose_branch_survives_deletion_fails_cleanup(monkeypatch) -> None:
    """Round 6 F2: the shared close_pr swallowed this; the exam must not."""
    from tests.e2e.exam import cleanup

    adapter = _StrictFake(delete_works=False)
    adapter.branches = {"7-exam"}
    monkeypatch.setattr(cleanup, "_github_adapter", lambda _repo: adapter)

    with pytest.raises(cleanup.ExamCleanupError, match="branch 7-exam still exists"):
        cleanup.remove_pr("o/r", 70)
    assert adapter.closed == [70] and adapter.deleted == ["7-exam"]


def test_teardown_removes_a_recovery_pr_strictly_and_reports_the_failure(monkeypatch) -> None:
    """A recovery-published PR (no cleanup labels) found through the issue
    link; its branch deletion failing surfaces through run_all_steps while the
    other steps still run."""
    from types import SimpleNamespace

    from tests.e2e.exam import cleanup
    from tests.e2e.exam.cleanup_steps import run_all_steps

    adapter = _StrictFake(delete_works=False)
    adapter.branches = {"7-exam"}
    monkeypatch.setattr(cleanup, "_github_adapter", lambda _repo: adapter)
    def linked(_repo, number, state):
        assert state == "all", "teardown must see closed PRs too (their branches can survive)"
        return [SimpleNamespace(number=70)] if number == 7 else []

    monkeypatch.setattr(cleanup, "linked_pull_requests", linked)
    ran: list[str] = []

    with pytest.raises(ExceptionGroup) as caught:
        run_all_steps(
            "exam cleanup",
            [
                ("teardown", lambda: cleanup.teardown_run("o/r", "io:e2e:exam-a-x", [7])),
                ("close issues", lambda: ran.append("close issues")),
            ],
        )

    assert ran == ["close issues"]
    assert [type(e) for e in caught.value.exceptions] == [cleanup.ExamCleanupError]
    assert "7-exam" in str(caught.value.exceptions[0])


def _exit_observation(ended_by, report):
    from issue_orchestrator.testing.exam.cases import STALE_CLAIM_PAUSED_FOR_RECONCILE
    from tests.e2e.exam.observe import build_observation

    from .builders import item

    return build_observation(
        case_id=STALE_CLAIM_PAUSED_FOR_RECONCILE,
        engine_commit="c" * 40,
        items=(item(issue_labels=("in-progress", "io:needs-reconcile")),),
        tech_lead_runs=(),
        events=[],
        owned=frozenset({901}),
        gh_audit_report=report,
        elapsed_seconds=90.0,
        ended_by=ended_by,
        notes=(),
    )


def test_an_engine_that_died_mid_run_still_yields_a_failing_scorecard() -> None:
    """Round 7 F2: no audit report because the engine is gone — the
    observation says so explicitly and the case fails, instead of the run
    producing no scorecard at all."""
    import json

    from issue_orchestrator.testing.exam import ExamObservation, RunEnd, grade, render_summary
    from issue_orchestrator.testing.exam.cases import stale_claim_paused_for_reconcile

    observation = _exit_observation(RunEnd.ENGINE_EXITED, None)
    assert observation.github_calls is None
    assert ExamObservation.from_dict(json.loads(json.dumps(observation.to_dict()))) == observation

    card = grade(stale_claim_paused_for_reconcile(needs_reconcile_label="io:needs-reconcile"), observation)
    assert card.failures[0] == "engine exited before the case finished"
    assert json.loads(card.to_json())["github_calls"] is None
    assert "github calls: unavailable (the engine exited" in render_summary(card)


def test_a_missing_audit_report_from_a_live_engine_is_refused() -> None:
    from issue_orchestrator.testing.exam import RunEnd

    with pytest.raises(RuntimeError, match="no gh_audit report although it did not exit"):
        _exit_observation(RunEnd.WINDOW_ELAPSED, None)



def test_a_closed_pr_whose_branch_survived_is_cleaned_up(monkeypatch) -> None:
    """Round 8 F3: a closed (e.g. partially reset) PR's branch is deleted too."""
    from types import SimpleNamespace

    from tests.e2e.exam import cleanup

    adapter = _StrictFake(delete_works=True)
    adapter.pr_state = {70: "closed"}
    adapter.branches = {"7-exam"}
    monkeypatch.setattr(cleanup, "_github_adapter", lambda _repo: adapter)
    monkeypatch.setattr(
        cleanup, "linked_pull_requests",
        lambda _repo, number, state: [SimpleNamespace(number=70)] if (number, state) == (7, "all") else [],
    )

    cleanup.teardown_run("o/r", "io:e2e:exam-a-x", [7])

    assert adapter.closed == [] and adapter.deleted == ["7-exam"] and adapter.branches == set()



def test_the_engine_clone_pushes_to_the_runs_repository(monkeypatch, tmp_path) -> None:
    """Round 9 F1: with E2E_TEST_REPO pointing at a fork, the engine clone's
    origin is the fork — not whatever the harness checkout's origin is."""
    from tests.e2e.exam import engine_checkout

    harness = tmp_path / "harness"
    (harness / ".venv").mkdir(parents=True)
    parent = tmp_path / "engines"
    parent.mkdir()
    calls: list[tuple[str, ...]] = []

    def fake_git(cwd, *argv):
        calls.append(argv)
        return "b" * 40 if argv[0] == "rev-parse" else ""

    monkeypatch.setattr(engine_checkout, "_git", fake_git)
    checkout = engine_checkout.EngineCheckout.create(
        harness_root=harness, ref="HEAD", identity=RunIdentity.new("A"), repo="me/fork", parent=parent
    )

    assert ("remote", "set-url", "origin", "https://github.com/me/fork.git") in calls
    assert not any(argv[:2] == ("remote", "get-url") for argv in calls)
    checkout.remove()


@pytest.mark.parametrize("bad", ["", "owner", "owner/", "/name", "a/b/c"])
def test_a_malformed_repository_is_refused(bad: str) -> None:
    from tests.e2e.exam.run_identity import github_remote

    with pytest.raises(ValueError, match="owner/name"):
        github_remote(bad)
