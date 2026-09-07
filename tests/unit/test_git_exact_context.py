"""Effective auth and history validation use real Git reads; network writes are inert."""

import pytest

from issue_orchestrator.adapters.git.git_cli import GitCLI
from issue_orchestrator.adapters.github.auth import (
    GitHubAuth,
    StaticGitHubTokenProvider,
)
from issue_orchestrator.domain.exact_git import ExactPushOutcome
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.ports.git import GitResult
from .test_git_exact_operations import rig as rig


class RecordingPushGit(GitCLI):
    def __init__(self, runner):
        super().__init__(runner=runner)
        self.calls = []

    def run(self, repo, argv, **kwargs):
        self.calls.append((argv, kwargs.get("env")))
        if "push" in argv:
            return GitResult(argv, 0, "", "")
        assert "fetch" not in argv
        return super().run(repo, argv, **kwargs)


def auth(repo="owner/repo"):
    return GitHubAuth(
        token_provider=StaticGitHubTokenProvider(token="test-token-not-live"),
        source_descriptions=(),
        repo=repo,
        enable_git_push_auth=True,
    )


def push(rig, working, **kwargs):
    return working.push_exact(
        rig.root,
        remote="origin",
        branch="feature",
        target_sha=rig.target,
        expected_sha=rig.base,
        **kwargs,
    )


@pytest.mark.parametrize(
    "configured",
    [
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "https://github.com/owner/repo.git",
    ],
)
@pytest.mark.parametrize("bound", [False, True])
def test_real_auth_resolves_and_freezes_legitimate_transport_transition(
    rig, configured, bound
):
    rig.run("remote", "set-url", "origin", configured)
    git = RecordingPushGit(rig.git.runner)
    working = GitWorkingCopy(git=git, git_auth=auth())
    destination = working.resolve_push_destination(rig.root, remote="origin")
    assert destination.endpoint == "https://github.com/owner/repo.git"
    git.calls.clear()
    result = push(rig, working, **({"destination": destination} if bound else {}))
    assert result.outcome is ExactPushOutcome.PUSHED
    pushes = [(args, env) for args, env in git.calls if "push" in args]
    assert len(pushes) == 1
    args, env = pushes[0]
    assert args[-2:] == [destination.endpoint, f"{rig.target}:refs/heads/feature"]
    assert (
        "--atomic" in args
        and f"--force-with-lease=refs/heads/feature:{rig.base}" in args
    )
    assert "-c" not in args and "--no-verify" not in args
    assert env["GIT_CONFIG_VALUE_2"] == destination.endpoint
    for command, used_env in git.calls:
        if (
            command[0] == "check-ref-format"
            or "merge-base" in command
            or "cat-file" in command
        ):
            assert used_env == env


@pytest.mark.parametrize(
    "configured,authorized",
    [
        ("git@github.com:other/repo.git", "owner/repo"),
        ("https://github.com/owner/repo.git", "other/repo"),
        ("ssh://git@other.example/owner/repo.git", "owner/repo"),
    ],
)
def test_repository_auth_mismatch_never_pushes(rig, configured, authorized):
    rig.run("remote", "set-url", "origin", configured)
    git = RecordingPushGit(rig.git.runner)
    working = GitWorkingCopy(git=git, git_auth=auth(authorized))
    with pytest.raises(ValueError, match="repository scope"):
        push(rig, working)
    assert not any("push" in args for args, _ in git.calls)


@pytest.mark.parametrize(
    "history", ["shallow", "graft", "replacement", "rewrite", "push_rewrite"]
)
def test_unsupported_context_refuses_positive_ancestry_before_push(rig, history):
    if history == "shallow":
        (rig.root / ".git/shallow").write_text(rig.target + "\n")
    elif history == "graft":
        (rig.root / ".git/info/grafts").write_text(f"{rig.target} {rig.base}\n")
    elif history == "replacement":
        rig.run("replace", rig.target, rig.tip)
    else:
        key = "insteadOf" if history == "rewrite" else "pushInsteadOf"
        rig.run("config", f"url.{rig.remote}.{key}", "unused-prefix")
    git = RecordingPushGit(rig.git.runner)
    with pytest.raises(ValueError):
        push(rig, GitWorkingCopy(git=git))
    assert not any("push" in args for args, _ in git.calls)
    assert rig.remote_refs() == f"{rig.base} refs/heads/feature\n"


def test_effective_auth_supplied_rewrite_is_rejected(rig):
    class RewriteAuth:
        def git_env_overrides(self, *, remote):
            return {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url.elsewhere.insteadOf",
                "GIT_CONFIG_VALUE_0": "unused-prefix",
            }

    git = RecordingPushGit(rig.git.runner)
    with pytest.raises(ValueError, match="URL rewriting"):
        push(rig, GitWorkingCopy(git=git, git_auth=RewriteAuth()))
    assert not any("push" in args for args, _ in git.calls)


