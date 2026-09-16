"""Single owner for the tech-lead failure-investigation scratch identity (#6823).

A tech-lead failure investigation launches as an ``issue-{focus}`` session under
the focus issue's number but runs in a disposable, run-scoped worktree on its own
branch, so the focus issue's worktree and branch stay pure read-only evidence.

That naming is not cosmetic: it is the only durable trace, on an already-recorded
timeline row, that separates "this issue's own implementation session" from "a
tech-lead session that merely read this issue as evidence" (#6969). Before this
module the shape existed only as two f-strings inside
``control.tech_lead_session_policy``; a classifier written against a second copy
of the pattern would silently stop matching the moment either string changed.

Generator and matcher therefore live together here, and
``tests/unit/domain/test_tech_lead_scratch_identity.py`` pins that every name this
module generates is recognised by the matcher next to it.
"""

from __future__ import annotations

import re
import uuid
from pathlib import PurePath

#: Length of the random token appended to a scratch worktree/branch name.
SCRATCH_TOKEN_LENGTH = 12

#: ``<repo-root-name>-tech-lead-<focus issue>-<token>``
_WORKTREE_NAME_RE = re.compile(
    rf"^.+-tech-lead-(?P<issue>\d+)-[0-9a-f]{{{SCRATCH_TOKEN_LENGTH}}}$"
)

#: ``tech-lead-investigation-<focus issue>-<token>``
_BRANCH_NAME_RE = re.compile(
    rf"^tech-lead-investigation-(?P<issue>\d+)-[0-9a-f]{{{SCRATCH_TOKEN_LENGTH}}}$"
)


def new_scratch_token() -> str:
    """A fresh token so investigations of one focus issue never collide."""
    return uuid.uuid4().hex[:SCRATCH_TOKEN_LENGTH]


def scratch_worktree_name(repo_root_name: str, issue_number: int, token: str) -> str:
    """The disposable worktree directory basename for one investigation run."""
    return f"{repo_root_name}-tech-lead-{issue_number}-{token}"


def scratch_branch_name(issue_number: int, token: str) -> str:
    """The disposable branch for one investigation run.

    It deliberately does NOT start with the focus issue number so
    ``extract_issue_number_from_branch`` never mistakes it for the focus branch.
    """
    return f"tech-lead-investigation-{issue_number}-{token}"


def scratch_worktree_focus_issue(path: str) -> int | None:
    """The focus issue of the scratch worktree ``path`` sits in, if any.

    Run directories are nested inside the worktree
    (``<scratch worktree>/.issue-orchestrator/sessions/<run>``), so a recorded
    ``run_dir`` is recognised by its ancestor component rather than its own name.

    It returns the issue the shape NAMES rather than a bare boolean, because the
    shape is not self-validating: this pattern can appear anywhere in a path an
    operator chose, and an ancestor directory that happens to match would
    otherwise reclassify every run beneath it. The caller checks the returned
    issue against the record's own stream before treating it as evidence.

    The DEEPEST match wins, so a scratch worktree nested under a coincidentally
    matching parent is still read as itself.
    """
    if not path:
        return None
    for part in reversed(PurePath(path).parts):
        match = _WORKTREE_NAME_RE.match(part)
        if match is not None:
            return int(match.group("issue"))
    return None


def scratch_branch_focus_issue(name: str) -> int | None:
    """The focus issue an investigation branch name declares, if any."""
    match = _BRANCH_NAME_RE.match(name)
    return int(match.group("issue")) if match is not None else None


def is_scratch_worktree_name(name: str) -> bool:
    """True when ``name`` is a scratch worktree directory basename."""
    return _WORKTREE_NAME_RE.match(name) is not None


def is_scratch_branch_name(name: str) -> bool:
    """True when ``name`` is an investigation branch name."""
    return _BRANCH_NAME_RE.match(name) is not None


def path_is_under_scratch_worktree(path: str) -> bool:
    """True when ``path`` sits inside a scratch worktree of any focus issue."""
    return scratch_worktree_focus_issue(path) is not None
