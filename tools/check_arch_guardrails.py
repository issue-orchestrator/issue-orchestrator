#!/usr/bin/env python3
# AST guardrails checker (agent-hostile backstop).
#
# Reads tools/ast_guardrails.yml for rules.
# Detects:
# - forbidden imports
# - forbidden dynamic imports (__import__, importlib.import_module)
# - forbidden calls (subprocess.run, os.system, etc.)
#
# Exit codes: 0 OK, 2 violations, 1 unexpected error

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

import yaml


@dataclass(frozen=True)
class Violation:
    path: str
    lineno: int
    col: int
    kind: str
    detail: str

    def fmt(self) -> str:
        return f"{self.path}:{self.lineno}:{self.col} [{self.kind}] {self.detail}"


def iter_py_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*.py"):
        parts = set(p.parts)
        if "__pycache__" in parts or ".venv" in parts or "venv" in parts or ".tox" in parts:
            continue
        yield p


def base_mod(name: str) -> str:
    return name.split(".", 1)[0]


def is_allowed(path: Path, allow_prefixes: Sequence[str]) -> bool:
    p = path.as_posix()
    return any(p.startswith(prefix.rstrip("/")) for prefix in allow_prefixes)


def get_attr_call(node: ast.AST) -> Tuple[Optional[str], Optional[str]]:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id, node.attr
    return None, None


def load_rules(rules_path: Path) -> dict:
    with rules_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("ast_guardrails.yml must contain a mapping at top level")
    return data


def _is_git_subprocess_call(node: ast.Call) -> bool:
    m, a = get_attr_call(node.func)
    if not (m and a) or m != "subprocess":
        return False
    if a not in {"run", "Popen", "call", "check_call", "check_output"}:
        return False
    if not node.args:
        return False
    arg0 = node.args[0]
    if not isinstance(arg0, (ast.List, ast.Tuple)) or not arg0.elts:
        return False
    first = arg0.elts[0]
    return isinstance(first, ast.Constant) and first.value == "git"


def matches_module(import_name: str, deny_patterns: Sequence[str]) -> Optional[str]:
    """Check if an import matches any of the deny patterns.

    Returns the matching pattern if found, None otherwise.
    """
    for pattern in deny_patterns:
        # Match if import starts with pattern (e.g., 'issue_orchestrator.adapters' matches 'issue_orchestrator.adapters.github')
        if import_name == pattern or import_name.startswith(pattern + "."):
            return pattern
    return None


def resolve_relative_import(path: Path, module: Optional[str], level: int) -> Optional[str]:
    """Resolve a relative import to an absolute module name.

    Args:
        path: Path to the file containing the import
        module: The module name from ast.ImportFrom (e.g., 'adapters.github')
        level: Number of dots (e.g., 2 for 'from ..adapters')

    Returns:
        Absolute module name (e.g., 'issue_orchestrator.adapters.github')
    """
    if level == 0:
        return module

    # Convert path to module parts
    # e.g., 'src/issue_orchestrator/entrypoints/web.py' -> ['issue_orchestrator', 'entrypoints', 'web']
    parts = list(path.with_suffix("").parts)

    # Find 'issue_orchestrator' in path and use that as the root
    try:
        root_idx = parts.index("issue_orchestrator")
        parts = parts[root_idx:]  # Start from issue_orchestrator
    except ValueError:
        return module  # Can't resolve, return as-is

    # Remove 'level' number of parts from the end (for the dots)
    # level=1 means current package, level=2 means parent, etc.
    if level > len(parts):
        return module
    parts = parts[: -level] if level > 0 else parts

    # Append the imported module
    if module:
        parts.extend(module.split("."))

    return ".".join(parts)


def check_layer_boundaries(path: Path, tree: ast.AST, rules: dict) -> list[Violation]:
    """Check layer boundary rules (e.g., entrypoints cannot import adapters)."""
    violations: list[Violation] = []
    layer_rules = rules.get("layer_boundaries", []) or []

    for rule in layer_rules:
        deny_in = rule.get("deny_in", []) or []
        deny_imports = rule.get("deny_imports", []) or []
        allow = rule.get("allow", []) or []
        rule_name = rule.get("name", "layer-boundary")

        # Check if this file is in a denied path
        p = path.as_posix()
        in_denied_path = any(p.startswith(prefix.rstrip("/")) for prefix in deny_in)

        # Check if this file is explicitly allowed
        is_allowed_file = any(p == allowed or p.startswith(allowed.rstrip("/") + "/") for allowed in allow)

        if not in_denied_path or is_allowed_file:
            continue

        # Check imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    match = matches_module(alias.name, deny_imports)
                    if match:
                        violations.append(
                            Violation(
                                path.as_posix(),
                                node.lineno,
                                node.col_offset,
                                rule_name,
                                f"import {alias.name} (forbidden: {match})",
                            )
                        )

            if isinstance(node, ast.ImportFrom):
                # Resolve relative imports to absolute module names
                resolved = resolve_relative_import(path, node.module, node.level)
                if resolved:
                    match = matches_module(resolved, deny_imports)
                    if match:
                        # Format the import for display
                        dots = "." * node.level
                        display_module = f"{dots}{node.module}" if node.module else dots
                        violations.append(
                            Violation(
                                path.as_posix(),
                                node.lineno,
                                node.col_offset,
                                rule_name,
                                f"from {display_module} import ... (forbidden: {match})",
                            )
                        )

    return violations


