"""Guardrail: control layer must not construct TraceEvent directly."""

from __future__ import annotations

import ast
from pathlib import Path


def _is_trace_event_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "TraceEvent"
    if isinstance(func, ast.Attribute):
        return func.attr == "TraceEvent"
    return False


def test_control_layer_does_not_construct_trace_event_directly() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    control_root = repo_root / "src" / "issue_orchestrator" / "control"
    violations: list[str] = []

    for py_file in sorted(control_root.rglob("*.py")):
        module = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _is_trace_event_call(node):
                rel = py_file.relative_to(repo_root)
                violations.append(f"{rel}:{node.lineno}")

    assert not violations, (
        "Control layer must construct events via make_trace_event/make_run_scoped_event wrappers. "
        "Direct TraceEvent(...) calls found:\n" + "\n".join(violations)
    )
