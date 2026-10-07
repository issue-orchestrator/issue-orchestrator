"""What the engine adds to an agent's launch prompt, resolved before launch (#8141).

Two things, each owned elsewhere and composed here so no launch path can carry
one without the other:

* the issue's **standing rulings** (``domain/standing_ruling``), PREPENDED:
  they bind the session, so they come before the task;
* the repository's **coder addendum** (``domain/coder_prompt``), appended, for
  the coder kinds it applies to.

Both are resolved (all I/O done) before the launch mutates anything, so a
composition can never fail half-way; one that cannot be resolved makes the
launch :class:`LaunchPromptUnavailable` instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from .coder_prompt import PreparedCoderPromptAddendum

#: Between the binding rulings and the task they bind.
RULINGS_SEPARATOR = "\n\n---\n\n"


@dataclass(frozen=True, slots=True)
class PreparedLaunchPrompt:
    """A resolved launch-prompt composition; composing performs no I/O."""

    #: The binding standing-rulings section, or None when the issue has none.
    rulings: str | None
    coder: PreparedCoderPromptAddendum

    @property
    def addendum(self) -> str | None:
        """The coder addendum alone (a review-exchange coder turn carries it)."""
        return self.coder.addendum

    def compose(self, prompt: str) -> str:
        """*prompt* with the rulings before it and the coder addendum after it."""
        bound = prompt if self.rulings is None else f"{self.rulings}{RULINGS_SEPARATOR}{prompt}"
        return self.coder.compose(bound)


@dataclass(frozen=True, slots=True)
class LaunchPromptUnavailable:
    """Something a launch prompt requires could not be resolved."""

    reason: str


LaunchPromptPreparation = PreparedLaunchPrompt | LaunchPromptUnavailable
