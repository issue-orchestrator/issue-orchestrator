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
    if "$ref" in schema:
        return ref_name(schema["$ref"])

    if "oneOf" in schema:
        return " | ".join(resolve_type(s) for s in schema["oneOf"])
    if "anyOf" in schema:
        return " | ".join(resolve_type(s) for s in schema["anyOf"])

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        types = [t for t in schema_type if t != "null"]
        resolved = [resolve_type({**schema, "type": t}) for t in types]
        resolved.append("None")
        return " | ".join(dict.fromkeys(resolved))

    if schema_type == "string":
        return "str"
    if schema_type == "integer":
        return "int"
    if schema_type == "number":
        return "float"
    if schema_type == "boolean":
        return "bool"
    if schema_type == "array":
        return f"list[{resolve_type(schema.get('items', {}))}]"
    if schema_type == "object":
        additional = schema.get("additionalProperties")
        if additional is True:
            return "dict[str, Any]"
        if isinstance(additional, dict):
            return f"dict[str, {resolve_type(additional)}]"
        return "dict[str, Any]"

    return "Any"


def is_optional(schema: dict[str, Any]) -> bool:
    schema_type = schema.get("type")
    if isinstance(schema_type, list) and "null" in schema_type:
        return True
    if schema.get("nullable") is True:
        return True
    if any("null" in (s.get("type") if isinstance(s.get("type"), list) else []) for s in schema.get("oneOf", [])):
        return True
    if any("null" in (s.get("type") if isinstance(s.get("type"), list) else []) for s in schema.get("anyOf", [])):
        return True
    return False


def iter_components(data: dict[str, Any]) -> list[ComponentSchema]:
    schemas = data.get("components", {}).get("schemas", {})
    return [ComponentSchema(name, schema) for name, schema in sorted(schemas.items())]


def render_python_models(components: list[ComponentSchema]) -> str:
    lines: list[str] = [
        HEADER,
        "\n",
        "from __future__ import annotations",
        "\n",
        "from typing import Any",
        "\n",
        "from pydantic import BaseModel, ConfigDict",
        "\n",
        "\n",
    ]

    for component in components:
        name = component.name
        schema = component.schema
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
            default = "" if prop in required else " = None"
            lines.append(f"    {prop}: {annotation}{default}")

        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_dts_types(components: list[ComponentSchema]) -> str:
    lines: list[str] = [DTS_HEADER, "\n"]

    def ts_type(schema: dict[str, Any]) -> str:
        if "$ref" in schema:
            return ref_name(schema["$ref"])
        if "oneOf" in schema:
            return " | ".join(ts_type(s) for s in schema["oneOf"])
        if "anyOf" in schema:
            return " | ".join(ts_type(s) for s in schema["anyOf"])
        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            return " | ".join(ts_type({**schema, "type": t}) for t in schema_type)
        if schema_type == "null":
            return "null"
        if schema_type == "string":
            return "string"
        if schema_type in ("integer", "number"):
            return "number"
        if schema_type == "boolean":
            return "boolean"
        if schema_type == "array":
            return f"{ts_type(schema.get('items', {}))}[]"
        if schema_type == "object":
            additional = schema.get("additionalProperties")
            if additional is True:
                return "Record<string, any>"
            if isinstance(additional, dict):
                return f"Record<string, {ts_type(additional)}>"
            return "Record<string, any>"
        return "any"

    for component in components:
        name = component.name
        schema = component.schema
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

    return "\n".join(lines).rstrip() + "\n"


def generate_artifacts(schema_path: Path | None = None, python_out: Path | None = None, dts_out: Path | None = None) -> None:
    data = load_schema(schema_path)
    components = iter_components(data)

    python_path = python_out or PYTHON_OUT
    dts_path = dts_out or DTS_OUT

    python_path.write_text(render_python_models(components))
    dts_path.parent.mkdir(parents=True, exist_ok=True)
    dts_path.write_text(render_dts_types(components))
