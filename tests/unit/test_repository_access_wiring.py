"""Every path that reads the issue->branch map authenticates its ``ls-remote``.

The map is read from the remote (#9550), so it is a network read. On a private
repository served by a GitHub App, Git transport authenticates only with the
installation token the configured auth owner mints. These tests configure App
auth, run each public path, and observe the one ``ls-remote`` it makes at the
subprocess boundary: the credential rides the environment and never argv
(#9550 review r1 F1, F2).
"""

from __future__ import annotations

import argparse
from base64 import b64encode
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from issue_orchestrator.adapters.github.auth import GitHubAppInstallationTokenProvider
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.repository_launch_selection import RepositoryLaunchSelection
from issue_orchestrator.entrypoints.bootstrap_run_services import create_io_adapters
from issue_orchestrator.entrypoints.cli_dry_run import run_dry_run
from issue_orchestrator.entrypoints.cli_queue_commands import cmd_audit
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.control_center_actions import (
    AuditActionRequest,
    AuditIssuesCommand,
)
from issue_orchestrator.execution.providers import (
    create_github_auth,
    create_repository_access,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.command_runner import CommandResult

TOKEN = "installation-token-not-live"
AUTH_HEADER = "Authorization: Basic " + b64encode(f"x-access-token:{TOKEN}".encode()).decode()


@dataclass
class GitTransport:
    """The subprocess boundary every Git command crosses: it records each one
    and answers ``ls-remote`` as a remote holding ``42-fix`` would."""

    calls: list[tuple[list[str], dict[str, str] | None]] = field(default_factory=list)

    def run(self, command: list[str], **kwargs: object) -> CommandResult:
        argv = list(command)
        assert argv[0] == "git" and argv[3:5] == ["ls-remote", "--heads"], argv
        env = kwargs.get("env")
        assert env is None or isinstance(env, dict)
        self.calls.append((argv, env))
        return CommandResult(returncode=0, stdout="abc123\trefs/heads/42-fix\n", stderr="")

    def assert_one_authenticated_read(self) -> None:
        assert len(self.calls) == 1, self.calls
        argv, env = self.calls[0]
        assert env is not None, "the read used ambient Git credentials"
        assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
        assert env["GIT_CONFIG_VALUE_0"] == AUTH_HEADER
        rendered = " ".join(argv)
        assert TOKEN not in rendered and AUTH_HEADER not in rendered


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> GitTransport:
    recorder = GitTransport()

    def run(_runner: LocalCommandRunner, command: list[str], **kwargs: object) -> CommandResult:
        return recorder.run(command, **kwargs)

    monkeypatch.setattr(LocalCommandRunner, "run", run)
    return recorder


@pytest.fixture
def app_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """A repository configured for GitHub App auth; minting is the only fake."""
    key = tmp_path / "bot.private-key.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n")
    monkeypatch.setattr(GitHubAppInstallationTokenProvider, "get_token", lambda _self: TOKEN)
    config = Config()
    config.repo = "owner/repo"
    config.repo_root = tmp_path
    config.github_app_client_id = "Iv23example"
    config.github_app_installation_id = "145305179"
    config.github_app_private_key_path = str(key)
    config.agents = {"agent:backend": AgentConfig(prompt_path=tmp_path / "prompt.md")}
    return config


def test_repository_access_builds_one_auth_owner_for_host_and_working_copy(
    app_config: Config, transport: GitTransport
) -> None:
    from issue_orchestrator.adapters import github as github_adapters

    real_build = github_adapters.build_github_auth
    with patch.object(github_adapters, "build_github_auth", side_effect=real_build) as build:
        access = create_repository_access("owner/repo", app_config)

    build.assert_called_once()
    assert build.call_args.kwargs["configured_app_installation_id"] == "145305179"
    assert build.call_args.kwargs["repo"] == "owner/repo"
    assert access.host.auth_kind == "github_app"
    assert access.working_copy.list_remote_branches(app_config.repo_root) == ["42-fix"]
    transport.assert_one_authenticated_read()


def test_the_engines_working_copy_reads_with_the_configured_auth(
    app_config: Config, transport: GitTransport
) -> None:
    _, working_copy, _, _ = create_io_adapters(
        app_config.repo_root, create_github_auth("owner/repo", app_config)
    )

    assert working_copy.list_remote_branches(app_config.repo_root) == ["42-fix"]
    transport.assert_one_authenticated_read()


def test_cli_audit_reads_the_branch_map_with_the_configured_auth(
    app_config: Config, transport: GitTransport
) -> None:
    with (
        patch("issue_orchestrator.entrypoints.cli_support.load_config", return_value=app_config),
        patch("issue_orchestrator.infra.audit.audit_queue", return_value=[]) as audit,
        patch(
            "issue_orchestrator.entrypoints.cli_queue_commands._stored_known_proposals",
            return_value=frozenset(),
        ),
    ):
        assert cmd_audit(argparse.Namespace()) == 0

    transport.assert_one_authenticated_read()
    assert audit.call_args.kwargs["issue_branches"] == {42: "42-fix"}
    assert audit.call_args.kwargs["issue_tracker"].auth_kind == "github_app"


def test_dry_run_reads_the_branch_map_once_with_the_configured_auth(
    app_config: Config, transport: GitTransport
) -> None:
    with (
        patch(
            "issue_orchestrator.adapters.github.GitHubAdapter.list_issues",
            return_value=[Mock()],
        ),
        patch("issue_orchestrator.infra.analysis.analyze_all_issues", return_value=[]) as analyze,
        patch(
            "issue_orchestrator.infra.analysis.analyze_orphan_branches", return_value=[]
        ) as orphans,
        patch("issue_orchestrator.entrypoints.cli_dry_run._print_dry_run_summary"),
    ):
        assert run_dry_run(argparse.Namespace(), app_config) == 0

    transport.assert_one_authenticated_read()
    assert analyze.call_args.kwargs["issue_branches"] == {42: "42-fix"}
    assert orphans.call_args.args[0] == {42: "42-fix"}


@pytest.mark.asyncio
async def test_control_center_audit_reads_the_branch_map_with_the_configured_auth(
    app_config: Config, transport: GitTransport
) -> None:
    with (
        patch(
            "issue_orchestrator.execution.control_center_runtime.load_config_for_selection",
            return_value=app_config,
        ),
        patch("issue_orchestrator.infra.audit.audit_queue", return_value=[]) as audit,
        patch(
            "issue_orchestrator.execution.control_center_actions._stored_known_proposals",
            return_value=frozenset(),
        ),
    ):
        result = await AuditIssuesCommand().execute(
            AuditActionRequest(
                repo_root=app_config.repo_root,
                selection=RepositoryLaunchSelection.default(),
            ),
        )

    assert result.status_code == 200, result.payload
    transport.assert_one_authenticated_read()
    assert audit.call_args.kwargs["issue_branches"] == {42: "42-fix"}
    assert audit.call_args.kwargs["issue_tracker"].auth_kind == "github_app"
