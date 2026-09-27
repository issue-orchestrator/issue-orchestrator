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
