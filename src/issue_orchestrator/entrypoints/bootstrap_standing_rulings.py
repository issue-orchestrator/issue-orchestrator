"""Composition of an issue's standing rulings and the launch prompt that carries them (#8141).

One owner per process: the launch prompt every session reads, the review rule
in the completion pipeline, the tech-lead executors that record rulings, the
operator's ruling route and the tech-lead page all share it, so a ruling one
of them records is the ruling every other one sees.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..control.launch_prompt import IssueLaunchPrompt
from ..control.standing_rulings import StandingRulingsOwner
from ..execution.internal_review_prompt import build_coder_prompt_addendum_provider
from ..infra.standing_rulings_store import SqliteStandingRulingsIndex

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.repository_host import RepositoryHost


def build_issue_prompt_owners(
    config: "Config", repository_host: "RepositoryHost"
) -> tuple[IssueLaunchPrompt, StandingRulingsOwner]:
    """The launch-prompt owner and the standing-rulings owner it reads."""
    rulings = StandingRulingsOwner(
        read_issue=repository_host.get_issue,
        write_body=repository_host.update_issue_body,
        index=SqliteStandingRulingsIndex.for_repo(config.repo_root),
    )
    return IssueLaunchPrompt(build_coder_prompt_addendum_provider(config), rulings), rulings
