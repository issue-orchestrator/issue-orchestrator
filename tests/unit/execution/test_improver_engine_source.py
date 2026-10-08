"""The run's staged engine source, read as Python (#8700)."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.execution.improver_engine_source import RunDirEngineSource

HANDLER = "improver-data/engine-source/src/issue_orchestrator/control/completion_handler.py"
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
    assert RunDirEngineSource(run_dir).enclosing_function(HANDLER, line, quote) == function


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


def test_a_module_defines_its_classes_functions_and_module_names_by_qualified_name(run_dir: Path) -> None:
    source = RunDirEngineSource(run_dir)

    for module in (("completion_handler",), ("control", "completion_handler"),
                   ("issue_orchestrator", "control", "completion_handler")):
        assert source.defines(module, ("CompletionHandler", "_update"))
    for symbol in (("_update",), ("CompletionHandler",), ("LIMIT",), ("fallback",), ("_update", "nested")):
        assert source.defines(("completion_handler",), symbol), symbol
    for symbol in (("Other", "_update"), ("retries",), ("_fast",), ("missing",)):
        assert not source.defines(("completion_handler",), symbol), symbol
    assert not source.defines(("domain", "completion_handler"), ("CompletionHandler",))
    assert not source.defines(("broken",), ("anything",))


def test_a_module_two_staged_files_end_with_defines_nothing(run_dir: Path) -> None:
    other = run_dir / "improver-data" / "engine-source" / "src" / "issue_orchestrator" / "domain" / "completion_handler.py"
    other.parent.mkdir(parents=True)
    other.write_text("class CompletionHandler:\n    pass\n")
    source = RunDirEngineSource(run_dir)

    assert not source.defines(("completion_handler",), ("CompletionHandler",))
    assert source.defines(("domain", "completion_handler"), ("CompletionHandler",))