def check_attr_call_rules(path: Path, tree: ast.AST, rules: dict) -> list[Violation]:
    """Check attribute call rules (e.g., disallow get_issue_labels in control)."""
    violations: list[Violation] = []
    attr_rules = rules.get("deny_attr_calls", []) or []

    for rule in attr_rules:
        deny_in = rule.get("deny_in", []) or []
        deny_attr_names = set(rule.get("deny_attr_names", []) or [])
        allow = rule.get("allow", []) or []
        rule_name = rule.get("name", "deny-attr-call")

        p = path.as_posix()
        in_denied_path = any(p.startswith(prefix.rstrip("/")) for prefix in deny_in)
        is_allowed_file = any(p == allowed or p.startswith(allowed.rstrip("/") + "/") for allowed in allow)

        if not in_denied_path or is_allowed_file:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in deny_attr_names:
                    violations.append(
                        Violation(
                            path.as_posix(),
                            node.lineno,
                            node.col_offset,
                            rule_name,
                            f"{node.func.attr}(...)",
                        )
                    )

    return violations


def check_file(path: Path, rules: dict, allow_prefixes: Sequence[str]) -> list[Violation]:
    allow_general = is_allowed(path, allow_prefixes)

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
    except SyntaxError as e:
        return [Violation(path.as_posix(), e.lineno or 1, e.offset or 0, "syntax", e.msg)]

    violations: list[Violation] = []

    # Check layer boundary rules first
    violations.extend(check_layer_boundaries(path, tree, rules))
    violations.extend(check_attr_call_rules(path, tree, rules))

    deny_imports = set(rules.get("deny_imports", []) or [])
    deny_dynamic_imports = set(rules.get("deny_dynamic_imports", []) or [])
    deny_calls = set(tuple(x.split(".", 1)) for x in (rules.get("deny_calls", []) or []))
    deny_os_calls = bool(rules.get("deny_os_system_like", True))
    deny_dynamic_any = set(rules.get("deny_dynamic_any", []) or [])
    deny_git_subprocess = bool(rules.get("deny_git_subprocess", False))
    allow_git_prefixes = rules.get("allow_git_subprocess_prefixes", []) or []
    allow_git_subprocess = is_allowed(path, allow_git_prefixes)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not allow_general and base_mod(alias.name) in deny_imports:
                    violations.append(
                        Violation(path.as_posix(), node.lineno, node.col_offset, "import", f"import {alias.name}")
                    )

        if isinstance(node, ast.ImportFrom):
            if not allow_general and node.module and base_mod(node.module) in deny_imports:
                violations.append(
                    Violation(
                        path.as_posix(),
                        node.lineno,
                        node.col_offset,
                        "import",
                        f"from {node.module} import ...",
                    )
                )

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
        ):
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                mod = base_mod(node.args[0].value)
                if not allow_general and (mod in deny_dynamic_imports or mod in deny_imports):
                    violations.append(
                        Violation(
                            path.as_posix(),
                            node.lineno,
                            node.col_offset,
                            "dynamic-import",
                            f'__import__("{node.args[0].value}")',
                        )
                    )
            elif not allow_general and "__import__" in deny_dynamic_any:
                violations.append(
                    Violation(path.as_posix(), node.lineno, node.col_offset, "dynamic-import", "__import__(...)")
                )

        if isinstance(node, ast.Call):
            m, a = get_attr_call(node.func)
            if (m, a) == ("importlib", "import_module"):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    mod = base_mod(node.args[0].value)
                    if not allow_general and (mod in deny_dynamic_imports or mod in deny_imports):
                        violations.append(
                            Violation(
                                path.as_posix(),
                                node.lineno,
                                node.col_offset,
                                "dynamic-import",
                                f'importlib.import_module("{node.args[0].value}")',
                            )
                        )
                elif not allow_general and "importlib.import_module" in deny_dynamic_any:
                    violations.append(
                        Violation(
                            path.as_posix(),
                            node.lineno,
                            node.col_offset,
                            "dynamic-import",
                            "importlib.import_module(...)",
                        )
                    )

            if not allow_general and m and a and (m, a) in deny_calls:
                violations.append(
                    Violation(path.as_posix(), node.lineno, node.col_offset, "call", f"{m}.{a}(...)")
                )

            if not allow_general and deny_os_calls and m == "os" and a in {"system", "popen"}:
                violations.append(
                    Violation(path.as_posix(), node.lineno, node.col_offset, "call", f"os.{a}(...)")
                )

            if deny_git_subprocess and not allow_git_subprocess and _is_git_subprocess_call(node):
                violations.append(
                    Violation(path.as_posix(), node.lineno, node.col_offset, "git-subprocess", "subprocess.*(['git', ...])")
                )

    return violations


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default="src", help="Root to scan (default: src)")
    ap.add_argument("--rules", default="tools/ast_guardrails.yml", help="Rules file path")
    ap.add_argument(
        "--allow-prefix",
        action="append",
        default=[],
        help="Allowed path prefix (repeatable)",
    )
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.exists():
        print(f"Root not found: {root}", file=sys.stderr)
        return 1

    rules = load_rules(Path(args.rules))
    allow_prefixes = args.allow_prefix or (rules.get("allow_prefixes", []) or [])
    if not allow_prefixes:
        allow_prefixes = [
            "src/issue_orchestrator/execution",
            "src/issue_orchestrator/adapters",
        ]

    all_v: list[Violation] = []
    for p in iter_py_files(root):
        all_v.extend(check_file(p, rules, allow_prefixes))

    if all_v:
        print("Architecture guardrails violations:\n", file=sys.stderr)
        for v in sorted(all_v, key=lambda x: (x.path, x.lineno, x.col, x.kind)):
            print(v.fmt(), file=sys.stderr)
        print(
            "\nFix: move side effects into allowed adapters/runners, or explicitly allow a folder via allow_prefixes.",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
