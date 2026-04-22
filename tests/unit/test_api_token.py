"""Unit tests for Control API bearer-token resolution.

See security issue #5987 (F3). The token module generates and persists
the shared secret used by the Control API middleware; these tests cover
file creation, permission enforcement, env-var override, and
constant-time comparison.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from issue_orchestrator.infra.api_token import (
    TOKEN_ENV_VAR,
    generate_token,
    load_or_create_token,
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
