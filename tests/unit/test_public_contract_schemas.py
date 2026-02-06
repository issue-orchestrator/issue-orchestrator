"""Ensure public contract schemas are generated and kept in sync."""

from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.contracts.public import generate_public_schemas


def test_public_contract_schemas_are_current():
    base_dir = Path(__file__).resolve().parents[2]
    schema_dir = base_dir / "contracts" / "public"
    file_map = {
        path.stem: json.loads(path.read_text())
        for path in schema_dir.glob("*.json")
    }

    generated = generate_public_schemas()
    assert set(file_map) == set(generated)

    for name, schema in generated.items():
        assert file_map[name] == schema
