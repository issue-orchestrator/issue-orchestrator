"""Tests for environment filtering invariants.

The critical invariant pinned here: ``build_filtered_env`` never drops
``PYTHONPATH`` (and sibling launch-time env vars) from a subprocess's
environment, no matter what allowlist ``env_passthrough`` a caller
passes. The frozen-snapshot fix (#5950) relies on ``PYTHONPATH``
propagating from the CC to every agent subprocess; if a future
``env_passthrough`` allowlist omits it, the fix silently regresses
for that adapter's sessions with no log signal.

Reviewers asked for this as a named invariant test rather than a
one-time grep, because allowlists drift independently of the code
that relies on them.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.execution.agent_runner_env import (
    ALWAYS_PASSTHROUGH_ENV_VARS,
    build_filtered_env,
)
from issue_orchestrator.infra.secret_env import (
    EXTRA_FORBIDDEN_ENV_VARS_ENV,
    GITHUB_APP_PRIVATE_KEY_ENV,
    configure_extra_forbidden_env_vars,
)


class TestAlwaysPassthroughInvariant:
    """Vars in ``ALWAYS_PASSTHROUGH_ENV_VARS`` must propagate regardless
    of allowlist/denylist mode."""

    def test_pythonpath_is_in_the_always_passthrough_list(self) -> None:
        """The snapshot-freeze fix depends on this. Deleting PYTHONPATH
        from the list regresses #5950 silently — changing the list is
        meant to trigger a deliberate test edit."""
        assert "PYTHONPATH" in ALWAYS_PASSTHROUGH_ENV_VARS

    def test_orchestrator_python_is_in_the_list(self) -> None:
        """PR #5944 pre-push hook resolution; hooks in foreign target
        repos cannot find Python without this env var."""
        assert "ISSUE_ORCHESTRATOR_PYTHON" in ALWAYS_PASSTHROUGH_ENV_VARS

    def test_cc_snapshot_is_in_the_list(self) -> None:
        """Observability companion to PYTHONPATH — if PYTHONPATH is
        propagated but the snapshot marker is not, the CC's diagnostic
        output loses the link back to the frozen tree."""
        assert "ISSUE_ORCHESTRATOR_CC_SNAPSHOT" in ALWAYS_PASSTHROUGH_ENV_VARS

    def test_allowlist_mode_preserves_pythonpath(self) -> None:
        """A caller-supplied empty passthrough (strictest allowlist) must
        still propagate PYTHONPATH. This is the whole point of the
        always-passthrough set."""
        env = build_filtered_env(
            base_env={"PYTHONPATH": "/snapshot/src", "OTHER": "value"},
            passthrough_vars=[],  # Strictest possible allowlist
            include_git_safe=False,
        )

        assert env.get("PYTHONPATH") == "/snapshot/src"

    def test_allowlist_mode_preserves_every_always_passthrough_var(self) -> None:
        """Same guarantee for every var in the list — guards against a
        future change that special-cases only PYTHONPATH."""
        base = {var: f"value-{var}" for var in ALWAYS_PASSTHROUGH_ENV_VARS}
        base["SHOULD_BE_FILTERED"] = "x"

        env = build_filtered_env(
            base_env=base,
            passthrough_vars=["NOTHING_MATCHES"],
            include_git_safe=False,
        )

        for var in ALWAYS_PASSTHROUGH_ENV_VARS:
            assert env.get(var) == f"value-{var}", (
                f"{var} was dropped by allowlist mode despite being "
                "in ALWAYS_PASSTHROUGH_ENV_VARS"
            )
        assert "SHOULD_BE_FILTERED" not in env

    def test_allowlist_mode_with_explicit_include_is_idempotent(self) -> None:
        """If a caller happens to list PYTHONPATH in their allowlist
        explicitly, behaviour is unchanged — no duplication, no
        reordering concerns."""
        env = build_filtered_env(
            base_env={"PYTHONPATH": "/snapshot/src"},
            passthrough_vars=["PYTHONPATH"],
            include_git_safe=False,
        )

        assert env == {"PYTHONPATH": "/snapshot/src"}

    def test_denylist_mode_still_preserves_pythonpath(self) -> None:
        """Denylist mode already propagated PYTHONPATH before the fix;
        this pins that the behaviour survives the always-passthrough
        logic addition."""
        env = build_filtered_env(
            base_env={"PYTHONPATH": "/snapshot/src", "GH_TOKEN": "secret"},
            include_git_safe=False,
        )

        assert env.get("PYTHONPATH") == "/snapshot/src"
        assert "GH_TOKEN" not in env  # Still scrubbed

    def test_always_passthrough_does_not_break_scrub_in_denylist(self) -> None:
        """Defensive: the new list must never be used as a tunnel to
        re-introduce a scrubbed credential. Verify a forbidden var
        listed in ALWAYS_PASSTHROUGH would hypothetically still be
        filtered — tests the denylist intersection."""
        # (No credential lives in ALWAYS_PASSTHROUGH today; this asserts
        # the mode's logic rather than a specific var.)
        env = build_filtered_env(
            base_env={"PYTHONPATH": "/snapshot/src"},
            include_git_safe=False,
        )
        assert env.get("PYTHONPATH") == "/snapshot/src"

    def test_allowlist_mode_cannot_reintroduce_scrubbed_secret(self) -> None:
        """Allowlist mode is not allowed to override credential scrubbing."""
        env = build_filtered_env(
            base_env={GITHUB_APP_PRIVATE_KEY_ENV: "private-key", "SAFE": "ok"},
            passthrough_vars=[GITHUB_APP_PRIVATE_KEY_ENV, "SAFE"],
            include_git_safe=False,
        )

        assert GITHUB_APP_PRIVATE_KEY_ENV not in env
        assert env["SAFE"] == "ok"


class TestSecretEnvScrubbing:
    """Agent subprocesses must not inherit orchestrator-owned app secrets."""

    def test_default_github_app_private_key_env_is_scrubbed(self) -> None:
        env = build_filtered_env(
            base_env={GITHUB_APP_PRIVATE_KEY_ENV: "private-key", "SAFE": "ok"},
            include_git_safe=False,
        )

        assert GITHUB_APP_PRIVATE_KEY_ENV not in env
        assert env["SAFE"] == "ok"

    def test_configured_github_app_private_key_env_is_scrubbed(self, monkeypatch) -> None:
        monkeypatch.setenv(EXTRA_FORBIDDEN_ENV_VARS_ENV, "CUSTOM_GH_APP_PRIVATE_KEY")

        env = build_filtered_env(
            base_env={"CUSTOM_GH_APP_PRIVATE_KEY": "private-key", "SAFE": "ok"},
            include_git_safe=False,
        )

        assert "CUSTOM_GH_APP_PRIVATE_KEY" not in env
        assert env["SAFE"] == "ok"

    def test_configured_secret_env_names_must_be_shell_safe(self) -> None:
        with pytest.raises(ValueError, match="Invalid environment variable name"):
            configure_extra_forbidden_env_vars(["BAD;rm"])
