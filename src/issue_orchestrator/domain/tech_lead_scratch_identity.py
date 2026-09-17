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
from enum import Enum
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
    caller disables reuse; ``WorktreeContext.create`` suppresses the seed) --
    EXCEPT when the identity is a continuation, where reuse is the point.

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


class ScratchIdentityVerdict(Enum):
    """What a recorded (worktree, branch) pair turned out to be."""

    #: Neither half names an investigation: an ordinary issue session.
    ORDINARY = "ordinary"
    #: Both halves name the SAME run of the CLAIMED issue: the record is
    #: structurally consistent. That is a statement about the NAMES only. It
    #: does not mean the investigation can be safely resumed -- resuming one
    #: needs its original run's launch authority and inputs, which is #7273.
    CONSISTENT = "consistent"
    #: One half names an investigation and the pair does not agree. Unsafe.
    CORRUPT = "corrupt"


@dataclass(frozen=True, slots=True)
class ScratchIdentityReading:
    """The verdict on a recorded pair, and the identity when there is one."""

    verdict: ScratchIdentityVerdict
    identity: ScratchWorktreeIdentity | None
    detail: str

    @property
    def is_corrupt(self) -> bool:
        return self.verdict is ScratchIdentityVerdict.CORRUPT


def read_scratch_identity(
    worktree_path: str, branch_name: str, issue_number: int
) -> ScratchIdentityReading:
    """Read a recorded (worktree, branch) pair as an investigation identity.

    Read back, never re-minted (#7263): the durable halves a queued retry
    carries are what say WHICH investigation a record is about. Whether that
    investigation can be relaunched is a separate question with a different
    answer -- see ``CONSISTENT`` -- and this module does not answer it.

    ``worktree_path``'s OWN basename must be the scratch worktree, not merely
    sit under one: a run directory nested inside it is not a checkout to reuse.

    Three answers, not two, because "not an investigation" and "an investigation
    whose record does not hold together" must not be handled the same way.
    Falling back to the ordinary derivation on a CORRUPT pair is worse than
    refusing: the caller still holds the recorded branch name, so it would check
    an investigation branch out inside the focus issue's own worktree -- the
    exact mutation this module exists to prevent.

    A pair is ``CONSISTENT`` only when both halves parse, name the same focus
    issue, name the ISSUE THIS RETRY IS FOR, and carry the same run TOKEN.
    Matching issue numbers alone would accept halves from two different
    investigations of one issue, and resuming that pair would check one run's
    branch out in another run's directory.
    """
    worktree_name = PurePath(worktree_path).name if worktree_path else ""
    from_worktree = parse_scratch_worktree_name(worktree_name)
    from_branch = parse_scratch_branch_name(branch_name)

    if from_worktree is None and from_branch is None:
        return ScratchIdentityReading(
            ScratchIdentityVerdict.ORDINARY, None, "not an investigation"
        )
    if from_worktree is None or from_branch is None:
        return _corrupt(
            issue_number,
            worktree_name,
            branch_name,
            "only one half names an investigation",
        )
    if from_worktree != from_branch:
        return _corrupt(
            issue_number,
            worktree_name,
            branch_name,
            "the worktree and the branch are from different investigation runs",
        )
    if from_branch.issue_number != issue_number:
        return _corrupt(
            issue_number,
            worktree_name,
            branch_name,
            f"it belongs to issue {from_branch.issue_number}",
        )
    return ScratchIdentityReading(
        ScratchIdentityVerdict.CONSISTENT,
        ScratchWorktreeIdentity(
            worktree_name=worktree_name, branch_name=branch_name
        ),
        f"investigation run {from_branch.token}",
    )


def _corrupt(
    issue_number: int, worktree_name: str, branch_name: str, why: str
) -> ScratchIdentityReading:
    detail = (
        f"unusable investigation identity for issue {issue_number}: {why} "
        f"(worktree={worktree_name!r} branch={branch_name!r})"
    )
    logger.warning("%s", detail)
    return ScratchIdentityReading(ScratchIdentityVerdict.CORRUPT, None, detail)


def investigation_retry_refusal(worktree_path: str, branch_name: str, issue_number: int) -> str | None:
    """Why a queued validation retry must NOT be relaunched, or ``None``.

    A tech-lead failure investigation runs in a disposable worktree on a
    throwaway branch (#6823) because it READS its focus issue's worktree and
    branch as evidence and must never mutate them. The retry path derives its
    worktree from the focus issue like any other coding retry, so relaunching an
    investigation's retry puts it straight back inside that evidence, on the
    recorded investigation branch, where an agent commit lands on the focus
    branch.

    Resuming it in its own worktree instead is the eventual answer and is #7273:
    the resumed run needs the original run's launch authority and trusted
    inputs, without which completion rejects it as ``missing_authority`` -- and a
    session marked disposable to get its worktree back would then have that
    rejection FORCE-DELETE the branch holding the commits under re-validation.
    Until that lands, the safe answer is not to relaunch at all: escalate, and
    leave the branch exactly where it is.

    A pair that does not hold together is refused for a second reason -- there
    is no single investigation to speak of -- and says which.
    """
    reading = read_scratch_identity(worktree_path, branch_name, issue_number)
    if reading.verdict is ScratchIdentityVerdict.CORRUPT:
        return reading.detail
    if reading.verdict is ScratchIdentityVerdict.CONSISTENT:
        # Its record is fine -- both halves agree. It simply cannot be
        # relaunched yet, which is a different thing from being unusable, and
        # the operator-facing message must not say otherwise.
        return (
            "this is a tech-lead failure investigation, and its record is "
            "consistent; it just cannot be relaunched yet. A validation retry "
            "of an investigation would be derived into the focus issue's own "
            "worktree, mutating the evidence it was sent to read (#6823), and "
            "resuming it in its own disposable worktree needs the original "
            "run's launch authority and inputs, which is #7273."
        )
    return None
