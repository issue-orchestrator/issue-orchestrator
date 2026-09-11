"""Guardrails for UI OpenAPI generated artifacts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from issue_orchestrator.contracts.ui_openapi_generator import (
    ComponentSchema,
    generate_artifacts,
    is_optional,
    render_dts_types,
    render_js_validators,
    render_python_models,
    resolve_type,
    validation_projection,
)


def test_ui_openapi_artifacts_match_generated(tmp_path: Path) -> None:
    python_out = tmp_path / "ui_openapi_models.py"
    dts_out = tmp_path / "ui-contracts.d.ts"
    validators_out = tmp_path / "ui-contracts.validators.js"

    generate_artifacts(
        python_out=python_out,
        dts_out=dts_out,
        validators_out=validators_out,
    )

    assert (
        python_out.read_text()
        == Path("src/issue_orchestrator/contracts/ui_openapi_models.py").read_text()
    )
    assert (
        dts_out.read_text()
        == Path("src/issue_orchestrator/static/js/ui-contracts.d.ts").read_text()
    )
    assert (
        validators_out.read_text()
        == Path("src/issue_orchestrator/static/js/ui-contracts.validators.js").read_text()
    ), "browser validators are stale — run scripts/generate_ui_contracts.py"


def test_control_center_recovery_read_is_registered_in_ui_openapi() -> None:
    from issue_orchestrator.contracts.ui_openapi_generator import load_schema

    operation = load_schema()["paths"][
        "/api/control-center/repositories/{repo_key}/validated-work"
    ]["get"]
    assert operation["operationId"] == "getControlCenterValidatedWork"
    assert operation["parameters"] == [
        {
            "name": "repo_key",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "pattern": "^repo-[0-9a-f]{64}$"},
            "description": "Opaque key issued by the configured repository registry",
        }
    ]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ControlCenterRecoveryRowsPayload"
    }


def test_control_center_recovery_stop_is_registered_in_ui_openapi() -> None:
    from issue_orchestrator.contracts.ui_openapi_generator import load_schema

    operation = load_schema()["paths"][
        "/api/control-center/repositories/{repo_key}/engines/{instance_key}/stop-validated-work-owner"
    ]["post"]
    assert operation["operationId"] == "stopControlCenterValidatedWorkOwner"
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/StopValidatedWorkOwnerRequestPayload"
    }
    for status in ["200", "404", "409", "500", "503"]:
        assert operation["responses"][status]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/StopValidatedWorkOwnerOutcomePayload"}


def _python_class_body(source: str, class_name: str) -> str:
    marker = f"class {class_name}(BaseModel):"
    start = source.index(marker)
    rest = source[start + len(marker) :]
    end = rest.find("\nclass ")
    return rest if end == -1 else rest[:end]


def _dts_interface_body(source: str, interface_name: str) -> str:
    marker = f"export interface {interface_name} {{"
    start = source.index(marker)
    rest = source[start + len(marker) :]
    return rest[: rest.index("}")]


def test_response_payload_view_fields_reference_timeline_view() -> None:
    """Issue #5976 F1: the two response payloads that echo the rendered lens
    back to the client (``IssueDetailPayload`` / ``E2ERunDetailPayload``) carry
    the shared ``TimelineView`` type in the generated artifacts, not a free
    ``str`` / ``string``.

    A plain ``str`` here would let a generated client accept or propagate an
    arbitrary response ``view`` value the runtime normalizer never produces —
    the exact looseness this contract hardening closes.
    """
    python_models = Path(
        "src/issue_orchestrator/contracts/ui_openapi_models.py"
    ).read_text()
    dts_types = Path("src/issue_orchestrator/static/js/ui-contracts.d.ts").read_text()

    for class_name in ("IssueDetailPayload", "E2ERunDetailPayload"):
        py_body = _python_class_body(python_models, class_name)
        assert "view: TimelineView | None = None" in py_body, class_name
        assert "view: str" not in py_body, class_name

        ts_body = _dts_interface_body(dts_types, class_name)
        assert "view?: TimelineView;" in ts_body, class_name
        assert "view?: string;" not in ts_body, class_name


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
    assert "state: Literal['queued', 'done']" in python_models
    assert "maybe_text: str | None" in python_models
    assert "UnionPayload: TypeAlias = ConstEnumPayload | None" in python_models
    assert 'kind: "const_enum";' in dts_types
    assert 'state: "queued" | "done";' in dts_types
    assert "maybe_text: string | null;" in dts_types
    assert "export type UnionPayload = ConstEnumPayload | null;" in dts_types


def test_ui_openapi_generator_preserves_python_regex_patterns() -> None:
    components = [
        ComponentSchema(
            "PatternPayload",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["worker_agent_label"],
                "properties": {
                    "worker_agent_label": {
                        "type": "string",
                        "pattern": r"^agent:(?!tech-lead$).+",
                    },
                },
            },
        ),
    ]

    python_models = render_python_models(components)

    assert "@field_validator('worker_agent_label')" in python_models
    assert "re.search('^agent:(?!tech-lead$).+', value)" in python_models


def test_ui_openapi_generator_preserves_array_cardinality() -> None:
    components = [
        ComponentSchema(
            "BoundedCollectionsPayload",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["empty", "populated"],
                "properties": {
                    "empty": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 0,
                    },
                    "populated": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 3,
                    },
                },
            },
        ),
    ]

    python_models = render_python_models(components)

    assert "empty: list[str] = Field(..., max_length=0)" in python_models
    assert (
        "populated: list[str] = Field(..., min_length=1, max_length=3)" in python_models
    )


def test_ui_openapi_generator_keeps_nullable_constrained_integers_strict() -> None:
    components = [
        ComponentSchema(
            "NullableIdentityPayload",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["pr_number"],
                "properties": {
                    "pr_number": {
                        "type": ["integer", "null"],
                        "minimum": 1,
                    },
                },
            },
        ),
    ]

    python_models = render_python_models(components)

    assert "pr_number: int | None = Field(..., ge=1, strict=True)" in python_models


def test_ui_openapi_generator_keeps_boolean_fields_strict() -> None:
    components = [
        ComponentSchema(
            "BooleanPayload",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["retained", "maybe_retained"],
                "properties": {
                    "retained": {"type": "boolean"},
                    "maybe_retained": {"type": ["boolean", "null"]},
                },
            },
        ),
    ]

    python_models = render_python_models(components)

    assert "retained: bool = Field(..., strict=True)" in python_models
    assert "maybe_retained: bool | None = Field(..., strict=True)" in python_models


def test_ui_openapi_generator_renders_bare_enum_component_as_reusable_alias() -> None:
    """A top-level ``enum`` component (e.g. ``TimelineView``) must render as
    a reusable ``Literal``/``type`` alias, not an empty Pydantic model, and
    ``$ref`` sites must resolve to the alias name.

    The alias carries no forward references, so it is emitted *before* any
    model that references it — that keeps the generated Pydantic module
    importable without a deferred ``model_rebuild``.
    """
    components = [
        ComponentSchema(
            "ViewEnum",
            {"type": "string", "enum": ["user", "ops", "debug", "raw"]},
        ),
        ComponentSchema(
            "UsesEnumPayload",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["view"],
                "properties": {"view": {"$ref": "#/components/schemas/ViewEnum"}},
            },
        ),
    ]

    python_models = render_python_models(components)
    dts_types = render_dts_types(components)

    assert (
        "ViewEnum: TypeAlias = Literal['user', 'ops', 'debug', 'raw']" in python_models
    )
    # No empty model was emitted for the enum component.
    assert "class ViewEnum(BaseModel)" not in python_models
    # The referencing field resolves to the alias name.
    assert "view: ViewEnum" in python_models
    # Alias precedes the model that references it.
    assert python_models.index("ViewEnum: TypeAlias") < python_models.index(
        "class UsesEnumPayload"
    )

    assert 'export type ViewEnum = "user" | "ops" | "debug" | "raw";' in dts_types
    assert "export interface ViewEnum" not in dts_types
    assert "view: ViewEnum;" in dts_types


def test_ui_openapi_generator_rejects_mixed_enum_object_schema() -> None:
    components = [
        ComponentSchema(
            "MixedEnumPayload",
            {
                "enum": ["a", "b"],
                "properties": {"kind": {"const": "mixed"}},
            },
        ),
    ]

    with pytest.raises(ValueError, match="must not mix enum with properties"):
        render_python_models(components)


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
    assert (
        resolve_type(
            {
                "type": ["string", "null"],
                "enum": [
                    "validation_artifacts",
                    "session_evidence",
                    "diagnostics",
                    None,
                ],
            }
        )
        == "Literal['validation_artifacts', 'session_evidence', 'diagnostics'] | None"
    )


def test_ui_openapi_generator_orders_composed_union_aliases() -> None:
    components = [
        ComponentSchema(
            "OuterPayload",
            {
                "anyOf": [
                    {"$ref": "#/components/schemas/ZInnerPayload"},
                    {"type": "null"},
                ]
            },
        ),
        ComponentSchema(
            "ZInnerPayload",
            {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        ),
    ]

    python_models = render_python_models(components)

    assert python_models.index("ZInnerPayload: TypeAlias") < python_models.index(
        "OuterPayload: TypeAlias"
    )


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
# ── Browser runtime validators (issue #6337) ─────────────────────────


def test_browser_validators_cover_every_component() -> None:
    """The browser registry must carry the same components as the Python
    and TypeScript artifacts.

    A component missing here is a JSON boundary the browser silently
    cannot validate — the exact gap this layer closes.
    """
    validators_js = Path("src/issue_orchestrator/static/js/ui-contracts.validators.js").read_text()
    python_models = Path("src/issue_orchestrator/contracts/ui_openapi_models.py").read_text()

    for name in (
        "LifecycleCommandPayload",
        "TimelineCommandPayload",
        "RecentE2ERunsPayload",
        "OpenE2ERunCommandPayload",
    ):
        assert f'"{name}"' in validators_js, f"{name} missing from the browser schema registry"

    # Every generated Pydantic model has a browser counterpart.
    model_names = re.findall(r"^class (\w+)\(BaseModel\):", python_models, flags=re.MULTILINE)
    assert model_names, "expected generated Pydantic models"
    missing = [name for name in model_names if f'"{name}": {{' not in validators_js]
    assert missing == [], f"components missing from browser validators: {missing}"


def test_browser_validators_keep_numeric_and_enum_constraints() -> None:
    """Constraints that make a payload check meaningful must survive the
    projection into the browser registry.

    Without this, ``ui-contracts.validators.js`` could ship a schema that
    validates the *shape* of ``run_id`` while dropping ``minimum: 1`` —
    silently accepting run 0 in the browser while the server rejects it.
    """
    components = [
        ComponentSchema(
            "RunPayload",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["run_id"],
                "properties": {
                    "run_id": {"type": "integer", "minimum": 1, "description": "prose"},
                    "view": {"enum": ["user", "ops"]},
                },
            },
        ),
    ]
    rendered = render_js_validators(components)

    assert '"minimum": 1' in rendered
    assert '"enum"' in rendered
    assert '"required"' in rendered
    assert '"additionalProperties": false' in rendered
    # Prose is dropped: it carries no runtime constraint.
    assert "prose" not in rendered
    assert '"description"' not in rendered


def test_validation_projection_rejects_unenforced_keywords() -> None:
    """Adding a JSON Schema keyword the browser engine cannot enforce must
    fail generation, not silently under-validate.

    A generated validator that ignores, say, ``uniqueItems`` would claim to
    enforce the contract while quietly letting violations through — worse
    than having no validator, because callers would trust it.
    """
    with pytest.raises(ValueError, match="uniqueItems"):
        validation_projection(
            {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "Thing/properties/names",
        )


def test_validation_projection_drops_prose_but_keeps_nested_constraints() -> None:
    projected = validation_projection(
        {
            "type": "object",
            "description": "prose",
            "properties": {
                "runs": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "default": 3},
                },
            },
        }
    )
    assert projected == {
        "type": "object",
        "properties": {"runs": {"type": "array", "items": {"type": "integer", "minimum": 1}}},
    }


def _browser_validate(schema_name: str, payloads: list[object]) -> list[bool]:
    """Run the generated browser validator over payloads via node."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    assert node, "node runtime is required to cross-check the browser validators"
    script = (
        "const v = require('./src/issue_orchestrator/static/js/ui-contracts.validators.js');"
        "const [name, payloads] = JSON.parse(process.argv[1]);"
        "console.log(JSON.stringify(payloads.map((p) => v.validate(name, p).ok)));"
    )
    result = subprocess.run(
        [node, "-e", script, json.dumps([schema_name, payloads])],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
        check=False,
    )
    assert result.returncode == 0, f"node failed: {result.stderr}"
    return json.loads(result.stdout)