@pytest.mark.parametrize("variable", ["GIT_GRAFT_FILE", "GIT_SHALLOW_FILE"])
def test_history_environment_override_is_rejected(rig, monkeypatch, variable):
    monkeypatch.setenv(variable, "/dev/null")
    git = RecordingPushGit(rig.git.runner)
    with pytest.raises(ValueError, match="history environment"):
        push(rig, GitWorkingCopy(git=git))
    assert not any("push" in args for args, _ in git.calls)


def test_authentication_failure_is_typed_and_never_pushes(rig):
    class BrokenAuth:
        def git_env_overrides(self, *, remote):
            raise RuntimeError("token unavailable")

    git = RecordingPushGit(rig.git.runner)
    assert (
        push(rig, GitWorkingCopy(git=git, git_auth=BrokenAuth())).outcome
        is ExactPushOutcome.AUTH_FAILED
    )
    assert not git.calls


def test_destination_change_after_prior_resolution_is_rejected(rig, tmp_path):
    destination = rig.working.resolve_push_destination(rig.root, remote="origin")
    rig.run("remote", "set-url", "origin", str(tmp_path / "different.git"))
    git = RecordingPushGit(rig.git.runner)
    with pytest.raises(ValueError, match="destination changed"):
        push(rig, GitWorkingCopy(git=git), destination=destination)
    assert not any("push" in args for args, _ in git.calls)


def test_config_change_during_ancestry_is_rechecked_before_write(rig, tmp_path):
    class ChangingGit(RecordingPushGit):
        def run(self, repo, argv, **kwargs):
            result = super().run(repo, argv, **kwargs)
            if "merge-base" in argv:
                rig.run("remote", "set-url", "origin", str(tmp_path / "different.git"))
            return result

    git = ChangingGit(rig.git.runner)
    with pytest.raises(ValueError, match="destination changed"):
        push(rig, GitWorkingCopy(git=git))
    assert not any("push" in args for args, _ in git.calls)


def test_exact_push_keeps_real_pre_push_hook_enabled(rig):
    marker = rig.root / "hook-ran"
    hook = rig.root / ".git/hooks/pre-push"
    hook.write_text(f"#!/bin/sh\nprintf invoked > '{marker}'\nexit 1\n")
    hook.chmod(0o755)
    assert push(rig, rig.working).outcome is ExactPushOutcome.TRANSIENT
    assert marker.read_text() == "invoked"
    assert rig.remote_refs() == f"{rig.base} refs/heads/feature\n"


@pytest.mark.parametrize("binding", ["different", "encoded_space", "encoded_percent"])
def test_file_url_scope_matches_actual_git_decoding(rig, tmp_path, binding):
    literal, spaced = (
        tmp_path / "authorized%20repo.git",
        tmp_path / "authorized repo.git",
    )
    for directory in (literal, spaced):
        directory.mkdir()
        rig.git.run(directory, ["init", "--bare"])
        rig.run("push", str(directory), f"{rig.base}:refs/heads/feature")
    configured = spaced if binding == "encoded_space" else literal
    effective = (
        "file://" + str(literal) if binding == "different" else configured.as_uri()
    )
    rig.run("remote", "set-url", "origin", str(configured))

    class BoundEndpoint:
        def git_env_overrides(self, *, remote):
            return {
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": f"remote.{remote}.url",
                "GIT_CONFIG_VALUE_0": effective,
                "GIT_CONFIG_KEY_1": f"remote.{remote}.pushurl",
                "GIT_CONFIG_VALUE_1": effective,
            }

    working = GitWorkingCopy(git=rig.git, git_auth=BoundEndpoint())
    if binding == "different":
        with pytest.raises(ValueError, match="repository scope"):
            push(rig, working)
    else:
        assert push(rig, working).outcome is ExactPushOutcome.PUSHED
    for directory in (literal, spaced):
        expected = (
            rig.target
            if binding != "different" and directory == configured
            else rig.base
        )
        assert (
            rig.git.run(directory, ["rev-parse", "refs/heads/feature"]).stdout.strip()
            == expected
        )


def test_remote_helper_is_not_misclassified_as_ssh_repository(rig):
    rig.run("remote", "set-url", "origin", "ext::not-a-repository")
    git = RecordingPushGit(rig.git.runner)
    with pytest.raises(ValueError, match="remote helpers"):
        push(rig, GitWorkingCopy(git=git))
    assert not any("push" in args for args, _ in git.calls)
