import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from issue_orchestrator.infra.config import AgentConfig, Config
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
        config_path = process._write_e2e_config()  # noqa: SLF001

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
        first_path = first._write_e2e_config()  # noqa: SLF001
        second_path = second._write_e2e_config()  # noqa: SLF001

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
    captured: dict[str, object] = {}

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
