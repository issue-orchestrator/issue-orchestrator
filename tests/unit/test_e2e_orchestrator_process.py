import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from issue_orchestrator.domain.repository_launch_selection import RepositoryLaunchSelection
from issue_orchestrator.infra.config import AgentConfig, Config
from issue_orchestrator.infra.repo_registry import list_repos, record_launched_selection
from tests.e2e.fixtures.orchestrator_process import OrchestratorProcess
from tests.e2e.fixtures.process_group_owner import ProcessGroupOwner


def test_write_e2e_config_preserves_agent_prompt_contract(tmp_path: Path) -> None:
    config = Config(
        repo="BruceBGordon/issue-orchestrator",
        repo_root=tmp_path,
        worktree_base=tmp_path / "worktrees",
        github_token_env="GH_TOKEN",
    )
    config.agents = {
        "agent:backend": AgentConfig(
            prompt_path=tmp_path / "repo-specific" / "prompts" / "simple-fix.md",
            provider="claude-code",
            model="opus",
            timeout_minutes=75,
            initial_prompt="Work on issue #{issue_number}: stay focused and use --follow-up-file /tmp/follow-up-issues.jsonl.",
            provider_args={"permission_mode": "bypassPermissions", "verbose": "true"},
            retry_prompt_template="repo-specific/prompts/retry.md",
        )
    }

    process = OrchestratorProcess(config, tmp_path)
    try:
        config_path = process.write_e2e_config()

        payload = yaml.safe_load(config_path.read_text())
        agent_payload = payload["agents"]["agent:backend"]

        assert agent_payload["provider"] == "claude-code"
        assert agent_payload["model"] == "opus"
        assert agent_payload["timeout_minutes"] == 75
        assert (
            agent_payload["initial_prompt"]
            == config.agents["agent:backend"].initial_prompt
        )
        assert agent_payload["provider_args"] == {
            "permission_mode": "bypassPermissions",
            "verbose": "true",
        }
        assert agent_payload["retry_prompt_template"] == "repo-specific/prompts/retry.md"
    finally:
        process._close_log_file()  # noqa: SLF001


def test_write_e2e_config_uses_unique_path_per_process(tmp_path: Path) -> None:
    config = Config(
        repo="BruceBGordon/issue-orchestrator",
        repo_root=tmp_path,
        worktree_base=tmp_path / "worktrees",
        github_token_env="GH_TOKEN",
    )
    config.claims.enabled = True
    config.claims.claimant_id = "orchestrator-a"

    first = OrchestratorProcess(config, tmp_path)
    second = OrchestratorProcess(config, tmp_path)

    try:
        first_path = first.write_e2e_config()
        second_path = second.write_e2e_config()

        assert first_path != second_path
        assert first_path.parent != second_path.parent
        assert first_path.parent.name.startswith("e2e-orchestrator-config-")
        assert second_path.parent.name.startswith("e2e-orchestrator-config-")
    finally:
        first._close_log_file()  # noqa: SLF001
        second._close_log_file()  # noqa: SLF001


def test_start_runs_source_from_separate_repo_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo-root"
    source_root = tmp_path / "source-root"
    repo_root.mkdir()
    source_root.mkdir()
    executable = source_root / ".venv" / "bin" / "issue-orchestrator"
    executable.parent.mkdir(parents=True)
    executable.touch()
    config = Config(
        repo="BruceBGordon/issue-orchestrator",
        repo_root=repo_root,
        worktree_base=tmp_path / "worktrees",
        github_token_env="GH_TOKEN",
    )

    process = OrchestratorProcess(config, repo_root, source_root=source_root)
    captured = _capture_engine_launch(monkeypatch, tmp_path)

    try:
        process.start(max_issues=1)
    finally:
        process._close_log_file()  # noqa: SLF001

    cmd = captured["cmd"]
    env = captured["env"]
    assert isinstance(cmd, list)
    assert isinstance(env, dict)

    assert cmd[0] == str(executable)
    assert captured["cwd"] == repo_root
    assert captured["start_new_session"] is True
    assert str(source_root / "src") == env["PYTHONPATH"].split(":", maxsplit=1)[0]


