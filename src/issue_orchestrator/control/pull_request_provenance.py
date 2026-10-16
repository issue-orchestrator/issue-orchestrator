"""THE owner of whether io opened a PR (#9499 review F1).

A PR whose provenance is io's own skips the integration effort's custody: no
adoption, no admission rule, no reviewed-head receipt before a merge. So the
answer must come from something the PR's author cannot write. Its body can't
provide that: whoever opens a PR writes its body, and could paste io's
marker into a hand-opened one from the start.

GitHub records the account that opened a PR as its author - the credential
the PR was created under - and nothing can edit it afterwards. io opens its
PRs under its own credential (its token's user, or its App's bot account:
:meth:`~..ports.pull_request_tracker.PullRequestTracker.engine_account_id`),
the same identity every receipt io trusts is matched against. A PR is io's
own work (:attr:`~..domain.external_pr_adoption.Authorship.IO`) when that
author is io's account AND its body carries io's marker; the marker is only
the hint that lets every other PR be answered without reading io's account.

Every boundary asks this one owner, and carries its typed answer in
:class:`~..domain.external_pr_adoption.PullRequestFacts`: discovery and the
custody listings, startup recovery, queue and launch admission, and the
integration owner's routing, update and merge gates.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..domain.external_pr_adoption import Authorship, io_owned, marker_hint

if TYPE_CHECKING:
    from ..ports.pull_request_tracker import PRInfo


class EngineAccount(Protocol):
    """The account io opens PRs as (the repository host)."""

    def engine_account_id(self) -> int: ...


@dataclass(frozen=True)
class PullRequestProvenance:
    """Who opened a PR: io (its own account, its marker) or anyone else."""

    host: EngineAccount

    def authorship(self, pr: "PRInfo") -> Authorship:
        """io only when GitHub records io's own account as *pr*'s author and its
        body carries io's marker. Reads io's account (once per process) only
        for a PR with the marker; raises RepositoryHostError when it cannot."""
        if not marker_hint(pr.body) or pr.author is None:
            return Authorship.EXTERNAL
        return Authorship.IO if pr.author.account_id == self.host.engine_account_id() else Authorship.EXTERNAL

    def io_owned(self, pr: "PRInfo", *, labels: Iterable[str]) -> bool:
        """io's own PR: io opened it, and it was never adopted (*labels* as the boundary read them)."""
        return io_owned(self.authorship(pr), labels)


__all__ = ["EngineAccount", "PullRequestProvenance"]
