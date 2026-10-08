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
from collections import Counter
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
        span = _quoted_span(lines, line, quote)
        if span is None:
            return None
        # The quote's own lines decide (r5 F2), and they must be in one function.
        first, last = (_function_at(tree, at) for at in span)
        if first is None or first != last:
            return None
        # A name defined twice (r4 F2: `def run` in each branch of an `if`)
        # names two functions: no one site.
        return CodeSite(module=module, symbol=first) if _defined_names(tree)[first] == 1 else None

    def resolve(self, written: CodeSite) -> CodeSite | None:
        # A file staged under two names (a symlink) is one module (r2 F4).
        files = {path for dotted, path in self._modules if _ends_with(dotted, written.module)}
        if len(files) != 1:
            return None
        [path] = files
        parsed, module = self._parse(path), self._module_of(path)
        if parsed is None or module is None:
            return None
        # Each definition counts: a name defined twice is two (r4 F2).
        symbols = [q for q, n in _defined_names(parsed[0]).items() for _ in range(n) if _ends_with(q, written.symbol)]
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
            # Lines end at "\n" only, as the citation validator numbers them.
            return ast.parse(text), text.split("\n")
        except SyntaxError:
            return None


def _ends_with(path: tuple[str, ...], tail: tuple[str, ...]) -> bool:
    return 0 < len(tail) <= len(path) and path[len(path) - len(tail):] == tail


def _quoted_span(lines: list[str], line: int, quote: str) -> tuple[int, int] | None:
    """The first and last line of the shortest run of lines, within the
    citation slack of ``line``, that holds ``quote`` (the validator matches
    a quote across the lines of that window, so it may span several), the
    nearest to ``line`` of those; None when no run holds it."""
    wanted = normalized(quote)
    low, high = max(1, line - LINE_SLACK), min(len(lines), line + LINE_SLACK)
    spans = [
        (first, last)
        for first in range(low, high + 1)
        for last in range(first, high + 1)
        if wanted in normalized(" ".join(lines[first - 1:last]))
    ]
    return min(spans, key=lambda s: (s[1] - s[0], abs(s[0] - line)), default=None)


def _function_at(tree: ast.Module, line: int) -> tuple[str, ...] | None:
    """The qualified name of the innermost function whose body holds
    ``line``; None in a class's body or the module's."""
    chain: list[ast.AST] = []
    scope: ast.AST = tree
    while (inner := _child_scope(scope, line)) is not None:
        chain.append(inner)
        scope = inner
    if not chain or not isinstance(chain[-1], _FUNCTION):
        return None
    return tuple(node.name for node in chain if isinstance(node, _SCOPE))


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


def _defined_names(tree: ast.Module) -> Counter[tuple[str, ...]]:
    """How many times each class's and function's qualified name, and each
    module-level name, is defined."""
    names: Counter[tuple[str, ...]] = Counter()

    def visit(scope: ast.AST, prefix: tuple[str, ...]) -> None:
        for node in ast.iter_child_nodes(scope):
            if isinstance(node, _SCOPE):
                names[(*prefix, node.name)] += 1
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
