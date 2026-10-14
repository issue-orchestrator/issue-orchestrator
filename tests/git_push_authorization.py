"""Owner for the privileged, test-only Git push authorization used by fixtures.

The orchestrator puts shadow ``git``/``gh`` wrappers on an agent's ``PATH``
(``src/issue_orchestrator/scripts/``). They refuse ``git push`` and the
mutating ``gh`` subcommands unless ``ORCHESTRATOR_GH_AUTH`` carries the
completion-command token — see ``docs/design/guardrails.md``.

Tests that build a throwaway local Git remote under ``tmp_path`` have to push
to it, so they need that same bypass while running inside an orchestrator
session. That bypass is security-sensitive policy, so it lives here rather
than being hand-spelled at each fixture: changing the authorization contract
means editing one module, and a new fixture cannot quietly invent its own
spelling of the token.

The local-only boundary lives here for the same reason. A fixture environment
never carries the ambient Git state that could point a fixture command at a
different repository or remote, and reads no configuration but the fixture
repository's own, so callers do not have to know where that state hides.

Use :func:`authorized_local_fixture_git_env` for the positive path and
:func:`unauthorized_git_env` for the negative control that proves the
wrappers still block.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# The env var and literal token the wrapper scripts check for. Both are
# asserted against the real wrapper sources in
# ``tests/unit/test_guardrails.py`` so this module cannot drift away from the
# scripts it authorizes against.
GH_AUTH_ENV_VAR = "ORCHESTRATOR_GH_AUTH"
GH_AUTH_TOKEN = "agent-done-authorized"

# Ambient Git state that redirects a command away from the throwaway
# repository and remote a fixture created. An orchestrator session carries it:
# validated-work publication runs the publish gate with GIT_CONFIG_COUNT and
# GIT_CONFIG_KEY_n/VALUE_n setting remote.origin.url and pushurl to the real
# GitHub repository behind an authenticated extraheader, git exports GIT_DIR
# and GIT_INDEX_FILE to hooks, and ``git -c`` hands GIT_CONFIG_PARAMETERS to
# every child git. Environment config outranks the fixture's own
# ``.git/config``, so an inherited override beats the fixture's origin.
#
# Dropping is not enough for the global and system configuration levels: a
# missing GIT_CONFIG_GLOBAL falls back to $XDG_CONFIG_HOME/git/config and
# $HOME/.gitconfig, and any of those files can set remote.origin.pushurl,
# which git pushes to instead of the fixture's remote.origin.url. Pointing
# both levels at the null device switches them off and leaves the fixture
# repository's local config the only configuration git reads.
_SWITCHED_OFF_GIT_CONFIG_LEVELS = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}
_REDIRECTING_GIT_VARS = frozenset(
    {
        # Repository selection.
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        # Environment-supplied configuration.
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    }
)
_REDIRECTING_GIT_VAR_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def _redirects_fixture_git(name: str) -> bool:
    return name in _REDIRECTING_GIT_VARS or name.startswith(
        _REDIRECTING_GIT_VAR_PREFIXES
    )


def _local_fixture_env(base: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if base is None else base
    env = {
        name: value
        for name, value in source.items()
        if not _redirects_fixture_git(name)
    }
    env.update(_SWITCHED_OFF_GIT_CONFIG_LEVELS)
    return env


def authorized_local_fixture_git_env(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment allowed to push to a local test-fixture remote.

    Args:
        base: Environment to build from. Defaults to a copy of ``os.environ``.
            Every Git repository-selection and environment-config variable in
            it is dropped and the global and system configuration levels are
            switched off, so a fixture's ``git`` acts only on the repository
            its working directory names and the remotes that repository
            configures. A fixture that commits sets its own identity in that
            repository. Everything else, such as ``PATH`` or a hook's
            ``ISSUE_ORCHESTRATOR_PYTHON``, is kept.

    The returned dict is a fresh copy, so it is safe to mutate further and
    safe under xdist (nothing here touches ``os.environ`` in place).
    """
    env = _local_fixture_env(base)
    env[GH_AUTH_ENV_VAR] = GH_AUTH_TOKEN
    return env


def unauthorized_git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a local-fixture environment with the push authorization removed.

    This is the negative control: the wrappers must still block ``git push``
    and mutating ``gh`` subcommands under this environment. It shuts out the
    same redirecting Git state as :func:`authorized_local_fixture_git_env`, so a
    wrapper regression cannot turn the control into a push to a real remote.
    """
    env = _local_fixture_env(base)
    env.pop(GH_AUTH_ENV_VAR, None)
    return env
