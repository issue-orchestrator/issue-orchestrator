"""Guardrail to enforce adapter/port boundaries.

This module prevents non-adapter code (control, entrypoints, observation, domain)
from directly accessing adapter-internal attributes and classes.

The guardrail runs as part of static analysis and flags violations where:
- Code outside execution/ or adapters/ imports adapter-internal classes
- Code accesses private attributes on adapter instances (e.g., github._http_client)
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BoundaryViolation:
    """A detected violation of adapter/port boundaries."""

    file_path: str
    line_number: int
    violation_type: str  # "import" or "attribute_access"
    message: str
    code_snippet: str


@dataclass(frozen=True)
class AdapterBoundaryResult:
    """Result of boundary guardrail check."""

    status: str  # "ok", "fail", "error"
    violations: list[BoundaryViolation]
    reason: Optional[str] = None


# Adapter-internal classes that should not be imported outside execution/adapters
ADAPTER_INTERNALS = {
    "GitHubHttpClient",
    "GitHubCache",
    "GitHubIssueResolver",
    "HTTPError",
    "HTTPException",
}

# Packages that are allowed to access adapter internals
ALLOWED_PACKAGES = {
    "issue_orchestrator.execution",
    "issue_orchestrator.adapters",
}

# Packages where violations should be checked
VIOLATION_CHECK_PACKAGES = {
    "issue_orchestrator.control",
    "issue_orchestrator.entrypoints",
    "issue_orchestrator.observation",
    "issue_orchestrator.domain",
    "issue_orchestrator.infra",
}


def _get_module_package(file_path: Path) -> str:
    """Determine the module package from file path."""
    try:
        # Convert path to module name
        parts = file_path.relative_to(
            file_path.parent.parent.parent / "issue_orchestrator"
        ).parts
        parts = parts[:-1]  # Remove .py filename
        if not parts:
            return ""
        return "issue_orchestrator." + ".".join(parts)
    except (ValueError, IndexError):
        return ""


def _should_check_file(module_package: str) -> bool:
    """Check if this file should be analyzed for violations."""
    if not module_package:
        return False
    return any(module_package.startswith(pkg) for pkg in VIOLATION_CHECK_PACKAGES)


def _is_adapter_internal_import(node: ast.ImportFrom | ast.Import) -> Optional[str]:
    """Check if an import statement imports adapter-internal classes."""
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if not any(module.startswith(pkg) for pkg in ALLOWED_PACKAGES):
            return None

        for alias in node.names:
            if alias.name in ADAPTER_INTERNALS:
                return alias.name
    elif isinstance(node, ast.Import):
        for alias in node.names:
            if any(
                c in ADAPTER_INTERNALS
                for c in alias.name.split(".")
            ):
                return alias.name

    return None


def _check_file(file_path: Path, source_code: str) -> list[BoundaryViolation]:
    """Check a single file for boundary violations."""
    violations = []

    module_package = _get_module_package(file_path)
    if not _should_check_file(module_package):
        return violations

    try:
        tree = ast.parse(source_code, filename=str(file_path))
    except SyntaxError as e:
        logger.debug("Syntax error in %s: %s", file_path, e)
        return violations

    lines = source_code.split("\n")

    for node in ast.walk(tree):
        # Check for imports of adapter internals
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if hasattr(node, "lineno") and hasattr(node, "col_offset"):
                internal = _is_adapter_internal_import(node)
                if internal:
                    line_num = node.lineno
                    code_line = (
                        lines[line_num - 1] if line_num <= len(lines) else ""
                    )
                    violations.append(
                        BoundaryViolation(
                            file_path=str(file_path),
                            line_number=line_num,
                            violation_type="import",
                            message=f"Importing adapter-internal class '{internal}' outside execution/adapters",
                            code_snippet=code_line.strip(),
                        )
                    )

        # Check for direct attribute access on known adapters
        if isinstance(node, ast.Attribute):
            if hasattr(node, "lineno") and node.attr.startswith("_"):
                # Check if this is accessing a private attribute
                # We look for patterns like github._http_client
                if isinstance(node.value, ast.Name):
                    line_num = node.lineno
                    code_line = (
                        lines[line_num - 1] if line_num <= len(lines) else ""
                    )
                    # Only flag obvious violations on known adapter vars
                    if node.value.id in {"github", "repository_host"}:
                        violations.append(
                            BoundaryViolation(
                                file_path=str(file_path),
                                line_number=line_num,
                                violation_type="attribute_access",
                                message=f"Accessing private attribute '{node.attr}' on adapter instance",
                                code_snippet=code_line.strip(),
                            )
                        )

    return violations


def check_adapter_boundaries(
    source_dir: Path,
    scope_patterns: list[str] | None = None,
) -> AdapterBoundaryResult:
    """Check all Python files in a directory for adapter boundary violations.

    Args:
        source_dir: Root directory to scan (usually src/issue_orchestrator/)
        scope_patterns: Optional glob patterns to limit which files to check

    Returns:
        AdapterBoundaryResult with list of violations found
    """
    if not source_dir.exists():
        reason: str = f"Source directory not found: {source_dir}"
        return AdapterBoundaryResult(
            status="error",
            violations=[],
            reason=reason,
        )

    violations = []

    # Determine which files to check
    py_files = sorted(source_dir.glob("**/*.py"))
    if scope_patterns:
        filtered = []
        for py_file in py_files:
            for pattern in scope_patterns:
                if py_file.match(pattern):
                    filtered.append(py_file)
                    break
        py_files = filtered

    for py_file in py_files:
        try:
            source_code = py_file.read_text(encoding="utf-8")
            file_violations = _check_file(py_file, source_code)
            violations.extend(file_violations)
        except (IOError, OSError) as e:
            logger.debug("Error reading %s: %s", py_file, e)
            continue

    if violations:
        return AdapterBoundaryResult(
            status="fail",
            violations=violations,
            reason=f"Found {len(violations)} adapter boundary violation(s)",
        )

    return AdapterBoundaryResult(status="ok", violations=[])
