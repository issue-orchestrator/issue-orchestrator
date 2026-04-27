"""Unit tests for Control API bearer-token resolution.

See security issue #5987 (F3). The token module generates and persists
the shared secret used by the Control API middleware; these tests cover
file creation, permission enforcement, env-var override, and
constant-time comparison.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading

import pytest

from issue_orchestrator.infra.api_token import (
    AGENT_CALLBACK_TOKEN_ENV_VAR,
    TOKEN_ENV_VAR,
    generate_token,
    load_or_create_token,
    read_existing_admin_token,
    read_existing_agent_callback_token,
    read_existing_token,
    resolve_agent_callback_token,
    resolve_api_token,
    verify_token,
)


class TestTokenGeneration:
    def test_generate_token_returns_hex_string(self) -> None:
        token = generate_token()
        assert isinstance(token, str)
        assert len(token) == 64  # 32 bytes hex-encoded
        int(token, 16)  # must be valid hex

    def test_generate_token_is_random(self) -> None:
        assert generate_token() != generate_token()


class TestLoadOrCreate:
    def test_creates_file_with_mode_0600(self, tmp_path: Path) -> None:
        target = tmp_path / ".issue-orchestrator" / "api-token"
        token = load_or_create_token(target)

        assert target.exists()
        assert target.read_text().strip() == token
        assert target.stat().st_mode & 0o777 == 0o600

    def test_reuses_existing_token(self, tmp_path: Path) -> None:
        target = tmp_path / ".issue-orchestrator" / "api-token"
        target.parent.mkdir(parents=True)
        target.write_text("pre-existing-token\n")
        target.chmod(0o600)

        token = load_or_create_token(target)

        assert token == "pre-existing-token"

    def test_tightens_loose_permissions(self, tmp_path: Path) -> None:
        target = tmp_path / ".issue-orchestrator" / "api-token"
        target.parent.mkdir(parents=True)
        target.write_text("loose-token\n")
        target.chmod(0o644)

        token = load_or_create_token(target)

        assert token == "loose-token"
        assert target.stat().st_mode & 0o777 == 0o600

    def test_regenerates_empty_file(self, tmp_path: Path) -> None:
        target = tmp_path / ".issue-orchestrator" / "api-token"
        target.parent.mkdir(parents=True)
        target.write_text("")
        target.chmod(0o600)

        token = load_or_create_token(target)

        assert token
        assert target.read_text().strip() == token

    def test_concurrent_first_start_returns_single_token(self, tmp_path: Path) -> None:
        target = tmp_path / ".issue-orchestrator" / "agent-callback-token"
        workers = 24
        ready = threading.Barrier(workers)

        def load() -> str:
            ready.wait(timeout=5)
            return load_or_create_token(target)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            tokens = list(pool.map(lambda _: load(), range(workers)))

        assert len(set(tokens)) == 1
        assert target.read_text().strip() == tokens[0]
        assert target.stat().st_mode & 0o777 == 0o600
        assert list(target.parent.glob("*.tmp")) == []


class TestResolve:
    def test_env_var_takes_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "api-token"
        target.write_text("file-token")
        target.chmod(0o600)
        monkeypatch.setenv(TOKEN_ENV_VAR, "env-token")

        assert resolve_api_token(target) == "env-token"

    def test_falls_back_to_file_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "api-token"
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

        token = resolve_api_token(target)

        assert token
        assert target.read_text().strip() == token


class TestVerifyToken:
    def test_accepts_match(self) -> None:
        assert verify_token("correct-token", "correct-token") is True

    def test_rejects_mismatch(self) -> None:
        assert verify_token("correct-token", "wrong-token") is False

    def test_rejects_none(self) -> None:
        assert verify_token("correct-token", None) is False

    def test_rejects_empty_string(self) -> None:
        assert verify_token("correct-token", "") is False

    def test_rejects_different_length(self) -> None:
        assert verify_token("short", "much-longer-value") is False


class TestReadExistingToken:
    """Tests for the non-creating token-file readers (#6017 review P3)."""

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent-token"
        assert read_existing_token(missing) is None

    def test_reads_existing_admin_file(self, tmp_path: Path) -> None:
        target = tmp_path / "api-token"
        target.write_text("on-disk-token")
        target.chmod(0o600)
        assert read_existing_token(target) == "on-disk-token"

    def test_admin_helper_prefers_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TOKEN_ENV_VAR, "from-env")

        assert read_existing_admin_token() == "from-env"

    def test_admin_helper_returns_none_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        # No file in tmp_path/.issue-orchestrator/api-token.
        assert read_existing_admin_token() is None


class TestAgentCallbackToken:
    """Scoped callback token surface (security #6017 review P2)."""

    def test_resolve_creates_separate_file(self, tmp_path: Path) -> None:
        path = tmp_path / "agent-callback-token"
        token = resolve_agent_callback_token(path)

        assert token
        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600

    def test_resolve_prefers_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "agent-callback-token"
        path.write_text("file-value")
        path.chmod(0o600)
        monkeypatch.setenv(AGENT_CALLBACK_TOKEN_ENV_VAR, "env-value")

        assert resolve_agent_callback_token(path) == "env-value"

    def test_read_existing_returns_none_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(AGENT_CALLBACK_TOKEN_ENV_VAR, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        assert read_existing_agent_callback_token() is None

    def test_admin_and_agent_tokens_are_independent(self) -> None:
        """Different env vars — writing one must not leak into the other."""
        assert TOKEN_ENV_VAR != AGENT_CALLBACK_TOKEN_ENV_VAR
