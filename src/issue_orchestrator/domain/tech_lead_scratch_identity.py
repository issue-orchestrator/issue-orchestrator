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

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePath

logger = logging.getLogger(__name__)

#: Length of the random token appended to a scratch worktree/branch name.
SCRATCH_TOKEN_LENGTH = 12

#: ``<repo-root-name>-tech-lead-<focus issue>-<token>``
_WORKTREE_NAME_RE = re.compile(
    rf"^.+-tech-lead-(?P<issue>\d+)-(?P<token>[0-9a-f]{{{SCRATCH_TOKEN_LENGTH}}})$"
)

#: ``tech-lead-investigation-<focus issue>-<token>``
_BRANCH_NAME_RE = re.compile(
    rf"^tech-lead-investigation-(?P<issue>\d+)-(?P<token>[0-9a-f]{{{SCRATCH_TOKEN_LENGTH}}})$"
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


@dataclass(frozen=True, slots=True)
class ScratchNameParts:
    """What one scratch name says: which focus issue, and which RUN of it."""

    issue_number: int
    token: str


def parse_scratch_worktree_name(name: str) -> ScratchNameParts | None:
    """Read a scratch worktree directory BASENAME, or ``None``."""
    match = _WORKTREE_NAME_RE.match(name)
    if match is None:
        return None
    return ScratchNameParts(
        issue_number=int(match.group("issue")), token=match.group("token")
    )


def parse_scratch_branch_name(name: str) -> ScratchNameParts | None:
    """Read an investigation BRANCH name, or ``None``."""
    match = _BRANCH_NAME_RE.match(name)
    if match is None:
        return None
    return ScratchNameParts(
        issue_number=int(match.group("issue")), token=match.group("token")
    )


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


def scratch_worktree_name_pattern(repo_root_name: str) -> str:
    """The regex SOURCE for a scratch worktree basename of one repository.

    Exported so a caller that must dispatch on "does this directory look like
    an investigation worktree at all?" -- startup reconciliation classifying a
    registered worktree, and the reviewer worktrees named after one -- composes
    the owner's grammar instead of re-typing it. A second copy of the shape
    stops matching the moment the generator changes, and a classifier that
    stops matching does not fail: it silently reclassifies (#6969, #7263).
    """
    return (
        rf"{re.escape(repo_root_name)}-tech-lead-\d+-"
        rf"[0-9a-f]{{{SCRATCH_TOKEN_LENGTH}}}"
    )


def ordinary_worktree_name_pattern(repo_root_name: str) -> str:
    """The regex source for an ordinary issue worktree basename.

    Its only purpose here is to sit beside the scratch grammar: the two are read
    together by every caller that tells them apart.
    """
    return rf"{re.escape(repo_root_name)}-\d+"


@dataclass(frozen=True, slots=True)
class ScratchWorktreeIdentity:
    """Disposable, run-scoped worktree identity for an investigation session.

    A tech-lead failure investigation READS its focus issue's branch history and
    run-dirs as evidence and must never mutate them (#6823). It therefore runs
    in a throwaway worktree -- a unique directory basename plus a fresh branch
    off the base branch, keyed to this run rather than the focus issue -- so the
    focus worktree/branch stay pure read-only evidence and even an agent commit
    can only ever land on the disposable scratch branch.

    Its presence fully determines the worktree directory basename and branch,
    and implies a clean checkout off the base branch: reuse of any existing
    worktree and the configured worktree ``seed_ref`` are both suppressed (the
    caller disables reuse; ``WorktreeContext.create`` suppresses the seed).

    It lives here, beside the generator and matcher that decide its two strings,
    because an identity type whose shape is owned somewhere else is how the two
    f-strings drifted from their classifier in the first place.
    """

    worktree_name: str
    branch_name: str


def new_scratch_identity(
    repo_root_name: str, issue_number: int
) -> ScratchWorktreeIdentity:
    """A fresh disposable identity for one investigation run."""
    token = new_scratch_token()
    return ScratchWorktreeIdentity(
        worktree_name=scratch_worktree_name(repo_root_name, issue_number, token),
        branch_name=scratch_branch_name(issue_number, token),
    )


def scratch_pair_identity(
    worktree_name: str, branch_name: str
) -> ScratchWorktreeIdentity | None:
    """The identity a (worktree basename, branch) pair describes, if they agree.

    ``None`` when either half is not a scratch name, or when the two are from
    different runs. Agreement means the same focus issue AND the same run
    TOKEN: matching issue numbers alone accepts halves from two different
    investigations of one issue, which describe no single checkout.

    A pair matcher rather than a verdict: the only question a caller has is
    whether these two names belong together, and answering more than that is
    how an owner accumulates states nobody reads.
    """
    from_worktree = parse_scratch_worktree_name(worktree_name)
    if from_worktree is None or parse_scratch_branch_name(branch_name) != from_worktree:
        return None
    return ScratchWorktreeIdentity(
        worktree_name=worktree_name, branch_name=branch_name
    )
