"""Tests for centralized env var access."""

import os

import pytest

from issue_orchestrator.infra.env import ENV_PREFIX, get_env, get_env_bool, set_env


class TestEnvPrefix:
    """Tests for ENV_PREFIX constant."""

    def test_prefix_value(self):
        assert ENV_PREFIX == "ISSUE_ORCHESTRATOR_"


class TestGetEnv:
    """Tests for get_env function."""

    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_TEST_VAR", "test_value")
        assert get_env("TEST_VAR") == "test_value"

    def test_returns_default_when_not_set(self):
        # Ensure the var doesn't exist
        os.environ.pop("ISSUE_ORCHESTRATOR_MISSING_VAR", None)
        assert get_env("MISSING_VAR") is None
        assert get_env("MISSING_VAR", "default") == "default"

    def test_returns_none_default_when_not_set(self):
        os.environ.pop("ISSUE_ORCHESTRATOR_UNSET_VAR", None)
        assert get_env("UNSET_VAR") is None


class TestSetEnv:
    """Tests for set_env function."""

    def test_sets_prefixed_var(self):
        set_env("SET_TEST", "my_value")
        assert os.environ.get("ISSUE_ORCHESTRATOR_SET_TEST") == "my_value"
        # Cleanup
        os.environ.pop("ISSUE_ORCHESTRATOR_SET_TEST", None)

    def test_overwrites_existing_value(self, monkeypatch):
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_OVERWRITE", "old")
        set_env("OVERWRITE", "new")
        assert os.environ.get("ISSUE_ORCHESTRATOR_OVERWRITE") == "new"


class TestGetEnvBool:
    """Tests for get_env_bool function."""

    @pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "Yes", "YES"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_BOOL_VAR", value)
        assert get_env_bool("BOOL_VAR") is True

    @pytest.mark.parametrize("value", ["0", "false", "False", "no", "No", ""])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_BOOL_VAR", value)
        assert get_env_bool("BOOL_VAR") is False

    def test_default_when_not_set(self):
        os.environ.pop("ISSUE_ORCHESTRATOR_BOOL_MISSING", None)
        assert get_env_bool("BOOL_MISSING") is False
        assert get_env_bool("BOOL_MISSING", default=True) is True
