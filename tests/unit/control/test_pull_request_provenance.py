"""THE owner of whether io opened a PR (#9499 review F1), at its port boundary."""

from __future__ import annotations

import pytest

from issue_orchestrator.control.pull_request_provenance import PullRequestProvenance
from issue_orchestrator.domain.external_pr_adoption import ORCHESTRATOR_PR_MARKER, Authorship
from issue_orchestrator.ports.pull_request_tracker import PRInfo, PullRequestAuthor
from issue_orchestrator.ports.repository_host import RepositoryHostError
from tests.conftest import HAND_AUTHOR, IO_ACCOUNT_ID, IO_AUTHOR

MARKED = f"Closes #7\n\n*{ORCHESTRATOR_PR_MARKER}*"


class _Account:
    """The engine's account as the repository host reads it, counting reads."""

    def __init__(self, error: RepositoryHostError | None = None) -> None:
        self.error = error
        self.reads = 0

    def engine_account_id(self) -> int:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return IO_ACCOUNT_ID


def _pr(*, body: str = MARKED, author: PullRequestAuthor | None = IO_AUTHOR) -> PRInfo:
    return PRInfo(number=70, title="t", url="u", branch="7-x", body=body, state="open", labels=[], author=author)


@pytest.mark.parametrize(
    ("body", "author", "expected"),
    [
        (MARKED, IO_AUTHOR, Authorship.IO),
        # io's marker pasted into a hand-opened PR: GitHub records someone else.
        (MARKED, HAND_AUTHOR, Authorship.EXTERNAL),
        # The opening account is gone (GitHub's ghost): nothing proves io opened it.
        (MARKED, None, Authorship.EXTERNAL),
        # io opened it, but its body no longer carries the marker the lanes look for.
        ("Closes #7", IO_AUTHOR, Authorship.EXTERNAL),
    ],
)
def test_io_opened_a_pr_only_when_io_s_account_is_its_author_and_it_carries_io_s_marker(
    body: str, author: PullRequestAuthor | None, expected: Authorship,
) -> None:
    assert PullRequestProvenance(_Account()).authorship(_pr(body=body, author=author)) is expected


def test_a_pr_without_io_s_marker_is_answered_without_reading_io_s_account() -> None:
    account = _Account()

    assert PullRequestProvenance(account).authorship(_pr(body="Closes #7", author=HAND_AUTHOR)) is Authorship.EXTERNAL
    assert account.reads == 0


def test_an_unreadable_account_proves_nothing() -> None:
    """Fail loud: a caller fails closed on the error, never on a guessed answer."""
    provenance = PullRequestProvenance(_Account(RepositoryHostError("502 /user")))

    with pytest.raises(RepositoryHostError):
        provenance.authorship(_pr())


def test_once_adopted_not_even_a_pr_io_opened_is_io_s_own() -> None:
    provenance = PullRequestProvenance(_Account())

    assert provenance.io_owned(_pr(), labels=["needs-code-review"])
    assert not provenance.io_owned(_pr(), labels=["origin:external"])
    assert not provenance.io_owned(_pr(author=HAND_AUTHOR), labels=[])


def test_a_boundary_that_leaves_unproven_prs_alone_reads_an_unreadable_account_as_not_io_s() -> None:
    """Startup recovery and the orphaned-label reconcile act only on PROVEN io work."""
    unreadable = PullRequestProvenance(_Account(RepositoryHostError("502 /user")))

    assert not unreadable.proven_io_owned(_pr())
    assert PullRequestProvenance(_Account()).proven_io_owned(_pr())
    adopted = _pr()
    adopted.labels.append("origin:external")
    assert not PullRequestProvenance(_Account()).proven_io_owned(adopted)
