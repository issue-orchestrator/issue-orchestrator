#!/usr/bin/env python3
"""Guardrails for maintained markdown docs and skills.

Checks:
1. Relative markdown links resolve on disk.
2. Known stale path tokens are not reintroduced in maintained docs.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

CHECK_ROOTS = (
    REPO_ROOT / "docs",
    REPO_ROOT / ".claude" / "skills",
    REPO_ROOT / "src" / "issue_orchestrator",
    REPO_ROOT / "tests",
    REPO_ROOT / "README.md",
    REPO_ROOT / "AGENTS.md",
)

SKIP_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".claude/worktrees",
    "packages/vscode/.vscode-test",
}

STALE_TOKENS = (
    "src/issue_orchestrator/bootstrap.py",
    "REVIEWER_README.md",
    "development/DEBUGGING.md",
    "development/CONTROL_CENTER_LIFECYCLE_CHECKLIST.md",
)


def _iter_markdown_files() -> list[Path]:
    files: list[Path] = []
    for root in CHECK_ROOTS:
        if root.is_file():
            files.append(root)
            continue
        files.extend(root.rglob("*.md"))
    return [
        path for path in files
        if not any(part in str(path) for part in SKIP_PARTS)
    ]


def _check_links(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(errors="ignore")
    for match in MARKDOWN_LINK.finditer(text):
        target = match.group(1).strip().split("#", 1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        resolved = (
            (REPO_ROOT / target.lstrip("/")).resolve()
            if target.startswith("/")
            else (path.parent / target).resolve()
        )
        if not resolved.exists():
            errors.append(f"{path.relative_to(REPO_ROOT)}: broken link -> {target}")
    return errors


def _check_stale_tokens(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(errors="ignore")
    for token in STALE_TOKENS:
        if token in text:
            errors.append(f"{path.relative_to(REPO_ROOT)}: stale reference -> {token}")
    return errors


def main() -> int:
    errors: list[str] = []
    for path in _iter_markdown_files():
        errors.extend(_check_links(path))
        errors.extend(_check_stale_tokens(path))

    if errors:
        print("Markdown documentation checks failed:\n")
        for error in sorted(set(errors)):
            print(f"- {error}")
        return 1

    print("Markdown documentation checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
