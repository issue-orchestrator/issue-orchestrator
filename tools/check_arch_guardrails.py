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


def check_file(path: Path, rules: dict, allow_prefixes: Sequence[str]) -> list[Violation]:
    if is_allowed(path, allow_prefixes):
        return []

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
    except SyntaxError as e:
        return [Violation(path.as_posix(), e.lineno or 1, e.offset or 0, "syntax", e.msg)]

    violations: list[Violation] = []
    deny_imports = set(rules.get("deny_imports", []) or [])
    deny_dynamic_imports = set(rules.get("deny_dynamic_imports", []) or [])
    deny_calls = set(tuple(x.split(".", 1)) for x in (rules.get("deny_calls", []) or []))
    deny_os_calls = bool(rules.get("deny_os_system_like", True))
    deny_dynamic_any = set(rules.get("deny_dynamic_any", []) or [])

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if base_mod(alias.name) in deny_imports:
                    violations.append(
                        Violation(path.as_posix(), node.lineno, node.col_offset, "import", f"import {alias.name}")
                    )

        if isinstance(node, ast.ImportFrom):
            if node.module and base_mod(node.module) in deny_imports:
                violations.append(
                    Violation(
                        path.as_posix(),
                        node.lineno,
                        node.col_offset,
                        "import",
                        f"from {node.module} import ...",
                    )
                )

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                mod = base_mod(node.args[0].value)
                if mod in deny_dynamic_imports or mod in deny_imports:
                    violations.append(
                        Violation(
                            path.as_posix(),
                            node.lineno,
                            node.col_offset,
                            "dynamic-import",
                            f'__import__("{node.args[0].value}")',
                        )
                    )
            elif "__import__" in deny_dynamic_any:
                violations.append(
                    Violation(path.as_posix(), node.lineno, node.col_offset, "dynamic-import", "__import__(...)")
                )

        if isinstance(node, ast.Call):
            m, a = get_attr_call(node.func)
            if (m, a) == ("importlib", "import_module"):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    mod = base_mod(node.args[0].value)
                    if mod in deny_dynamic_imports or mod in deny_imports:
                        violations.append(
                            Violation(
                                path.as_posix(),
                                node.lineno,
                                node.col_offset,
                                "dynamic-import",
                                f'importlib.import_module("{node.args[0].value}")',
                            )
                        )
                elif "importlib.import_module" in deny_dynamic_any:
                    violations.append(
                        Violation(
                            path.as_posix(),
                            node.lineno,
                            node.col_offset,
                            "dynamic-import",
                            "importlib.import_module(...)",
                        )
                    )

            if m and a and (m, a) in deny_calls:
                violations.append(
                    Violation(path.as_posix(), node.lineno, node.col_offset, "call", f"{m}.{a}(...)")
                )

            if deny_os_calls and m == "os" and a in {"system", "popen"}:
                violations.append(
                    Violation(path.as_posix(), node.lineno, node.col_offset, "call", f"os.{a}(...)")
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