def test_browser_validator_never_accepts_what_python_rejects() -> None:
    """Cross-language parity for the two generated artifacts.

    Both are generated from the same schema, but from *different* renderers
    — nothing but a test forces them to agree. The invariant that matters
    for safety is directional: the browser must never accept a payload the
    server-side model would reject, or the UI would act on data the
    contract considers invalid.

    (The converse is allowed: the browser validator checks wire types
    strictly, while Pydantic's lax mode coerces some scalars. Being
    stricter at the browser boundary is the intended direction.)
    """
    from pydantic import TypeAdapter, ValidationError

    from issue_orchestrator.contracts.ui_openapi_models import TimelineCommandPayload

    payloads: list[object] = [
        # Valid variants.
        {"kind": "open_e2e_run", "label": "Open E2E Run", "run_id": 88},
        {"kind": "open_e2e_run", "label": "Open", "run_id": 1, "expand_run_details": True},
        {"kind": "open_issue_timeline", "label": "T", "issue_number": 7, "scope_kind": "dashboard"},
        {"kind": "switch_e2e_timeline_view", "label": "V", "run_id": 3, "view": "ops"},
        {"kind": "open_completion_record", "label": "C", "path": "/cr.json"},
        {
            "kind": "open_session_recording",
            "label": "S",
            "issue_number": 7,
            "run_dir": "/r",
            "round_index": None,
            "session_role": None,
        },
        # Invalid: both layers must reject.
        {"kind": "not_a_command", "label": "x", "run_id": 1},
        {"kind": "open_e2e_run", "run_id": 88},
        {"kind": "open_e2e_run", "label": "x", "run_id": 0},
        {"kind": "open_e2e_run", "label": "x", "run_id": "88"},
        {"kind": "open_e2e_run", "label": "x", "run_id": True},
        {"kind": "open_e2e_run", "label": "x", "run_id": 1, "extra": "x"},
        {"kind": "switch_e2e_timeline_view", "label": "V", "run_id": 3, "view": "sideways"},
        {},
    ]

    adapter = TypeAdapter(TimelineCommandPayload)
    python_ok: list[bool] = []
    for payload in payloads:
        try:
            adapter.validate_python(payload)
            python_ok.append(True)
        except ValidationError:
            python_ok.append(False)

    browser_ok = _browser_validate("TimelineCommandPayload", payloads)
    assert len(browser_ok) == len(payloads)

    for payload, py_ok, js_ok in zip(payloads, python_ok, browser_ok, strict=True):
        if js_ok:
            assert py_ok, f"browser accepts a payload Python rejects: {payload}"

    # The six canonical payloads above are valid on both sides, and the
    # eight malformed ones are rejected on both sides. Pinning the exact
    # split keeps this test honest: a validator that rejected everything
    # would satisfy the directional check alone.
    assert python_ok[:6] == [True] * 6, "canonical payloads must satisfy the Python contract"
    assert browser_ok[:6] == [True] * 6, "canonical payloads must satisfy the browser contract"
    assert python_ok[6:] == [False] * 8
    assert browser_ok[6:] == [False] * 8
