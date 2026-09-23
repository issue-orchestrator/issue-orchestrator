"""Every WorkingCopy reports a missing branch the same way (#7263 / #7269 F3).

``WorkingCopy.get_current_branch`` contracts to return ``str | None`` -- ``None``
on a detached HEAD or a read failure. Callers code against exactly that: the
review-outcome and processing-completed emitters record no branch when it is
``None`` and deliberately carry no defensive ``except``, because the port already
promises not to raise.

A stub that raises instead is STRICTER than the contract its callers were written
for, so a caller handling ``None`` correctly still aborts. That is what the
simulated-scenario stub did, and on a detached checkout it would have failed an
already-decided completion.

Two earlier versions of this file were weaker than they looked:

* the first asserted on ``inspect.signature(...).return_annotation`` -- theatre,
  because an implementation can keep the ``str | None`` annotation and still
  raise;
* the second invoked the implementations, but found them by importing every
  module under ``issue_orchestrator`` and ``tests``. That import sweep saw only
  module-level classes (missing four nested definitions), swallowed import
  errors, skipped anything needing constructor arguments -- including the
  production ``GitRevisionReader`` -- and booted real adapters as a side effect.

This version separates the two jobs. A **static** AST guard enumerates every
``get_current_branch`` definition in the tree without importing anything, and
fails when one is not accounted for below. A **deterministic registry** then
constructs each implementation -- injecting real dependencies where the
constructor needs them -- and invokes it. Nothing is skipped: an import or
construction failure fails its test.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.swept_sources import swept_source_files_in
from issue_orchestrator.ports.working_copy import WorkingCopy

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SWEPT_TREES = ("src/issue_orchestrator", "tests")


def _import(module: str) -> Any:
    import importlib

    return importlib.import_module(module)


def _factories() -> dict[str, Callable[[], object]]:
    """Every implementation this contract is enforced against, by dotted name.

    Imports live inside the factories so that a module that stops importing
    fails the test that needs it rather than breaking collection for all of
    them, and so that enumerating the registry costs nothing.
    """

    def _git_working_copy() -> object:
        from issue_orchestrator.execution.git_working_copy import GitWorkingCopy

        return GitWorkingCopy()

    def _git_revision_reader() -> object:
        from issue_orchestrator.adapters.git.git_cli import GitCLI
        from issue_orchestrator.execution.command_runner import LocalCommandRunner
        from issue_orchestrator.execution.git_revision_reader import GitRevisionReader
        from issue_orchestrator.ports.git import GitResult

        # The production dependency: GitWorkingCopy builds this reader over a
        # GitCLI on a LocalCommandRunner, so that is what it is given here. A
        # stubbed runner would test the stub -- and the defect this contract
        # exists for is a real git read failing on a real unreadable checkout.
        git = GitCLI(runner=LocalCommandRunner())

        def run(worktree: Path, args: list[str], *, check: bool = True) -> GitResult:
            return git.run(worktree, args, check=check)

        return GitRevisionReader(run)

    def _named(
        module: str, *path: str, arguments: tuple[object, ...] = ()
    ) -> Callable[[], object]:
        """Build one implementation by dotted path, with its dependencies.

        ``arguments`` is what the import sweep had no way to supply, so it
        skipped every constructor-dependent implementation instead -- including
        the production revision reader. Stating them here is the point.
        """

        def factory() -> object:
            target: object = _import(module)
            for attribute in path:
                target = getattr(target, attribute)
            assert callable(target)
            return target(*arguments)

        return factory

    return {
        "issue_orchestrator.execution.git_working_copy.GitWorkingCopy": _git_working_copy,
        "issue_orchestrator.execution.git_revision_reader.GitRevisionReader": _git_revision_reader,
        "tests.integration.test_completion_command_contracts._NoopGitAdapter": _named(
            "tests.integration.test_completion_command_contracts", "_NoopGitAdapter"
        ),
        "tests.integration.test_completion_command_contracts._DirtyGit": _named(
            "tests.integration.test_completion_command_contracts", "_DirtyGit"
        ),
        "tests.integration.test_orchestrator_completion.StubWorkingCopy": _named(
            "tests.integration.test_orchestrator_completion", "StubWorkingCopy"
        ),
        "tests.integration.test_session_output.DummyGitAdapter": _named(
            "tests.integration.test_session_output", "DummyGitAdapter"
        ),
        "tests.integration.test_timeout_flow.StubWorkingCopy": _named(
            "tests.integration.test_timeout_flow", "StubWorkingCopy"
        ),
        "tests.simulated_scenarios.conftest.StubWorkingCopy": _named(
            "tests.simulated_scenarios.conftest", "StubWorkingCopy"
        ),
        "tests.unit.test_completion_record_result_support.FakeGitAdapter": _named(
            "tests.unit.test_completion_record_result_support", "FakeGitAdapter"
        ),
        "tests.unit.test_session_controller.StubWorkingCopy": _named(
            "tests.unit.test_session_controller", "StubWorkingCopy"
        ),
        "tests.unit.test_session_controller.MockWorkingCopy": _named(
            "tests.unit.test_session_controller", "MockWorkingCopy"
        ),
        "tests.unit.test_session_controller.TestProcessingCompletedNamesItsBranch"
        "._InvestigationWorkingCopy": _named(
            "tests.unit.test_session_controller",
            "TestProcessingCompletedNamesItsBranch",
            "_InvestigationWorkingCopy",
        ),
        "tests.unit.test_session_controller.TestProcessingCompletedNamesItsBranch"
        "._DetachedWorkingCopy": _named(
            "tests.unit.test_session_controller",
            "TestProcessingCompletedNamesItsBranch",
            "_DetachedWorkingCopy",
        ),
        "tests.unit.test_session_launcher.MockWorkingCopy": _named(
            "tests.unit.test_session_launcher", "MockWorkingCopy"
        ),
        "tests.unit.test_session_restorer.MockWorkingCopy": _named(
            "tests.unit.test_session_restorer", "MockWorkingCopy"
        ),
        # Constructor-dependent, so the import sweep skipped it and the contract
        # never reached it. The dependency is supplied here instead: "detached"
        # is the case these tests care about and the one the contract is about.
        "tests.unit.test_completion_review_exchange_async._MutableBranchReader": _named(
            "tests.unit.test_completion_review_exchange_async",
            "_MutableBranchReader",
            arguments=(None,),
        ),
        "tests.unit.domain.test_review_subject._Checkout": _named(
            "tests.unit.domain.test_review_subject",
            "_Checkout",
            arguments=(None,),
        ),
    }


_FACTORIES = _factories()

_PROTOCOL_DEFINITIONS = {
    # Declarations, not implementations: the bodies are `...`, so there is no
    # behaviour to invoke. Anything that satisfies them IS registered above.
    "issue_orchestrator.ports.working_copy.WorkingCopy",
    "issue_orchestrator.control.completion_ports.GitAdapter",
    "issue_orchestrator.control.completion_record_validation.CompletionValidationGitAdapter",
    "issue_orchestrator.domain.review_subject.CurrentBranchReader",
}


def _dotted_module(path: Path) -> str:
    relative = path.relative_to(_REPO_ROOT).with_suffix("")
    parts = relative.parts
    if parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def _static_definitions() -> dict[str, str]:
    """Dotted name -> ``path:line`` for every ``get_current_branch`` definition.

    Parsed, never imported: this sees nested and method-local classes that an
    import sweep cannot reach, boots no adapters, and cannot silently drop a
    module that fails to import.

    The trade is that a method created at RUNTIME -- by ``type()``, a decorator,
    a metaclass, or assignment rather than ``def`` -- is invisible to it. No
    implementation in the tree is written that way, and the previous import
    sweep's failures were the far more common kind, but a fake built that way
    would escape this guard.
    """
    definitions: dict[str, str] = {}
    for tree in _SWEPT_TREES:
        for source in swept_source_files_in(_REPO_ROOT / tree, root=_REPO_ROOT):
            module = _dotted_module(source)
            parsed = ast.parse(source.read_text(), filename=str(source))

            def visit(node: ast.AST, scope: list[str]) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.ClassDef):
                        qualified = ".".join([module, *scope, child.name])
                        for member in child.body:
                            if (
                                isinstance(
                                    member, ast.FunctionDef | ast.AsyncFunctionDef
                                )
                                and member.name == "get_current_branch"
                            ):
                                definitions[qualified] = (
                                    f"{source.relative_to(_REPO_ROOT)}:{member.lineno}"
                                )
                        visit(child, [*scope, child.name])
                    elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                        visit(child, [*scope, child.name, "<locals>"])
                    else:
                        visit(child, scope)

            visit(parsed, [])
    return definitions


def test_every_definition_in_the_tree_is_accounted_for() -> None:
    """The static guard: no implementation escapes the contract unnoticed.

    A new fake added tomorrow fails here until it is registered, which is the
    part a hand-maintained list alone cannot give you -- and the part the import
    sweep got wrong for the four nested definitions it never saw.
    """
    definitions = _static_definitions()
    accounted = set(_FACTORIES) | _PROTOCOL_DEFINITIONS

    unregistered = {
        name: where for name, where in definitions.items() if name not in accounted
    }
    assert not unregistered, (
        "these classes define get_current_branch but are not held to its"
        " contract. Add a factory to _FACTORIES in this file (hoist a"
        " method-local class to module scope so it can be constructed), or"
        f" list it in _PROTOCOL_DEFINITIONS if it is a Protocol: {unregistered}"
    )

    stale = accounted - set(definitions)
    assert not stale, (
        f"these registry entries no longer exist in the tree: {sorted(stale)}"
    )


def test_the_sweep_actually_reads_the_tree() -> None:
    """Guard the guard: a broken sweep must not silently account for nothing."""
    definitions = _static_definitions()

    assert (
        "issue_orchestrator.execution.git_working_copy.GitWorkingCopy" in definitions
    ), definitions
    assert len(definitions) >= 15, definitions


@pytest.mark.parametrize("name", sorted(_FACTORIES), ids=lambda name: name)
def test_an_unreadable_checkout_returns_none_without_raising(
    name: str, tmp_path: Path
) -> None:
    """The behaviour the emitters rely on, checked by invocation.

    ``tmp_path`` is an empty directory -- not a checkout at all -- which is the
    worst case a caller can hand a working copy. Construction failures are not
    caught: an implementation this contract cannot build is an implementation
    this contract does not cover, and that is a failure, not a skip.
    """
    instance = _FACTORIES[name]()

    try:
        answer = instance.get_current_branch(tmp_path)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - the raise IS the defect under test
        pytest.fail(
            f"{name}.get_current_branch raised {type(exc).__name__} for an"
            " unreadable checkout. The port contracts str | None, and callers"
            " rely on that instead of a defensive except -- a raise here aborts"
            " an already-decided completion."
        )

    assert answer is None or isinstance(answer, str), (
        f"{name}.get_current_branch returned {type(answer).__name__};"
        " the port contracts str | None"
    )


def test_the_port_keeps_documenting_the_none_case() -> None:
    doc = WorkingCopy.get_current_branch.__doc__ or ""

    assert "None" in doc, (
        "the port's own docstring must keep stating the None case; callers rely"
        " on it instead of defensive catches"
    )
