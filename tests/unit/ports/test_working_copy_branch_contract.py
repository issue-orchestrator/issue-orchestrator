"""Every WorkingCopy reports a missing branch the same way (#7263 review F4).

``WorkingCopy.get_current_branch`` contracts to return ``str | None`` -- ``None``
on a detached HEAD or a read failure. Callers code against that: the review and
processing-completed emitters record no branch when it is ``None``, and
deliberately carry no defensive ``except`` because the port already promises not
to raise.

A stub that raises instead is STRICTER than the contract its callers were written
for, so a caller that handles ``None`` correctly still aborts. That is exactly
what the simulated-scenario ``StubWorkingCopy`` did, and on a detached checkout it
would have failed an already-decided completion.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from issue_orchestrator.ports.working_copy import WorkingCopy

#: Annotations may arrive as a real ``types.UnionType`` or, under
#: ``from __future__ import annotations``, as the source string. Normalise both.
_PERMITTED = {"str | None", "Optional[str]", "str|None", "typing.Optional[str]"}


def _implementations() -> list[type]:
    from issue_orchestrator.execution.git_revision_reader import GitRevisionReader
    from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
    from tests.simulated_scenarios.conftest import StubWorkingCopy as ScenarioStub
    from tests.unit.test_session_controller import StubWorkingCopy as ControllerStub

    return [GitWorkingCopy, GitRevisionReader, ScenarioStub, ControllerStub]


@pytest.mark.parametrize(
    "implementation", _implementations(), ids=lambda c: c.__name__
)
def test_get_current_branch_admits_none(implementation: type) -> None:
    annotation = inspect.signature(implementation.get_current_branch).return_annotation
    rendered = annotation if isinstance(annotation, str) else str(annotation)

    assert rendered in _PERMITTED, (
        f"{implementation.__name__}.get_current_branch is annotated"
        f" {rendered!r}. The port contracts str | None; narrowing it to str"
        " makes the implementation stricter than every caller was written for."
    )


def test_a_checkout_with_no_readable_branch_reads_as_none(tmp_path: Path) -> None:
    """The production adapter's answer for an unreadable checkout is None."""
    from issue_orchestrator.execution.git_working_copy import GitWorkingCopy

    assert GitWorkingCopy().get_current_branch(tmp_path) is None


def test_the_port_keeps_documenting_the_none_case() -> None:
    doc = WorkingCopy.get_current_branch.__doc__ or ""

    assert "None" in doc, (
        "the port's own docstring must keep stating the None case; callers rely"
        " on it instead of defensive catches"
    )
