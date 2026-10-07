"""The one composition of an agent's launch-prompt additions (#8141).

Every launch path (coding, a validation retry of any kind, rework of every
kind, review, retrospective review, a review exchange's pair, a tech-lead
run) asks :class:`IssueLaunchPrompt` before it mutates anything, so the
issue's standing rulings reach every agent on the issue by construction, not
by each path remembering to add them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.coder_prompt import CoderPromptAddendumUnavailable, PreparedCoderPromptAddendum
from ..domain.launch_prompt import (
    LaunchPromptPreparation,
    LaunchPromptUnavailable,
    PreparedLaunchPrompt,
)
from ..domain.session_kind import SessionKind
from ..ports.coder_prompt import NO_CODER_PROMPT_ADDENDUM, CoderPromptAddendumProvider
from ..ports.standing_rulings import (
    NO_STANDING_RULINGS,
    StandingRulings,
    StandingRulingsUnavailable,
)


@dataclass(frozen=True)
class IssueLaunchPrompt:
    """:class:`~..ports.launch_prompt.LaunchPromptProvider` over its two owners."""

    coder_addendum: CoderPromptAddendumProvider
    standing_rulings: StandingRulings

    def prepare(self, *, kind: SessionKind, issue_number: int) -> LaunchPromptPreparation:
        # A reviewer is never handed the coder's addendum, so it is not even asked.
        reviewer = kind.capabilities.reports_verdict
        coder = PreparedCoderPromptAddendum(None) if reviewer else self.coder_addendum.prepare(kind=kind)
        if isinstance(coder, CoderPromptAddendumUnavailable):
            return LaunchPromptUnavailable(coder.reason)
        try:
            rulings = self.standing_rulings.prompt_section(issue_number, kind)
        except StandingRulingsUnavailable as error:
            return LaunchPromptUnavailable(str(error))
        return PreparedLaunchPrompt(rulings=rulings, coder=coder)


#: No coder addendum and no rulings: compositions without a repository host.
NO_LAUNCH_PROMPT = IssueLaunchPrompt(NO_CODER_PROMPT_ADDENDUM, NO_STANDING_RULINGS)
