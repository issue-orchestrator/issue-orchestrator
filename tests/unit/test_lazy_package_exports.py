from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "issue_orchestrator.domain",
        "issue_orchestrator.execution",
        "issue_orchestrator.ports",
    ],
)
def test_lazy_package_exports_match_all_and_resolve(module_name: str) -> None:
    module = importlib.import_module(module_name)
    exports = getattr(module, "_EXPORTS")

    assert tuple(exports) == tuple(module.__all__)
    for name in module.__all__:
        imported = __import__(module_name, fromlist=[name])
        assert getattr(imported, name) is getattr(module, name)


@pytest.mark.parametrize(
    "module_name",
    [
        "issue_orchestrator.domain",
        "issue_orchestrator.execution",
        "issue_orchestrator.ports",
    ],
)
def test_lazy_package_exports_reject_unknown_names(module_name: str) -> None:
    module = importlib.import_module(module_name)

    with pytest.raises(AttributeError):
        getattr(module, "not_exported")
