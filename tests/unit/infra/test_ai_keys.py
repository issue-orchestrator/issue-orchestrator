"""Tests for AI provider API-key metadata."""

from collections.abc import Iterator

import pytest

from issue_orchestrator.infra import ai_keys


class _FakeEntryPoint:
    def __init__(self, name: str, loaded: object):
        self.name = name
        self._loaded = loaded

    def load(self) -> object:
        return self._loaded


class _FakeEntryPoints:
    def __init__(self, entry_points: list[_FakeEntryPoint]):
        self._entry_points = entry_points

    def select(self, *, group: str) -> list[_FakeEntryPoint]:
        if group == ai_keys.PROVIDER_KEY_ENTRY_POINT_GROUP:
            return self._entry_points
        return []


@pytest.fixture(autouse=True)
def clear_provider_cache() -> Iterator[None]:
    ai_keys.clear_ai_provider_cache()
    yield
    ai_keys.clear_ai_provider_cache()


def test_provider_key_extension_contributes_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional packages can contribute provider key metadata."""

    def provider_keys() -> dict[str, ai_keys.ProviderKeyInfo]:
        return {
            "EXTERNAL_PROVIDER_API_KEY": {
                "name": "External Provider",
                "provider_aliases": ("external-provider",),
            },
        }

    monkeypatch.setattr(
        ai_keys.metadata,
        "entry_points",
        lambda: _FakeEntryPoints([_FakeEntryPoint("external", provider_keys)]),
    )

    providers = ai_keys.get_ai_providers()
    provider_key_map = ai_keys.get_provider_key_map()

    assert providers["EXTERNAL_PROVIDER_API_KEY"]["name"] == "External Provider"
    assert provider_key_map["external-provider"] == "EXTERNAL_PROVIDER_API_KEY"
    assert ai_keys.normalize_ai_key_name("external-provider") == "EXTERNAL_PROVIDER_API_KEY"


def test_provider_registry_mutation_does_not_modify_cached_state() -> None:
    """Callers receive a copy, not the cached provider registry itself."""
    providers = ai_keys.get_ai_providers()

    providers["EXTRA_API_KEY"] = {
        "name": "Extra",
        "provider_aliases": ("extra",),
    }
    providers["OPENAI_API_KEY"]["name"] = "Mutated"

    fresh_providers = ai_keys.get_ai_providers()

    assert "EXTRA_API_KEY" not in fresh_providers
    assert fresh_providers["OPENAI_API_KEY"]["name"] == "OpenAI (Codex/GPT)"


def test_provider_key_extension_rejects_malformed_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed optional provider-key packages fail loudly."""
    monkeypatch.setattr(
        ai_keys.metadata,
        "entry_points",
        lambda: _FakeEntryPoints([_FakeEntryPoint("broken", lambda: ["not", "a", "dict"])]),
    )

    with pytest.raises(ai_keys.ProviderKeyPluginError):
        ai_keys.get_ai_providers()


def test_provider_key_extension_wraps_load_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broken optional provider-key package imports fail with a registry error."""

    class BrokenEntryPoint(_FakeEntryPoint):
        def load(self) -> object:
            raise ImportError("missing optional dependency")

    monkeypatch.setattr(
        ai_keys.metadata,
        "entry_points",
        lambda: _FakeEntryPoints([BrokenEntryPoint("broken", object())]),
    )

    with pytest.raises(ai_keys.ProviderKeyPluginError) as exc_info:
        ai_keys.get_ai_providers()

    assert "failed to load" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ImportError)
