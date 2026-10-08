"""The run's staged engine source, read as Python (#8700).

:class:`RunDirEngineSource` implements
:class:`~..domain.improver_defects.EngineSource` over one run directory's
``improver-data/engine-source/``: which function encloses a cited source
line, and which one definition an owner names; both named in full. It reads only that tree (after
every symlink), and only ``.py`` files that parse; anything else answers
"no function", so it never relates two findings.
"""

from __future__ import annotations

import ast
from functools import cache, cached_property
from pathlib import Path

from ..contracts.improver_inputs import ENGINE_SOURCE_DIRNAME, IMPROVER_DATA_DIRNAME
from ..domain.improver_citations import LINE_SLACK, normalized
from ..domain.improver_defects import CodeSite, module_path

_FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPE = (*_FUNCTION, ast.ClassDef)


class RunDirEngineSource:
    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir.resolve()
        self._root = (self._run_dir / IMPROVER_DATA_DIRNAME / ENGINE_SOURCE_DIRNAME).resolve()
        self._parse = cache(self._parsed)

    def enclosing_function(self, path: str, line: int, quote: str) -> CodeSite | None:
        target = (self._run_dir / path).resolve()
        if not target.is_relative_to(self._root):
            return None
        parsed = self._parse(target)
        module = self._module_of(target)
        if parsed is None or module is None:
            return None
        tree, lines = parsed
        at = _quoted_line(lines, line, quote)
        chain: list[ast.AST] = []
        scope: ast.AST = tree
        while (inner := _child_scope(scope, at)) is not None:
            chain.append(inner)
            scope = inner
        # In a function's body, not a class's or the module's.
        if not chain or not isinstance(chain[-1], _FUNCTION):
            return None
        return CodeSite(module=module, symbol=tuple(node.name for node in chain if isinstance(node, _SCOPE)))

    def resolve(self, written: CodeSite) -> CodeSite | None:
        # A file staged under two names (a symlink) is one module (r2 F4).
        files = {path for dotted, path in self._modules if _ends_with(dotted, written.module)}
        if len(files) != 1:
            return None
        [path] = files
        parsed, module = self._parse(path), self._module_of(path)
        if parsed is None or module is None:
            return None
        symbols = [q for q in _defined_names(parsed[0]) if _ends_with(q, written.symbol)]
        return CodeSite(module=module, symbol=symbols[0]) if len(symbols) == 1 else None

    def _module_of(self, resolved: Path) -> tuple[str, ...] | None:
        """A staged file's one module identity: its RESOLVED path's, so a
        symlink and its target are one module."""
        return module_path(resolved.relative_to(self._root).as_posix())

    @cached_property
    def _modules(self) -> tuple[tuple[tuple[str, ...], Path], ...]:
        """Every staged Python file inside the source tree, under each name
        it is staged as, with the file it resolves to."""
        found = []
        for path in self._root.rglob("*.py"):
            resolved = path.resolve()
            dotted = module_path(path.relative_to(self._root).as_posix())
            if dotted is not None and resolved.is_relative_to(self._root) and resolved.is_file():
                found.append((dotted, resolved))
        return tuple(found)

    def _parsed(self, target: Path) -> tuple[ast.Module, list[str]] | None:
        if target.suffix != ".py" or not target.is_relative_to(self._root) or not target.is_file():
            return None
        text = target.read_text(encoding="utf-8", errors="replace")
        try:
            return ast.parse(text), text.splitlines()
        except SyntaxError:
            return None


def _ends_with(path: tuple[str, ...], tail: tuple[str, ...]) -> bool:
    return 0 < len(tail) <= len(path) and path[len(path) - len(tail):] == tail


def _quoted_line(lines: list[str], line: int, quote: str) -> int:
    """The line within the citation slack of ``line`` that holds ``quote``
    (agents count lines inexactly), else ``line`` itself."""
    wanted = normalized(quote)
    for candidate in sorted(range(line - LINE_SLACK, line + LINE_SLACK + 1), key=lambda n: abs(n - line)):
        if 1 <= candidate <= len(lines) and wanted in normalized(lines[candidate - 1]):
            return candidate
    return line


def _child_scope(scope: ast.AST, line: int) -> ast.AST | None:
    """The class or function directly in ``scope`` whose span holds ``line``."""
    for node in ast.iter_child_nodes(scope):
        found = (
            node if isinstance(node, _SCOPE) and node.lineno <= line <= (node.end_lineno or node.lineno)
            else None if isinstance(node, _SCOPE)
            else _child_scope(node, line)  # a class or def nested in an if, try or with
        )
        if found is not None:
            return found
    return None


def _defined_names(tree: ast.Module) -> set[tuple[str, ...]]:
    """Every class's and function's qualified name, and each module-level name."""
    names: set[tuple[str, ...]] = set()

    def visit(scope: ast.AST, prefix: tuple[str, ...]) -> None:
        for node in ast.iter_child_nodes(scope):
            if isinstance(node, _SCOPE):
                names.add((*prefix, node.name))
                visit(node, (*prefix, node.name))
            else:
                visit(node, prefix)

    visit(tree, ())
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign)
            else []
        )
        names.update((t.id,) for t in targets if isinstance(t, ast.Name))
    return names


__all__ = ["RunDirEngineSource"]
