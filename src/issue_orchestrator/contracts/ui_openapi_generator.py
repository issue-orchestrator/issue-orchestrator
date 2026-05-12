"""Generate UI OpenAPI artifacts from the canonical schema."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path("docs/api/ui-openapi.json")
PYTHON_OUT = Path("src/issue_orchestrator/contracts/ui_openapi_models.py")
DTS_OUT = Path("src/issue_orchestrator/static/js/ui-contracts.d.ts")

HEADER = """# This file is generated from docs/api/ui-openapi.json.
# Do not edit by hand. Run: scripts/generate_ui_contracts.py
"""

DTS_HEADER = """// This file is generated from docs/api/ui-openapi.json.
// Do not edit by hand. Run: scripts/generate_ui_contracts.py
"""


@dataclass(frozen=True)
class ComponentSchema:
    name: str
    schema: dict[str, Any]


def load_schema(schema_path: Path | None = None) -> dict[str, Any]:
    path = schema_path or SCHEMA_PATH
    return json.loads(path.read_text())


def ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def resolve_type(schema: dict[str, Any]) -> str:
    if "const" in schema:
        return _python_literal(schema["const"])
    if "enum" in schema and isinstance(schema["enum"], list):
        return _resolve_python_enum(schema["enum"])
    if "$ref" in schema:
        return ref_name(schema["$ref"])

    union = schema.get("oneOf") or schema.get("anyOf")
    if union:
        return " | ".join(resolve_type(s) for s in union)

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return _resolve_type_union(schema, schema_type)

    return _resolve_single_type(schema_type, schema)


def _resolve_type_union(schema: dict[str, Any], schema_types: list[str]) -> str:
    resolved = [resolve_type({**schema, "type": t}) for t in schema_types if t != "null"]
    if "null" in schema_types:
        resolved.append("None")
    return " | ".join(dict.fromkeys(resolved))


def _resolve_single_type(schema_type: str | None, schema: dict[str, Any]) -> str:
    scalar = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "null": "None",
    }
    if schema_type in scalar:
        return scalar[schema_type]
    if schema_type == "array":
        return f"list[{resolve_type(schema.get('items', {}))}]"
    if schema_type == "object":
        return _resolve_object_type(schema)
    return "Any"


def _resolve_object_type(schema: dict[str, Any]) -> str:
    additional = schema.get("additionalProperties")
    if additional is True:
        return "dict[str, Any]"
    if isinstance(additional, dict):
        return f"dict[str, {resolve_type(additional)}]"
    return "dict[str, Any]"


def is_optional(schema: dict[str, Any]) -> bool:
    if _schema_allows_null(schema):
        return True
    if schema.get("nullable") is True:
        return True
    if any(_schema_allows_null(s) for s in schema.get("oneOf", [])):
        return True
    if any(_schema_allows_null(s) for s in schema.get("anyOf", [])):
        return True
    return False


def _schema_allows_null(schema: dict[str, Any]) -> bool:
    schema_type = schema.get("type")
    return schema_type == "null" or (
        isinstance(schema_type, list) and "null" in schema_type
    )


def iter_components(data: dict[str, Any]) -> list[ComponentSchema]:
    schemas = data.get("components", {}).get("schemas", {})
    return [ComponentSchema(name, schema) for name, schema in sorted(schemas.items())]


def _pydantic_field_constraints(prop_schema: dict[str, Any]) -> list[str]:
    """Map JSON-schema numeric/length constraints to Pydantic ``Field`` kwargs.

    Without this, e.g. ``{"type": "integer", "minimum": 1}`` in the
    UI OpenAPI schema would generate ``int`` with no runtime check —
    so a contract that says ``run_id >= 1`` would silently accept 0
    in the Python contract layer (reviewer caught this on PR #6329).
    """
    constraints: list[str] = []
    if "minimum" in prop_schema:
        constraints.append(f"ge={prop_schema['minimum']}")
    if "exclusiveMinimum" in prop_schema:
        constraints.append(f"gt={prop_schema['exclusiveMinimum']}")
    if "maximum" in prop_schema:
        constraints.append(f"le={prop_schema['maximum']}")
    if "exclusiveMaximum" in prop_schema:
        constraints.append(f"lt={prop_schema['exclusiveMaximum']}")
    if "minLength" in prop_schema:
        constraints.append(f"min_length={prop_schema['minLength']}")
    if "maxLength" in prop_schema:
        constraints.append(f"max_length={prop_schema['maxLength']}")
    return constraints


def render_python_models(components: list[ComponentSchema]) -> str:
    lines: list[str] = [
        HEADER,
        "\n",
        "from __future__ import annotations",
        "\n",
        "from typing import Any, Literal, TypeAlias",
        "\n",
        "from pydantic import BaseModel, ConfigDict, Field",
        "\n",
        "\n",
    ]

    alias_components: list[ComponentSchema] = []
    for component in components:
        name = component.name
        schema = component.schema
        if _is_union_alias_schema(schema):
            alias_components.append(component)
            continue
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        additional = schema.get("additionalProperties")
        extra_mode = "forbid"
        if additional is True:
            extra_mode = "allow"

        lines.append(f"class {name}(BaseModel):")
        lines.append(f"    model_config = ConfigDict(extra=\"{extra_mode}\")")
        if not properties:
            lines.append("    pass")
            lines.append("")
            continue

        for prop in sorted(properties.keys()):
            prop_schema = properties[prop]
            annotation = resolve_type(prop_schema)
            if prop not in required and not is_optional(prop_schema):
                annotation = f"{annotation} | None"
            constraints = _pydantic_field_constraints(prop_schema)
            if constraints:
                # Required props get ``Field(..., ge=N)``; optional
                # ones get ``Field(default=None, ge=N)``.
                if prop in required:
                    field_call = "Field(..., " + ", ".join(constraints) + ")"
                else:
                    field_call = "Field(default=None, " + ", ".join(constraints) + ")"
                lines.append(f"    {prop}: {annotation} = {field_call}")
            else:
                default = "" if prop in required else " = None"
                lines.append(f"    {prop}: {annotation}{default}")

        lines.append("")

    for component in alias_components:
        lines.append(f"{component.name}: TypeAlias = {resolve_type(component.schema)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_dts_types(components: list[ComponentSchema]) -> str:
    lines: list[str] = [DTS_HEADER, "\n"]
    alias_components: list[ComponentSchema] = []
    for component in components:
        name = component.name
        schema = component.schema
        if _is_union_alias_schema(schema):
            alias_components.append(component)
            continue
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        additional = schema.get("additionalProperties")

        lines.append(f"export interface {name} {{")
        for prop in sorted(properties.keys()):
            prop_schema = properties[prop]
            optional = prop not in required
            suffix = "?" if optional else ""
            lines.append(f"  {prop}{suffix}: {ts_type(prop_schema)};")
        if additional is True:
            lines.append("  [key: string]: any;")
        lines.append("}\n")

    for component in alias_components:
        lines.append(f"export type {component.name} = {ts_type(component.schema)};\n")

    return "\n".join(lines).rstrip() + "\n"


def ts_type(schema: dict[str, Any]) -> str:
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema and isinstance(schema["enum"], list):
        return " | ".join(json.dumps(item) for item in schema["enum"])
    if "$ref" in schema:
        return ref_name(schema["$ref"])
    union = schema.get("oneOf") or schema.get("anyOf")
    if union:
        return " | ".join(ts_type(s) for s in union)

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return " | ".join(ts_type({**schema, "type": t}) for t in schema_type)

    return _ts_single_type(schema_type, schema)


def _ts_single_type(schema_type: str | None, schema: dict[str, Any]) -> str:
    scalar = {
        "null": "null",
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
    }
    if schema_type in scalar:
        return scalar[schema_type]
    if schema_type == "array":
        return f"{ts_type(schema.get('items', {}))}[]"
    if schema_type == "object":
        return _ts_object_type(schema)
    return "any"


def _ts_object_type(schema: dict[str, Any]) -> str:
    additional = schema.get("additionalProperties")
    if additional is True:
        return "Record<string, any>"
    if isinstance(additional, dict):
        return f"Record<string, {ts_type(additional)}>"
    return "Record<string, any>"


def _python_literal(value: Any) -> str:
    return f"Literal[{value!r}]"


def _resolve_python_enum(values: list[Any]) -> str:
    non_null_values = [value for value in values if value is not None]
    parts: list[str] = []
    if non_null_values:
        literal_values = ", ".join(repr(value) for value in non_null_values)
        parts.append(f"Literal[{literal_values}]")
    if any(value is None for value in values):
        parts.append("None")
    return " | ".join(parts) if parts else "Any"


def _is_union_alias_schema(schema: dict[str, Any]) -> bool:
    has_union = bool(schema.get("oneOf") or schema.get("anyOf"))
    if has_union and schema.get("properties"):
        raise ValueError("component schemas must not mix oneOf/anyOf with properties")
    return has_union


def generate_artifacts(schema_path: Path | None = None, python_out: Path | None = None, dts_out: Path | None = None) -> None:
    data = load_schema(schema_path)
    components = iter_components(data)

    python_path = python_out or PYTHON_OUT
    dts_path = dts_out or DTS_OUT

    python_path.write_text(render_python_models(components))
    dts_path.parent.mkdir(parents=True, exist_ok=True)
    dts_path.write_text(render_dts_types(components))
