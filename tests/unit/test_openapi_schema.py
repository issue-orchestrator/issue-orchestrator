"""Guardrails for the generated OpenAPI schema."""

from __future__ import annotations

import json
from pathlib import Path


def test_openapi_schema_matches_generated() -> None:
    from issue_orchestrator.entrypoints.web import app

    schema_path = Path("docs/api/openapi.json")
    assert schema_path.exists(), "Missing docs/api/openapi.json; run scripts/generate_openapi.py"

    expected = json.loads(schema_path.read_text())
    current = app.openapi()

    assert current == expected
