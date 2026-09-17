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

The first version of this file asserted on ``inspect.signature(...)
.return_annotation``, which is **theatre**: an implementation can keep the
``str | None`` annotation and still raise, and every test here passed. These tests
now INVOKE each implementation against an unreadable checkout, which is the
behaviour callers actually depend on.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest

from issue_orchestrator.ports.working_copy import WorkingCopy


def _discovered_implementations() -> list[type]:
    """Every class in src and tests that defines ``get_current_branch``.

    Discovered rather than listed. A hand-maintained inventory is how the
    simulated-scenario stub diverged from its port unnoticed in the first place,
    and a new fake added tomorrow must be held to the contract without anyone
    remembering to add it here.
    """
    import issue_orchestrator
    import tests

    found: dict[str, type] = {}
    for package in (issue_orchestrator, tests):
        for module in pkgutil.walk_packages(
            package.__path__, prefix=f"{package.__name__}."
        ):
            try:
                loaded = importlib.import_module(module.name)
            except Exception:  # noqa: BLE001 - an unimportable module is not ours to fix
                continue
            for _, obj in inspect.getmembers(loaded, inspect.isclass):
                if obj.__module__ != loaded.__name__:
                    continue
                candidate = obj.__dict__.get("get_current_branch")
                if candidate is None or not callable(candidate):
                    continue
                found.setdefault(f"{obj.__module__}.{obj.__qualname__}", obj)
    return [found[key] for key in sorted(found)]


_IMPLEMENTATIONS = _discovered_implementations()


def test_discovery_finds_the_known_implementations() -> None:
    """The sweep must actually be finding things, or every test below is vacuous."""
    names = {implementation.__name__ for implementation in _IMPLEMENTATIONS}

    assert "GitWorkingCopy" in names, names
    assert len(_IMPLEMENTATIONS) >= 3, names


@pytest.mark.parametrize(
    "implementation",
    _IMPLEMENTATIONS,
    ids=lambda c: f"{c.__module__.rsplit('.', 1)[-1]}.{c.__name__}",
)
def test_an_unreadable_checkout_returns_none_without_raising(
    implementation: type, tmp_path: Path
) -> None:
    """The behaviour the emitters rely on, checked by invocation."""
    try:
        instance = implementation()
    except TypeError:
        pytest.skip(f"{implementation.__name__} needs constructor arguments")

    try:
        answer = instance.get_current_branch(tmp_path)
    except Exception as exc:  # noqa: BLE001 - the raise IS the defect under test
        pytest.fail(
            f"{implementation.__module__}.{implementation.__name__}"
            f".get_current_branch raised {type(exc).__name__} for an unreadable"
            " checkout. The port contracts str | None, and callers rely on that"
            " instead of a defensive except -- a raise here aborts an"
            " already-decided completion."
        )

    assert answer is None or isinstance(answer, str), (
        f"{implementation.__name__}.get_current_branch returned"
        f" {type(answer).__name__}; the port contracts str | None"
    )


def test_the_port_keeps_documenting_the_none_case() -> None:
    doc = WorkingCopy.get_current_branch.__doc__ or ""

    assert "None" in doc, (
        "the port's own docstring must keep stating the None case; callers rely"
        " on it instead of defensive catches"
    )
