"""The shared fixture Git environment keeps a push on the fixture's own remote.

``tests/git_push_authorization`` authorizes fixture pushes through the agent
``git`` wrapper. An orchestrator session can also carry Git state that points
every child ``git`` somewhere else: validated-work publication runs the
publish gate with environment config setting ``remote.origin.url`` and
``pushurl`` to the real GitHub repository, and a global or system config file
can set a push URL just as well. These tests hand each redirect to the shared
environment, inherited or caller-supplied, and push for real: the push must
land on the fixture's bare remote and never on the decoy the redirect names.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.git_push_authorization import (
    GH_AUTH_ENV_VAR,
    GH_AUTH_TOKEN,
    authorized_local_fixture_git_env,
    unauthorized_git_env,
)

GIT_TIMEOUT_S = 60
# A non-Git variable a caller relies on keeping (the delete-only hook tests
# pass ISSUE_ORCHESTRATOR_PYTHON through this environment).
KEPT_VAR = "ISSUE_ORCHESTRATOR_PYTHON"
KEPT_VALUE = "/kept/python"


@dataclass(frozen=True)
class Repos:
    root: Path
    fixture: Path
    fixture_head: str
    remote: Path
    decoy: Path
    session_checkout: Path


def _bare_session_env(home: Path) -> dict[str, str]:
    """An env that inherits nothing from the session but PATH, with a HOME that
    holds no git config: the base a redirect is laid over for the control."""
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }


def _isolated_env(home: Path) -> dict[str, str]:
    """Setup and inspection env: no session state and no global or system config."""
    return {
        **_bare_session_env(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }


def _git(cwd: Path, env: Mapping[str, str], *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed:\n{result.stderr}"
    return result.stdout


def _heads(bare: Path) -> dict[str, str]:
    listing = _git(
        bare,
        _isolated_env(bare.parent),
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/heads",
    )
    return dict(line.split(" ", 1) for line in listing.splitlines())


def _repo_with_one_commit(path: Path, env: Mapping[str, str], origin: Path) -> str:
    path.mkdir()
    _git(path, env, "init", "-b", "main")
    _git(path, env, "remote", "add", "origin", str(origin))
    (path / "file").write_text(f"{path.name}\n")
    _git(path, env, "add", "file")
    _git(path, env, "commit", "-m", path.name)
    return _git(path, env, "rev-parse", "HEAD").strip()


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    """A fixture repo whose origin is a bare remote under ``tmp_path``, a decoy
    bare remote, and a stand-in session checkout whose origin is the decoy."""
    env = _isolated_env(tmp_path)
    remote = tmp_path / "fixture-remote.git"
    decoy = tmp_path / "decoy.git"
    for bare in (remote, decoy):
        _git(tmp_path, env, "init", "--bare", "-b", "main", str(bare))
    fixture = tmp_path / "fixture"
    fixture_head = _repo_with_one_commit(fixture, env, remote)
    session_checkout = tmp_path / "session-checkout"
    _repo_with_one_commit(session_checkout, env, decoy)
    return Repos(tmp_path, fixture, fixture_head, remote, decoy, session_checkout)


def _environment_config(repos: Repos) -> dict[str, str]:
    """The shape validated-work publication gives its publish gate."""
    return {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_0": "http.https://github.invalid/.extraheader",
        "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic not-a-real-token",
        "GIT_CONFIG_KEY_1": "remote.origin.url",
        "GIT_CONFIG_VALUE_1": str(repos.decoy),
        "GIT_CONFIG_KEY_2": "remote.origin.pushurl",
        "GIT_CONFIG_VALUE_2": str(repos.decoy),
    }


def _command_line_config(repos: Repos) -> dict[str, str]:
    """What ``git -c remote.origin.pushurl=...`` hands to every child git."""
    return {"GIT_CONFIG_PARAMETERS": f"'remote.origin.pushurl'='{repos.decoy}'"}


def _repository_selection(repos: Repos) -> dict[str, str]:
    """What git exports to a hook running in another checkout."""
    return {
        "GIT_DIR": str(repos.session_checkout / ".git"),
        "GIT_WORK_TREE": str(repos.session_checkout),
    }


def _push_url_config_file(path: Path, repos: Repos) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[remote "origin"]\n\tpushurl = {repos.decoy}\n')
    return path


def _global_config(repos: Repos) -> dict[str, str]:
    """GIT_CONFIG_GLOBAL names a config file that sets a push URL."""
    hostile = _push_url_config_file(repos.root / "hostile-global.gitconfig", repos)
    return {"GIT_CONFIG_GLOBAL": str(hostile)}


def _system_config(repos: Repos) -> dict[str, str]:
    """GIT_CONFIG_SYSTEM names a config file that sets a push URL."""
    hostile = _push_url_config_file(repos.root / "hostile-system.gitconfig", repos)
    return {"GIT_CONFIG_SYSTEM": str(hostile)}


def _home_config(repos: Repos) -> dict[str, str]:
    """Without GIT_CONFIG_GLOBAL, git reads the global level from $HOME."""
    home = repos.root / "hostile-home"
    _push_url_config_file(home / ".gitconfig", repos)
    return {"HOME": str(home)}


def _xdg_config(repos: Repos) -> dict[str, str]:
    """... and from $XDG_CONFIG_HOME/git/config."""
    xdg = repos.root / "hostile-xdg"
    _push_url_config_file(xdg / "git" / "config", repos)
    return {"XDG_CONFIG_HOME": str(xdg)}


Redirect = Callable[[Repos], dict[str, str]]

REDIRECTS = [
    pytest.param(_environment_config, id="environment-config"),
    pytest.param(_command_line_config, id="command-line-config"),
    pytest.param(_repository_selection, id="repository-selection"),
    pytest.param(_global_config, id="global-config-file"),
    pytest.param(_system_config, id="system-config-file"),
    pytest.param(_home_config, id="home-gitconfig"),
    pytest.param(_xdg_config, id="xdg-gitconfig"),
]


@pytest.mark.parametrize("redirect", REDIRECTS)
def test_an_unprotected_push_follows_the_redirect(
    repos: Repos, redirect: Redirect
) -> None:
    """Control: each redirect really does move a fixture push to the decoy, so
    the protected test below cannot pass because the redirect was inert."""
    env = {
        **_bare_session_env(repos.root),
        **redirect(repos),
        GH_AUTH_ENV_VAR: GH_AUTH_TOKEN,
    }

    _git(repos.fixture, env, "push", "origin", "main")

    assert _heads(repos.remote) == {}
    assert _heads(repos.decoy) != {}


def _inherited(
    monkeypatch: pytest.MonkeyPatch, redirect: dict[str, str]
) -> dict[str, str]:
    for name, value in {**redirect, KEPT_VAR: KEPT_VALUE}.items():
        monkeypatch.setenv(name, value)
    return authorized_local_fixture_git_env()


def _caller_supplied(
    _monkeypatch: pytest.MonkeyPatch, redirect: dict[str, str]
) -> dict[str, str]:
    return authorized_local_fixture_git_env(
        {**os.environ, **redirect, KEPT_VAR: KEPT_VALUE}
    )


@pytest.mark.parametrize(
    "fixture_env",
    [
        pytest.param(_inherited, id="inherited"),
        pytest.param(_caller_supplied, id="caller-supplied"),
    ],
)
@pytest.mark.parametrize("redirect", REDIRECTS)
def test_a_fixture_push_lands_only_on_the_fixture_remote(
    repos: Repos,
    redirect: Redirect,
    fixture_env: Callable[[pytest.MonkeyPatch, dict[str, str]], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = fixture_env(monkeypatch, redirect(repos))

    _git(repos.fixture, env, "push", "origin", "main")

    assert _heads(repos.remote) == {"refs/heads/main": repos.fixture_head}
    assert _heads(repos.decoy) == {}
    assert env[KEPT_VAR] == KEPT_VALUE
    assert env[GH_AUTH_ENV_VAR] == GH_AUTH_TOKEN


@pytest.mark.parametrize(
    "name",
    [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_11",
        "GIT_CONFIG_VALUE_11",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
    ],
)
def test_every_redirecting_git_variable_is_shut_out(name: str) -> None:
    env = authorized_local_fixture_git_env({name: "/elsewhere", KEPT_VAR: KEPT_VALUE})

    assert env == {
        KEPT_VAR: KEPT_VALUE,
        GH_AUTH_ENV_VAR: GH_AUTH_TOKEN,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }


def test_the_negative_control_drops_the_same_redirects(repos: Repos) -> None:
    base = {
        **os.environ,
        **_environment_config(repos),
        **_command_line_config(repos),
        **_repository_selection(repos),
        **_global_config(repos),
        **_system_config(repos),
        **_home_config(repos),
        **_xdg_config(repos),
        KEPT_VAR: KEPT_VALUE,
        GH_AUTH_ENV_VAR: GH_AUTH_TOKEN,
    }
    authorized = authorized_local_fixture_git_env(base)

    assert unauthorized_git_env(base) == {
        name: value for name, value in authorized.items() if name != GH_AUTH_ENV_VAR
    }
