"""The run's staged engine source, read as Python (#8700)."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.improver_defects import CodeSite, code_site
from issue_orchestrator.execution.improver_engine_source import RunDirEngineSource

HANDLER = "improver-data/engine-source/src/issue_orchestrator/control/completion_handler.py"
MODULE = ("issue_orchestrator", "control", "completion_handler")
SOURCE = '''"""Completion."""

LIMIT = 3


class CompletionHandler:
    """Finalizes."""

    retries: int = 0

    def finalize(self) -> None:
        self._update()

    def _update(self) -> None:
        if True:
            def nested() -> None:
                raise ValueError("needs_human from pr_pending")
            nested()


try:
    import fast as _fast
except ImportError:
    def fallback() -> None:
        return None
'''


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    handler = run / HANDLER
    handler.parent.mkdir(parents=True)
    handler.write_text(SOURCE)
    (handler.parent / "broken.py").write_text("def (:\n")
    (handler.parent / "notes.txt").write_text("def finalize\n")
    logs = run / "toolbox" / "logs"
    logs.mkdir(parents=True)
    (logs / "orchestrator.log").write_text("def finalize(self) -> None:\n")
    return run


@pytest.mark.parametrize(
    ("line", "quote", "function"),
    [
        (12, "self._update()", ("CompletionHandler", "finalize")),
        # The innermost function, nested in a statement of a method.
        (17, 'raise ValueError("needs_human from pr_pending")', ("CompletionHandler", "_update", "nested")),
        # Counted two lines off: the line that holds the quote decides.
        (14, "self._update()", ("CompletionHandler", "finalize")),
        (25, "return None", ("fallback",)),
        # In a class body or the module, not in a function.
        (9, "retries: int = 0", None),
        (3, "LIMIT = 3", None),
        (999, "self._update()", None),
    ],
)
def test_a_cited_source_line_is_in_the_function_that_encloses_it(
    run_dir: Path, line: int, quote: str, function: tuple[str, ...] | None
) -> None:
    site = RunDirEngineSource(run_dir).enclosing_function(HANDLER, line, quote)

    assert site == (None if function is None else CodeSite(MODULE, function))


@pytest.mark.parametrize(
    "path",
    [
        "toolbox/logs/orchestrator.log",  # not the engine source
        "improver-data/engine-source/src/issue_orchestrator/control/notes.txt",  # not Python
        "improver-data/engine-source/src/issue_orchestrator/control/broken.py",  # does not parse
        "improver-data/engine-source/../../../outside.py",  # outside the run
        "improver-data/engine-source/src/issue_orchestrator/control/missing.py",
    ],
)
def test_anything_but_staged_python_source_is_in_no_function(run_dir: Path, path: str) -> None:
    (run_dir.parent / "outside.py").write_text("def finalize(self) -> None:\n    pass\n")

    assert RunDirEngineSource(run_dir).enclosing_function(path, 1, "def finalize(self) -> None:") is None


def _resolve(source: RunDirEngineSource, owner: str) -> CodeSite | None:
    written = code_site(owner)
    assert written is not None, owner
    return source.resolve(written)


@pytest.mark.parametrize(
    ("owner", "symbol"),
    [
        ("completion_handler:CompletionHandler._update", ("CompletionHandler", "_update")),
        ("control/completion_handler.py:_update", ("CompletionHandler", "_update")),
        ("issue_orchestrator.control.completion_handler:CompletionHandler", ("CompletionHandler",)),
        ("src/issue_orchestrator/control/completion_handler.py:LIMIT", ("LIMIT",)),
        ("completion_handler:fallback", ("fallback",)),
        ("completion_handler:_update.nested", ("CompletionHandler", "_update", "nested")),
    ],
)
def test_an_owner_resolves_to_the_one_definition_it_names_in_full(
    run_dir: Path, owner: str, symbol: tuple[str, ...]
) -> None:
    assert _resolve(RunDirEngineSource(run_dir), owner) == CodeSite(MODULE, symbol)


@pytest.mark.parametrize(
    "owner",
    [
        "completion_handler:Other._update",
        "completion_handler:retries",  # a class attribute, not a definition
        "completion_handler:_fast",  # an import
        "domain/completion_handler.py:CompletionHandler",  # no such module
        "broken:anything",  # does not parse
        "handler:CompletionHandler",  # a module name is matched whole
    ],
)
def test_an_owner_naming_nothing_staged_resolves_to_nothing(run_dir: Path, owner: str) -> None:
    assert _resolve(RunDirEngineSource(run_dir), owner) is None


def test_an_owner_two_definitions_answer_to_resolves_to_nothing(run_dir: Path) -> None:
    """r1 F1: a bare method name two classes define, or a module two staged
    files end with, names no one function."""
    handler = run_dir / HANDLER
    handler.write_text(handler.read_text() + "\n\nclass Other:\n    def finalize(self) -> None:\n        pass\n")
    other = run_dir / "improver-data" / "engine-source" / "src" / "issue_orchestrator" / "domain" / "completion_handler.py"
    other.parent.mkdir(parents=True)
    other.write_text("class CompletionHandler:\n    pass\n")
    source = RunDirEngineSource(run_dir)

    assert _resolve(source, "control.completion_handler:finalize") is None
    assert _resolve(source, "completion_handler:CompletionHandler") is None
    assert _resolve(source, "control.completion_handler:Other.finalize") == CodeSite(MODULE, ("Other", "finalize"))
    assert _resolve(source, "domain.completion_handler:CompletionHandler") == CodeSite(
        ("issue_orchestrator", "domain", "completion_handler"), ("CompletionHandler",)
    )
