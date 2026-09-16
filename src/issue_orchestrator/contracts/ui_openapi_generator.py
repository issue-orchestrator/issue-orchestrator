"""Generate UI OpenAPI artifacts from the canonical schema."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path("docs/api/ui-openapi.json")
PYTHON_OUT = Path("src/issue_orchestrator/contracts/ui_openapi_models.py")
DTS_OUT = Path("src/issue_orchestrator/static/js/ui-contracts.d.ts")
VALIDATORS_OUT = Path("src/issue_orchestrator/static/js/ui-contracts.validators.js")

HEADER = """# This file is generated from docs/api/ui-openapi.json.
# Do not edit by hand. Run: scripts/generate_ui_contracts.py
"""

DTS_HEADER = """// This file is generated from docs/api/ui-openapi.json.
// Do not edit by hand. Run: scripts/generate_ui_contracts.py
"""

# Keywords the browser validation engine enforces. Anything outside this
# set (plus ``IGNORED_SCHEMA_KEYWORDS``) fails generation rather than
# silently generating a validator that under-enforces the contract.
SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "$ref",
        "additionalProperties",
        "anyOf",
        "const",
        "discriminator",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "nullable",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "type",
    }
)

# Documentation-only keywords: they carry no runtime constraint, so the
# generated registry drops them instead of shipping prose to the browser.
IGNORED_SCHEMA_KEYWORDS = frozenset(
    {"default", "description", "example", "examples", "format", "title"}
)


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


_PYDANTIC_FIELD_CONSTRAINTS = (
    ("minimum", "ge"),
    ("exclusiveMinimum", "gt"),
    ("maximum", "le"),
    ("exclusiveMaximum", "lt"),
    ("minLength", "min_length"),
    ("maxLength", "max_length"),
    ("minItems", "min_length"),
    ("maxItems", "max_length"),
)


def _pydantic_field_constraints(prop_schema: dict[str, Any]) -> list[str]:
    """Map JSON-schema numeric/size constraints to Pydantic ``Field`` kwargs.

    Without this, e.g. ``{"type": "integer", "minimum": 1}`` in the
    UI OpenAPI schema would generate ``int`` with no runtime check —
    so a contract that says ``run_id >= 1`` would silently accept 0
    in the Python contract layer (reviewer caught this on PR #6329).

    Also emits ``strict=True`` for integer fields with explicit
    numeric constraints (PR #6329 round-5).  JSON Schema's
    ``type: integer`` does not coerce ``"88"`` → 88 or ``True`` → 1,
    so the generated Pydantic model must not coerce either.  Without
    ``strict``, Pydantic accepts both and silently normalizes a
    malformed wire payload.  Strict is applied only to integers
    with constraints (the narrowest fix consistent with the
    reviewer's invariant request). Boolean fields are always strict:
    JSON Schema never treats ``0``/``1`` as booleans, while Pydantic's
    default ``bool`` parser does.
    """
    constraints: list[str] = []
    has_numeric_constraint = any(
        k in prop_schema for k in ("minimum", "exclusiveMinimum", "maximum", "exclusiveMaximum")
    )
    constraints.extend(
        f"{field_name}={prop_schema[schema_name]}"
        for schema_name, field_name in _PYDANTIC_FIELD_CONSTRAINTS
        if schema_name in prop_schema
    )
    # Numeric-constrained integers get strict scalar semantics so
    # the generated contract matches the canonical model + JSON
    # Schema (no coercion of strings/booleans).
    schema_type = prop_schema.get("type")
    is_integer = schema_type == "integer" or (
        isinstance(schema_type, list) and "integer" in schema_type
    )
    if has_numeric_constraint and is_integer:
        constraints.append("strict=True")
    is_boolean = schema_type == "boolean" or (
        isinstance(schema_type, list) and "boolean" in schema_type
    )
    if is_boolean:
        constraints.append("strict=True")
    return constraints


@dataclass(frozen=True)
class ClassifiedComponents:
    """Components partitioned by how they render.

    ``enum_aliases`` and ``union_aliases`` become ``Literal``/union type
    aliases; everything else becomes an object model. Enum aliases carry no
    forward references, so callers emit them before the models that ``$ref``
    them.
    """

    enum_aliases: tuple[ComponentSchema, ...]
    union_aliases: tuple[ComponentSchema, ...]
    models: tuple[ComponentSchema, ...]


def _classify_components(components: list[ComponentSchema]) -> ClassifiedComponents:
    """Split components into enum aliases, union aliases, and object models,
    preserving their (already sorted) input order within each group."""
    enum_aliases: list[ComponentSchema] = []
    union_aliases: list[ComponentSchema] = []
    models: list[ComponentSchema] = []
    for component in components:
        if _is_enum_alias_schema(component):
            enum_aliases.append(component)
        elif _is_union_alias_schema(component):
            union_aliases.append(component)
        else:
            models.append(component)
    return ClassifiedComponents(tuple(enum_aliases), tuple(union_aliases), tuple(models))


def _ordered_union_aliases(
    components: tuple[ComponentSchema, ...],
) -> tuple[ComponentSchema, ...]:
    """Order union aliases after any union aliases they reference."""
    by_name = {component.name: component for component in components}
    ordered: list[ComponentSchema] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(component: ComponentSchema) -> None:
        if component.name in visited:
            return
        if component.name in visiting:
            raise ValueError(f"cyclic union alias reference involving {component.name}")
        visiting.add(component.name)
        for branch in component.schema.get("oneOf") or component.schema.get("anyOf") or []:
            ref = branch.get("$ref") if isinstance(branch, dict) else None
            dependency = by_name.get(ref_name(ref)) if isinstance(ref, str) else None
            if dependency is not None:
                visit(dependency)
        visiting.remove(component.name)
        visited.add(component.name)
        ordered.append(component)

    for component in components:
        visit(component)
    return tuple(ordered)


def render_python_models(components: list[ComponentSchema]) -> str:
    lines: list[str] = [
        HEADER,
        "\n",
        "from __future__ import annotations",
        "\n",
        "import re",
        "\n",
        "from typing import Any, Literal, TypeAlias",
        "\n",
        "from pydantic import BaseModel, ConfigDict, Field, field_validator",
        "\n",
        "\n",
    ]

    classified = _classify_components(components)
    # Enum aliases carry no forward references, so emit them first —
    # every model field that ``$ref``s them resolves without a rebuild.
    for component in classified.enum_aliases:
        lines.append(f"{component.name}: TypeAlias = {resolve_type(component.schema)}")
        lines.append("")
    for component in classified.models:
        lines.extend(_render_python_model(component))
    for component in _ordered_union_aliases(classified.union_aliases):
        lines.append(f"{component.name}: TypeAlias = {resolve_type(component.schema)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_python_model(component: ComponentSchema) -> list[str]:
    schema = component.schema
    extra_mode = "allow" if schema.get("additionalProperties") is True else "forbid"
    lines = [
        f"class {component.name}(BaseModel):",
        f"    model_config = ConfigDict(extra=\"{extra_mode}\")",
    ]
    properties = schema.get("properties", {})
    if not properties:
        lines += ["    pass", ""]
        return lines

    required = set(schema.get("required", []))
    for prop in sorted(properties.keys()):
        prop_schema = properties[prop]
        annotation = resolve_type(prop_schema)
        if prop not in required and not is_optional(prop_schema):
            annotation = f"{annotation} | None"
        constraints = _pydantic_field_constraints(prop_schema)
        lines.append(_python_field_line(prop, annotation, constraints, prop in required))
    for prop in sorted(properties.keys()):
        pattern = properties[prop].get("pattern")
        if not isinstance(pattern, str):
            continue
        lines.extend(
            [
                "",
                f"    @field_validator({prop!r})",
                "    @classmethod",
                f"    def _validate_{prop}_pattern(cls, value: Any) -> Any:",
                f"        if value is not None and re.search({pattern!r}, value) is None:",
                f"            raise ValueError({f'{prop} must match {pattern!r}'!r})",
                "        return value",
            ]
        )
    lines.append("")
    return lines


def _python_field_line(
    prop: str, annotation: str, constraints: list[str], required: bool
) -> str:
    if not constraints:
        default = "" if required else " = None"
        return f"    {prop}: {annotation}{default}"
    # Required props get ``Field(..., ge=N)``; optional ones get
    # ``Field(default=None, ge=N)``.
    head = "Field(..., " if required else "Field(default=None, "
    return f"    {prop}: {annotation} = {head}{', '.join(constraints)})"


def render_dts_types(components: list[ComponentSchema]) -> str:
    lines: list[str] = [DTS_HEADER, "\n"]
    classified = _classify_components(components)
    for component in classified.enum_aliases:
        lines.append(f"export type {component.name} = {ts_type(component.schema)};\n")
    for component in classified.models:
        lines.extend(_render_dts_interface(component))
    for component in classified.union_aliases:
        lines.append(f"export type {component.name} = {ts_type(component.schema)};\n")
    return "\n".join(lines).rstrip() + "\n"


def _render_dts_interface(component: ComponentSchema) -> list[str]:
    schema = component.schema
    required = set(schema.get("required", []))
    properties = schema.get("properties", {})
    lines = [f"export interface {component.name} {{"]
    for prop in sorted(properties.keys()):
        suffix = "?" if prop not in required else ""
        lines.append(f"  {prop}{suffix}: {ts_type(properties[prop])};")
    if schema.get("additionalProperties") is True:
        lines.append("  [key: string]: any;")
    lines.append("}\n")
    return lines


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


VALIDATORS_HEADER = """// This file is generated from docs/api/ui-openapi.json.
// Do not edit by hand. Run: scripts/generate_ui_contracts.py
//
// Browser runtime validators for UI JSON payloads (issue #6337).
//
// ``SCHEMAS`` is the validation-relevant projection of the canonical
// OpenAPI components; the engine below enforces it in the browser so the
// contract layer reaches all the way into JavaScript instead of stopping
// at the server response model.
//
// This module owns JSON *payload shape* only.  Contextual invariants a
// schema cannot know (does this command's run_id match the DOM row that
// owns it?) stay in the UI owner abstractions that hold that context.
//
// Parse JSON through ``ui_contract_json.js`` rather than calling
// ``validate`` directly; that helper is the shared fail-closed entry
// point for every browser JSON boundary.
"""

_VALIDATOR_ENGINE = """
    const MAX_REF_HOPS = 100;

    function schemaNames() {
        return Object.keys(SCHEMAS).sort();
    }

    function hasSchema(name) {
        return Object.prototype.hasOwnProperty.call(SCHEMAS, name);
    }

    // Validates ``value`` against the named contract schema.
    //
    // Returns a result object rather than throwing, so callers fail
    // closed on ``ok === false`` without wrapping every JSON boundary in
    // a try/catch.  An unknown ``schemaName`` DOES throw: the name is
    // always a literal in browser code, never wire data, so a miss is a
    // programming error and must not be swallowed.
    function validate(schemaName, value) {
        if (!hasSchema(schemaName)) {
            throw new Error('Unknown UI contract schema: ' + schemaName);
        }
        const errors = [];
        _check(SCHEMAS[schemaName], value, '$', errors);
        if (errors.length) {
            return { ok: false, value: null, errors: errors, schemaName: schemaName };
        }
        return { ok: true, value: value, errors: [], schemaName: schemaName };
    }

    function _refName(ref) {
        const text = String(ref);
        return text.slice(text.lastIndexOf('/') + 1);
    }

    function _resolve(schema, path, errors) {
        let current = schema;
        let hops = 0;
        while (current && typeof current === 'object' && current.$ref) {
            const name = _refName(current.$ref);
            if (!hasSchema(name)) {
                errors.push(path + ': contract references unknown schema ' + name);
                return null;
            }
            current = SCHEMAS[name];
            hops += 1;
            if (hops > MAX_REF_HOPS) {
                errors.push(path + ': contract $ref chain does not terminate');
                return null;
            }
        }
        if (!current || typeof current !== 'object' || Array.isArray(current)) {
            errors.push(path + ': contract schema is not an object');
            return null;
        }
        return current;
    }

    function _check(schema, value, path, errors) {
        const resolved = _resolve(schema, path, errors);
        if (!resolved) return;
        if (resolved.nullable === true && value === null) return;
        if (Object.prototype.hasOwnProperty.call(resolved, 'const')) {
            if (value !== resolved.const) {
                errors.push(
                    path + ': expected ' + JSON.stringify(resolved.const)
                    + ', got ' + _describe(value),
                );
            }
            return;
        }
        if (Array.isArray(resolved.enum)) {
            if (resolved.enum.indexOf(value) === -1) {
                errors.push(
                    path + ': expected one of ' + JSON.stringify(resolved.enum)
                    + ', got ' + _describe(value),
                );
            }
            return;
        }
        const union = resolved.oneOf || resolved.anyOf;
        if (union) {
            _checkUnion(resolved, union, value, path, errors);
            return;
        }
        _checkTyped(resolved, value, path, errors);
    }

    function _checkTyped(schema, value, path, errors) {
        if (schema.type === undefined) return;
        const types = Array.isArray(schema.type) ? schema.type : [schema.type];
        let matched = null;
        for (const type of types) {
            if (_matchesType(type, value)) {
                matched = type;
                break;
            }
        }
        if (matched === null) {
            errors.push(path + ': expected ' + types.join(' | ') + ', got ' + _describe(value));
            return;
        }
        if (matched === 'object') {
            _checkObject(schema, value, path, errors);
        } else if (matched === 'array') {
            _checkArray(schema, value, path, errors);
        } else if (matched === 'string') {
            _checkString(schema, value, path, errors);
        } else if (matched === 'integer' || matched === 'number') {
            _checkNumber(schema, value, path, errors);
        }
    }

    // Wire-format types, so no coercion: "88" is not an integer and
    // `true` is not a 1.  This mirrors the ``strict=True`` the Python
    // generator emits for constrained integers — a malformed payload
    // must be rejected, never silently normalized.
    function _matchesType(type, value) {
        switch (type) {
            case 'null': return value === null;
            case 'boolean': return typeof value === 'boolean';
            case 'string': return typeof value === 'string';
            case 'integer': return typeof value === 'number' && Number.isInteger(value);
            case 'number': return typeof value === 'number' && Number.isFinite(value);
            case 'array': return Array.isArray(value);
            case 'object': return value !== null && typeof value === 'object' && !Array.isArray(value);
            default: return false;
        }
    }

    function _checkUnion(schema, branches, value, path, errors) {
        const propertyName = schema.discriminator && schema.discriminator.propertyName;
        if (propertyName && _matchesType('object', value)) {
            const branch = _branchForTag(branches, propertyName, value[propertyName]);
            if (!branch) {
                errors.push(
                    path + '.' + propertyName + ': no contract variant matches '
                    + _describe(value[propertyName]),
                );
                return;
            }
            _check(branch, value, path, errors);
            return;
        }
        // Untagged union, or a non-object under a discriminated union
        // (e.g. the `null` branch of an optional $ref): the value must
        // satisfy at least one variant.
        for (const branch of branches) {
            const branchErrors = [];
            _check(branch, value, path, branchErrors);
            if (!branchErrors.length) return;
        }
        errors.push(path + ': matches no contract variant, got ' + _describe(value));
    }

    function _branchForTag(branches, propertyName, tag) {
        for (const branch of branches) {
            const resolved = _resolve(branch, '$', []);
            if (!resolved) continue;
            const nested = resolved.oneOf || resolved.anyOf;
            if (nested) {
                const nestedBranch = _branchForTag(nested, propertyName, tag);
                if (nestedBranch) return nestedBranch;
                continue;
            }
            if (!resolved.properties) continue;
            const tagSchema = resolved.properties[propertyName];
            if (!tagSchema) continue;
            if (Object.prototype.hasOwnProperty.call(tagSchema, 'const')) {
                if (tagSchema.const === tag) return branch;
            } else if (Array.isArray(tagSchema.enum) && tagSchema.enum.indexOf(tag) !== -1) {
                return branch;
            }
        }
        return null;
    }

    function _checkObject(schema, value, path, errors) {
        const properties = schema.properties || {};
        const required = Array.isArray(schema.required) ? schema.required : [];
        for (const key of required) {
            if (!Object.prototype.hasOwnProperty.call(value, key)) {
                errors.push(path + '.' + key + ': required property is missing');
            }
        }
        const additional = schema.additionalProperties;
        for (const key of Object.keys(value)) {
            const childPath = path + '.' + key;
            if (Object.prototype.hasOwnProperty.call(properties, key)) {
                // Parity with the generated Pydantic models: a property
                // outside `required` renders as `T | None = None`, which
                // accepts an explicit null on the wire.
                if (value[key] === null && required.indexOf(key) === -1) continue;
                _check(properties[key], value[key], childPath, errors);
            } else if (additional === true) {
                continue;
            } else if (additional && typeof additional === 'object') {
                _check(additional, value[key], childPath, errors);
            } else {
                errors.push(childPath + ': unexpected property is not allowed by the contract');
            }
        }
    }

    function _checkArray(schema, value, path, errors) {
        if (schema.minItems !== undefined && value.length < schema.minItems) {
            errors.push(path + ': expected minItems ' + schema.minItems + ', got length ' + value.length);
        }
        if (schema.maxItems !== undefined && value.length > schema.maxItems) {
            errors.push(path + ': expected maxItems ' + schema.maxItems + ', got length ' + value.length);
        }
        if (schema.items === undefined) return;
        for (let index = 0; index < value.length; index += 1) {
            _check(schema.items, value[index], path + '[' + index + ']', errors);
        }
    }

    function _checkString(schema, value, path, errors) {
        if (schema.minLength !== undefined && value.length < schema.minLength) {
            errors.push(path + ': expected minLength ' + schema.minLength + ', got length ' + value.length);
        }
        if (schema.maxLength !== undefined && value.length > schema.maxLength) {
            errors.push(path + ': expected maxLength ' + schema.maxLength + ', got length ' + value.length);
        }
        if (schema.pattern !== undefined && !(new RegExp(schema.pattern)).test(value)) {
            errors.push(path + ': does not match pattern ' + JSON.stringify(schema.pattern));
        }
    }

    function _checkNumber(schema, value, path, errors) {
        if (schema.minimum !== undefined && value < schema.minimum) {
            errors.push(path + ': expected >= ' + schema.minimum + ', got ' + value);
        }
        if (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) {
            errors.push(path + ': expected > ' + schema.exclusiveMinimum + ', got ' + value);
        }
        if (schema.maximum !== undefined && value > schema.maximum) {
            errors.push(path + ': expected <= ' + schema.maximum + ', got ' + value);
        }
        if (schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum) {
            errors.push(path + ': expected < ' + schema.exclusiveMaximum + ', got ' + value);
        }
    }

    function _describe(value) {
        if (value === null) return 'null';
        if (value === undefined) return 'undefined';
        if (Array.isArray(value)) return 'array';
        const type = typeof value;
        if (type === 'string' || type === 'number' || type === 'boolean') {
            return type + ' ' + JSON.stringify(value);
        }
        return type;
    }
"""


def _project_schema_keyword(key: str, value: Any, path: str) -> Any:
    if key == "properties":
        return {name: validation_projection(sub, f"{path}/{name}") for name, sub in value.items()}
    if key == "items":
        return validation_projection(value, f"{path}/items")
    if key in ("oneOf", "anyOf"):
        return [validation_projection(sub, f"{path}/{key}/{i}") for i, sub in enumerate(value)]
    if key == "additionalProperties" and isinstance(value, dict):
        return validation_projection(value, f"{path}/additionalProperties")
    return value


def validation_projection(schema: Any, path: str = "") -> Any:
    """Strip a schema down to what the browser engine actually enforces.

    Prose keywords (``description``, ``default``, …) are dropped so the
    generated registry ships constraints rather than documentation.

    An unrecognised keyword raises instead of being ignored. Silently
    dropping, say, a newly added ``pattern`` would emit a validator that
    claims to enforce the contract while quietly under-checking it — the
    exact "looks validated but isn't" failure this layer exists to
    prevent. Adding a keyword here means teaching ``_VALIDATOR_ENGINE``
    to enforce it first.
    """
    if not isinstance(schema, dict):
        return schema
    projected: dict[str, Any] = {}
    for key, value in schema.items():
        if key in IGNORED_SCHEMA_KEYWORDS:
            continue
        if key not in SUPPORTED_SCHEMA_KEYWORDS:
            raise ValueError(
                f"{path or '<root>'}: JSON Schema keyword {key!r} is not enforced by the "
                "generated browser validator. Teach _VALIDATOR_ENGINE to enforce it and add "
                "it to SUPPORTED_SCHEMA_KEYWORDS, or add it to IGNORED_SCHEMA_KEYWORDS if it "
                "carries no runtime constraint."
            )
        projected[key] = _project_schema_keyword(key, value, path)
    return projected


def render_js_validators(components: list[ComponentSchema]) -> str:
    registry = {
        component.name: validation_projection(component.schema, component.name)
        for component in components
    }
    registry_json = json.dumps(registry, indent=4, sort_keys=True)
    # Re-indent the literal to sit inside the module factory body.
    registry_body = "\n".join(
        f"    {line}" if line else line for line in registry_json.splitlines()
    ).lstrip()
    return (
        VALIDATORS_HEADER
        + "(function (root, factory) {\n"
        + "    const api = factory();\n"
        + "    if (typeof module === 'object' && module.exports) {\n"
        + "        module.exports = api;\n"
        + "    }\n"
        + "    if (root) {\n"
        + "        root.uiContractValidators = api;\n"
        + "    }\n"
        + "})(typeof globalThis !== 'undefined' ? globalThis : this, function () {\n"
        + f"    const SCHEMAS = {registry_body};\n"
        + _VALIDATOR_ENGINE
        + "\n    return {\n"
        + "        SCHEMAS: SCHEMAS,\n"
        + "        hasSchema: hasSchema,\n"
        + "        schemaNames: schemaNames,\n"
        + "        validate: validate,\n"
        + "    };\n"
        + "});\n"
    )


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


def _is_union_alias_schema(component: ComponentSchema) -> bool:
    schema = component.schema
    has_union = bool(schema.get("oneOf") or schema.get("anyOf"))
    if has_union and schema.get("properties"):
        raise ValueError("component schemas must not mix oneOf/anyOf with properties")
    return has_union


def _is_enum_alias_schema(component: ComponentSchema) -> bool:
    """A top-level ``enum`` component (e.g. ``TimelineView``) renders as a
    reusable ``Literal`` alias, not a Pydantic model.

    Without this a bare-enum component would fall through to the object
    branch and emit an empty ``class TimelineView(BaseModel): pass`` — a
    useless model that ``$ref`` sites could not narrow against.  Enum
    aliases carry no forward references, so they are emitted before the
    model classes and can be referenced by any field.
    """
    schema = component.schema
    has_enum = "enum" in schema and isinstance(schema["enum"], list)
    if has_enum and schema.get("properties"):
        raise ValueError("component schemas must not mix enum with properties")
    return has_enum


def generate_artifacts(
    schema_path: Path | None = None,
    python_out: Path | None = None,
    dts_out: Path | None = None,
    validators_out: Path | None = None,
) -> None:
    data = load_schema(schema_path)
    components = iter_components(data)

    python_path = python_out or PYTHON_OUT
    dts_path = dts_out or DTS_OUT
    validators_path = validators_out or VALIDATORS_OUT

    python_path.write_text(render_python_models(components))
    dts_path.parent.mkdir(parents=True, exist_ok=True)
    dts_path.write_text(render_dts_types(components))
    validators_path.parent.mkdir(parents=True, exist_ok=True)
    validators_path.write_text(render_js_validators(components))
