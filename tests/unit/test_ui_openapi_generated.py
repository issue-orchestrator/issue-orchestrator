"""Guardrails for UI OpenAPI generated artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.contracts.ui_openapi_generator import (
    ComponentSchema,
    generate_artifacts,
    is_optional,
    render_dts_types,
    render_python_models,
    resolve_type,
)


def test_ui_openapi_artifacts_match_generated(tmp_path: Path) -> None:
    python_out = tmp_path / "ui_openapi_models.py"
    dts_out = tmp_path / "ui-contracts.d.ts"

    generate_artifacts(python_out=python_out, dts_out=dts_out)

    assert python_out.read_text() == Path("src/issue_orchestrator/contracts/ui_openapi_models.py").read_text()
    assert dts_out.read_text() == Path("src/issue_orchestrator/static/js/ui-contracts.d.ts").read_text()


def test_ui_openapi_generator_renders_const_enum_and_union_shapes() -> None:
    components = [
        ComponentSchema(
            "ConstEnumPayload",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "state", "maybe_text"],
                "properties": {
                    "kind": {"const": "const_enum"},
                    "state": {"enum": ["queued", "done"]},
                    "maybe_text": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                },
            },
        ),
        ComponentSchema(
            "UnionPayload",
            {
                "oneOf": [
                    {"$ref": "#/components/schemas/ConstEnumPayload"},
                    {"type": "null"},
                ],
            },
        ),
    ]

    python_models = render_python_models(components)
    dts_types = render_dts_types(components)

    assert "kind: Literal['const_enum']" in python_models
    assert "state: Literal['queued'] | Literal['done']" in python_models
    assert "maybe_text: str | None" in python_models
    assert "UnionPayload: TypeAlias = ConstEnumPayload | None" in python_models
    assert 'kind: "const_enum";' in dts_types
    assert 'state: "queued" | "done";' in dts_types
    assert "maybe_text: string | null;" in dts_types
    assert "export type UnionPayload = ConstEnumPayload | null;" in dts_types


def test_ui_openapi_generator_detects_nullable_schema_variants() -> None:
    assert is_optional(
        {
            "oneOf": [
                {"$ref": "#/components/schemas/IssueDetailPayload"},
                {"type": "null"},
            ],
        }
    )
    assert is_optional({"type": ["integer", "null"]})
    assert is_optional({"type": "string", "nullable": True})
    assert is_optional({"anyOf": [{"type": "string"}, {"type": "null"}]})
    assert resolve_type({"type": ["integer", "null"]}) == "int | None"


def test_ui_openapi_generator_rejects_mixed_union_object_schema() -> None:
    components = [
        ComponentSchema(
            "MixedPayload",
            {
                "oneOf": [{"type": "string"}, {"type": "null"}],
                "properties": {"kind": {"const": "mixed"}},
            },
        ),
    ]

    with pytest.raises(ValueError, match="must not mix oneOf/anyOf with properties"):
        render_python_models(components)