@pytest.mark.parametrize("overridden", [False, True])
def test_an_engine_registers_its_checkout_in_a_registry_of_its_own(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overridden: bool,
) -> None:
    """get_test_repo refuses every repository the operator's io registry
    names, and the exam and onboarding engines run from clones of the test
    repository, so no e2e engine registers there (#9529)."""
    operator_registry = tmp_path / "operator-io-config"
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_CONFIG_DIR", str(operator_registry))
    test_repo_clone = tmp_path / "exam-engine-clone"
    test_repo_clone.mkdir()
    config = Config(
        repo="someone/io-e2e-sandbox",
        repo_root=test_repo_clone,
        worktree_base=tmp_path / "worktrees",
        github_token_env="GH_TOKEN",
    )
    overrides = {"ISSUE_ORCHESTRATOR_CONFIG_DIR": str(operator_registry)} if overridden else {}
    process = OrchestratorProcess(config, test_repo_clone, env_overrides=overrides)
    captured = _capture_engine_launch(monkeypatch, tmp_path)

    try:
        process.start(max_issues=1)
        env = captured["env"]
        assert isinstance(env, dict)
        engine_registry = Path(env["ISSUE_ORCHESTRATOR_CONFIG_DIR"])
        assert engine_registry.is_dir()

        # What the engine does as it starts: register the checkout it runs from.
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_CONFIG_DIR", str(engine_registry))
        record_launched_selection(test_repo_clone, RepositoryLaunchSelection.default())
        assert [repo.path for repo in list_repos()] == [str(test_repo_clone.resolve())]
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_CONFIG_DIR", str(operator_registry))

        assert list_repos() == []
    finally:
        process._close_log_file()  # noqa: SLF001

    assert not engine_registry.exists()


def _capture_engine_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, object]:
    """Replace the engine launch: return what ``start`` would run it with.

    Its logs go to ``tmp_path``, not the machine-wide e2e log directory that
    every concurrent run prunes.
    """
    captured: dict[str, object] = {}
    log_dir = tmp_path / "e2e-logs"
    log_dir.mkdir()
    monkeypatch.setattr("tests.e2e.fixtures.orchestrator_process.E2E_LOG_DIR", log_dir)

    class FakeProcess:
        pid = 12345
        stdout = None
        stderr = None

    def fake_popen(cmd: list[str], **kwargs: object) -> FakeProcess:
        captured["cmd"] = cmd
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(
        "tests.e2e.fixtures.orchestrator_process.subprocess.Popen",
        fake_popen,
    )
    monkeypatch.setattr(OrchestratorProcess, "wait_until_ready", lambda self: None)
    monkeypatch.setattr(OrchestratorProcess, "_log_reader", lambda self: None)
    return captured


def test_process_group_owner_snapshots_the_full_process_tree(monkeypatch) -> None:
    process_table = """\
      10   1 110
      11  10 111
      12  11 111
      13  10 113
      20   1 120
    """
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=process_table),
    )
    snapshot = ProcessGroupOwner(10, protected_pgid=999).snapshot()

    assert snapshot == frozenset({110, 111, 113})


def test_teardown_tolerates_recycled_pid_permission_error(monkeypatch) -> None:
    """A captured group whose PID was recycled (EPERM) is treated as gone.

    Once a killed group's leader exits, its PID/PGID can be reused by an
    unrelated process before teardown runs. ``os.killpg`` then reports EPERM
    instead of ESRCH, and best-effort teardown must not crash on that benign
    PID-reuse race the way it did under heavy parallel load.
    """
    owner = ProcessGroupOwner(4321, protected_pgid=999)

    def deny(_pgid: int, _signum: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", deny)

    # None of these may raise: an unsignalable group is no longer ours to reap.
    owner.signal(frozenset({2000, 2001}), signal.SIGTERM)
    owner.terminate_survivors(frozenset({2000, 2001}))


def test_crash_reaps_engine_and_isolated_agent_process_groups() -> None:
    """A hard engine crash must not leave its detached agent child alive."""
    root_script = """
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    start_new_session=True,
)
print(child.pid, flush=True)
time.sleep(60)
"""
    root = subprocess.Popen(
        [sys.executable, "-c", root_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert root.stdout is not None
    child_pid = int(root.stdout.readline().strip())
    fixture = OrchestratorProcess(Config(), Path.cwd())
    fixture.process = root

    try:
        groups = ProcessGroupOwner(root.pid).snapshot()
        assert os.getpgid(root.pid) in groups
        assert os.getpgid(child_pid) in groups

        fixture.crash()

        assert root.poll() is not None
        deadline = time.monotonic() + 5
        while _pid_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_exists(child_pid)
    finally:
        for pid in (child_pid, root.pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            root.wait(timeout=2)
        except subprocess.TimeoutExpired:
            root.kill()
            root.wait(timeout=2)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
