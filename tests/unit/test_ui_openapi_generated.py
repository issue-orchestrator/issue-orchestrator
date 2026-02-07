"""Guardrails for UI OpenAPI generated artifacts."""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.contracts.ui_openapi_generator import generate_artifacts


def test_ui_openapi_artifacts_match_generated(tmp_path: Path) -> None:
    python_out = tmp_path / "ui_openapi_models.py"
    dts_out = tmp_path / "ui-contracts.d.ts"

    generate_artifacts(python_out=python_out, dts_out=dts_out)

    assert python_out.read_text() == Path("src/issue_orchestrator/contracts/ui_openapi_models.py").read_text()
    assert dts_out.read_text() == Path("src/issue_orchestrator/static/js/ui-contracts.d.ts").read_text()
