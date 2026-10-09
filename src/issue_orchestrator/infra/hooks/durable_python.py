"""The interpreter path io may write into a file that outlives this process.

Two kinds of generated script name an issue-orchestrator interpreter:

* **Session-scoped** artifacts (the per-worktree ``pre-push`` hook, a Codex
  session's ``-c hooks.PreToolUse`` argv) name the interpreter running *this*
  engine. They live no longer than the engine that wrote them, so
  ``_python_path.resolve_issue_orchestrator_python`` is right for them.
* **Durable** artifacts (``scripts/verify-pr.sh``, which the target repo
  commits) outlive the process, the shell and the checkout that generated
  them. Every later push on every clone reads the path back.

This module owns the durable case. It picks the interpreter from one ordered
source list and refuses a path that is known not to last: one inside a linked
git worktree (``git worktree remove`` deletes it) or inside a temporary
directory. It never substitutes a different interpreter on its own; a refusal
names the remedy instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import shlex
import sys
import tempfile

from ._python_path import ORCHESTRATOR_PYTHON_ENV

EXPLICIT_PYTHON_FLAG = "--python"


class DurablePythonSource(str, Enum):
    """Where the durable interpreter path came from."""

    EXPLICIT = "explicit"
    ENVIRONMENT = "environment"
    RUNNING_INTERPRETER = "running_interpreter"

    def describe(self) -> str:
        if self is DurablePythonSource.EXPLICIT:
            return EXPLICIT_PYTHON_FLAG
        if self is DurablePythonSource.ENVIRONMENT:
            return ORCHESTRATOR_PYTHON_ENV
        return "the interpreter running this command"


class UnstableInterpreterError(RuntimeError):
    """Raised when the only candidate interpreter would not outlive this run."""


@dataclass(frozen=True)
class DurableOrchestratorPython:
    """An absolute interpreter path that is safe to commit into a repository."""

    path: Path
    source: DurablePythonSource

    def shell_literal(self) -> str:
        return shlex.quote(str(self.path))


def resolve_durable_orchestrator_python(
    explicit: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
    running_interpreter: Path | None = None,
    temp_roots: tuple[Path, ...] | None = None,
) -> DurableOrchestratorPython:
    """Return the interpreter to write into a durable generated script.

    Source order: *explicit* (``--python``), then ``ISSUE_ORCHESTRATOR_PYTHON``,
    then the running interpreter. The first source that is set is the one
    used: an unusable or unstable choice raises rather than falling through to
    a later source, so the written path is always the one the operator chose
    (or the one they can see they did not override).
    """
    env = os.environ if environ is None else environ
    roots = default_temp_roots() if temp_roots is None else temp_roots
    candidate, source = _first_candidate(
        explicit,
        env.get(ORCHESTRATOR_PYTHON_ENV),
        Path(sys.executable) if running_interpreter is None else running_interpreter,
    )
    if not candidate.is_absolute():
        raise UnstableInterpreterError(
            f"Interpreter from {source.describe()} must be an absolute path: {candidate}"
        )
    if not (candidate.is_file() and os.access(candidate, os.X_OK)):
        raise UnstableInterpreterError(
            f"Interpreter from {source.describe()} is not an executable file: {candidate}"
        )
    reason = unstable_interpreter_reason(candidate, temp_roots=roots)
    if reason is not None:
        raise UnstableInterpreterError(_refusal_message(candidate, source, reason))
    return DurableOrchestratorPython(path=candidate, source=source)


def unstable_interpreter_reason(
    path: Path,
    *,
    temp_roots: tuple[Path, ...] | None = None,
) -> str | None:
    """Return why *path* will not outlive this run, or None when it should.

    *path* is judged as written AND as its symlinks resolve. As written,
    because a venv's ``bin/python`` usually links to a stable system
    interpreter while the venv itself is what ``git worktree remove`` deletes.
    As resolved, because a stable-looking symlink into a worktree dangles once
    the worktree goes. The containing directory is also resolved on its own so
    ``/tmp`` → ``/private/tmp`` style aliases match a temp root.
    """
    roots = default_temp_roots() if temp_roots is None else temp_roots
    forms = _path_forms(path)
    for form in forms:
        worktree = _enclosing_linked_worktree(form)
        if worktree is not None:
            return f"it is inside the linked git worktree {worktree}"
    for root in roots:
        if any(form == root or form.is_relative_to(root) for form in forms):
            return f"it is inside the temporary directory {root}"
    return None


def default_temp_roots() -> tuple[Path, ...]:
    """The temporary directories of this host, resolved and de-duplicated."""
    raw = [Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp")]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        raw.append(Path(tmpdir))
    resolved: list[Path] = []
    for root in raw:
        for form in (root.absolute(), root.resolve()):
            if form != Path(form.anchor) and form not in resolved:
                resolved.append(form)
    return tuple(resolved)


def _first_candidate(
    explicit: Path | None,
    environment: str | None,
    running: Path,
) -> tuple[Path, DurablePythonSource]:
    if explicit is not None:
        return explicit.expanduser(), DurablePythonSource.EXPLICIT
    if environment:
        return Path(environment).expanduser(), DurablePythonSource.ENVIRONMENT
    return running, DurablePythonSource.RUNNING_INTERPRETER


def _path_forms(path: Path) -> tuple[Path, ...]:
    absolute = path.absolute()
    forms: list[Path] = []
    for form in (absolute, absolute.parent.resolve() / absolute.name, absolute.resolve()):
        if form not in forms:
            forms.append(form)
    return tuple(forms)


def _enclosing_linked_worktree(path: Path) -> Path | None:
    """Return the nearest linked worktree containing *path*, if any.

    Git marks a linked worktree with a ``.git`` *file* naming a per-worktree
    git dir that holds a ``commondir`` file. A main checkout has a ``.git``
    directory, and a submodule's git dir has no ``commondir``; neither is
    itself deleted by ``git worktree remove``, but either may sit inside a
    linked worktree that is, so every ancestor is checked.
    """
    for ancestor in path.parents:
        dot_git = ancestor / ".git"
        if not dot_git.is_file():
            continue
        gitdir = _gitdir_from_file(dot_git)
        if gitdir is not None and (gitdir / "commondir").is_file():
            return ancestor
    return None


def _gitdir_from_file(dot_git: Path) -> Path | None:
    try:
        content = dot_git.read_text().strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    gitdir = Path(content.removeprefix("gitdir:").strip())
    if not gitdir.is_absolute():
        gitdir = dot_git.parent / gitdir
    return gitdir


def _main_checkout_python(worktree: Path) -> Path | None:
    gitdir = _gitdir_from_file(worktree / ".git")
    if gitdir is None:
        return None
    try:
        common = Path((gitdir / "commondir").read_text().strip())
    except OSError:
        return None
    if not common.is_absolute():
        common = gitdir / common
    main_checkout = common.resolve().parent
    for name in ("python", "python3"):
        candidate = main_checkout / ".venv" / "bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _refusal_message(
    candidate: Path,
    source: DurablePythonSource,
    reason: str,
) -> str:
    lines = [
        f"Refusing to write {candidate} (from {source.describe()}) into a committed "
        f"script: {reason}, so the path disappears with it and every later push "
        "falls through to a weaker interpreter or fails.",
        f"Pass {EXPLICIT_PYTHON_FLAG} <interpreter> or export {ORCHESTRATOR_PYTHON_ENV} "
        "naming a stable issue-orchestrator installation.",
    ]
    worktrees = (_enclosing_linked_worktree(form) for form in _path_forms(candidate))
    worktree = next((found for found in worktrees if found is not None), None)
    suggestion = None if worktree is None else _main_checkout_python(worktree)
    if suggestion is not None:
        lines.append(f"The main checkout's interpreter is {suggestion}.")
    return " ".join(lines)
