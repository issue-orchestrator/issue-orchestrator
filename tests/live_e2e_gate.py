"""The live e2e suite runs only on an explicit opt-in, never in an agent session.

``tests/e2e`` is the LIVE suite: it creates, edits and closes real GitHub
issues and PRs under the operator's identity. On 2026-10-14 (#9523) an agent
ran ``pytest -q $(ls tests/unit/*lock*.py tests/unit/infra/*lock*.py ...)``;
zsh's unmatched glob emptied the substitution, pytest fell back to
``testpaths`` (which holds ``tests``) and the suite's cleanup closed 17 real
PRs and deleted their branches.

So whether the suite may run is decided once, here, and enforced at two
boundaries:

- the root conftest keeps implicit collection (a bare ``pytest``, an empty
  path list, ``pytest tests``) out of ``tests/e2e`` unless the run is allowed,
  and makes naming ``tests/e2e``, or a path inside it, a usage error that names
  the opt-in rather than a silent no-op;
- the suite's own conftest refuses the same way whenever pytest loads it, so
  the suite stays closed when the root conftest is not loaded at all
  (``--confcutdir=tests/e2e``).

A run is allowed only with ``IO_E2E=1`` and outside an orchestrator agent
session. An agent session never satisfies the opt-in, whatever its
environment says: every session inherits the engine's environment, so an
``IO_E2E`` exported where the engine started would otherwise reach them all.

This gate is one of three independent layers. The suite also refuses the
production repository and every other repository an io engine manages
(``tests/e2e/fixtures/github_utils.get_test_repo``), and
every PR, branch, issue or label it closes or deletes goes through one owner
that admits only what a typed scope proves is the run's own
(``tests/e2e/fixtures/artifact_cleanup.py``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: The opt-in. ``make test-e2e`` and the other live targets set it.
OPT_IN_ENV = "IO_E2E"

#: Exported by the orchestrator into every agent session it launches; the
#: second is the legacy name, still exported for compatibility.
AGENT_SESSION_ENVS = ("ISSUE_ORCHESTRATOR_SESSION_ID", "ORCHESTRATOR_SESSION_ID")

LIVE_E2E_DIR = Path(__file__).resolve().parent / "e2e"

_LIVE_SUITE = (
    "tests/e2e is the LIVE e2e suite: it creates, edits and closes real GitHub "
    "issues and PRs."
)


@dataclass(frozen=True)
class LiveE2EGate:
    """Whether this pytest process may collect the live e2e suite."""

    opted_in: bool
    #: The variable that marks this process as an agent session, if any.
    agent_session_env: str | None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> LiveE2EGate:
        return cls(
            opted_in=environ.get(OPT_IN_ENV) == "1",
            agent_session_env=next(
                (name for name in AGENT_SESSION_ENVS if environ.get(name)), None
            ),
        )

    @property
    def allows_live_e2e(self) -> bool:
        return self.opted_in and self.agent_session_env is None

    def ignores(self, collection_path: Path) -> bool:
        """Whether implicit collection must skip this path."""
        return not self.allows_live_e2e and is_live_e2e_path(collection_path)

    def refusal(self, targets: Iterable[str], invocation_dir: Path) -> str | None:
        """Why naming ``targets`` on the command line is refused, or None."""
        named = [target for target in targets if is_live_e2e_target(target, invocation_dir)]
        if not named:
            return None
        return self._refusal(named)

    def suite_refusal(self) -> str | None:
        """Why the live suite may not be collected in this process at all, or None.

        The suite's own conftest asks this when pytest loads it: being loaded
        means the suite is being collected, whatever reached it.
        """
        return self._refusal(["tests/e2e"])

    def _refusal(self, refused: list[str]) -> str | None:
        if self.allows_live_e2e:
            return None
        if self.agent_session_env is not None:
            return (
                f"{_LIVE_SUITE} Agent sessions may never run it ({self.agent_session_env} "
                f"is set), and {OPT_IN_ENV} does not apply inside one. Refused: "
                f"{', '.join(refused)}"
            )
        return (
            f"{_LIVE_SUITE} It runs only on explicit opt-in: set {OPT_IN_ENV}=1 "
            "(make test-e2e does) and E2E_TEST_REPO=<owner>/<test-repo>, a "
            f"dedicated repository that is not this checkout's. Refused: {', '.join(refused)}"
        )


def is_live_e2e_path(path: Path) -> bool:
    """Whether ``path`` is the live suite's directory or anything inside it."""
    resolved = path.resolve()
    return resolved == LIVE_E2E_DIR or LIVE_E2E_DIR in resolved.parents


def is_live_e2e_target(target: str, invocation_dir: Path) -> bool:
    """Whether a pytest command-line target names the live suite or a path in it.

    A target is a path with an optional ``::node`` suffix, relative to the
    directory pytest was invoked from.
    """
    return is_live_e2e_path(invocation_dir / target.split("::", 1)[0])
