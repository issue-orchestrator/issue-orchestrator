"""Unit tests for ``GeminiProvider.build_command``."""

from __future__ import annotations

from issue_orchestrator.agent_runner import get_provider, is_valid_provider, list_providers
from issue_orchestrator.execution.agent_runner_providers.gemini import GeminiProvider


def _cmd(**kwargs: str) -> list[str]:
    return GeminiProvider().build_command(prompt="task", **kwargs)


class TestGeminiProviderRegistry:
    def test_gemini_is_registered_provider(self) -> None:
        assert "gemini" in list_providers()
        assert is_valid_provider("gemini") is True
        assert get_provider("gemini").name == "gemini"


class TestGeminiBaseCommand:
    def test_starts_with_gemini(self) -> None:
        assert _cmd()[0] == "gemini"

    def test_prompt_uses_prompt_flag(self) -> None:
        cmd = GeminiProvider().build_command(prompt="hello world")
        assert cmd[-2:] == ["--prompt", "hello world"]

    def test_yolo_approval_mode_is_default_for_automation(self) -> None:
        cmd = _cmd()
        assert "--approval-mode" in cmd
        assert cmd[cmd.index("--approval-mode") + 1] == "yolo"

    def test_default_approval_mode_omits_approval_flag(self) -> None:
        assert "--approval-mode" not in _cmd(approval_mode="default")

    def test_model_is_optional(self) -> None:
        assert "--model" not in _cmd()

    def test_model_is_forwarded_when_configured(self) -> None:
        cmd = GeminiProvider().build_command(prompt="task", model="gemini-2.5-pro")
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "gemini-2.5-pro"
