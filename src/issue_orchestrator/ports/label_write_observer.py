"""Port: a label write the engine just made, told to whoever caches labels (#8113).

Every engine label write ends at the repository adapter: the shared-block
owner, the governed label set, the action applier, label sync, claims and the
approval owner all hold that one capability. So the adapter is the one place
that sees every add AND every remove, and it reports each successful write
here. The engine's issue cache subscribes, so the tick-start copy of an issue
stops lagging the engine's own writes until the next GitHub refresh: a block
removed this tick no longer earns a triage grant, and a block put on this tick
is on the agenda of a review launched after it.

Told only what GitHub now holds, never what was attempted: a write that failed
or could not be verified reports nothing, and a remove GitHub answered with 404
(the label was already gone) reports the label absent, because it is.
"""

from __future__ import annotations

from typing import Protocol


class LabelWriteObserver(Protocol):
    """Hears each label write the adapter completed."""

    def label_written(self, issue_number: int, label: str, *, present: bool) -> None:
        """``label`` now stands on ``issue_number`` (``present``) or does not.

        Called on the writing thread, after the write. ``issue_number`` may be
        a pull request's: issues and PRs share GitHub's number space, and an
        observer that holds no such number has nothing to do.
        """
        ...


class IgnoreLabelWrites:
    """The observer of an adapter with no label cache to keep coherent.

    One-off tools (setup, test data, CLI helpers) build an adapter with no
    engine state behind it; only the composition root has a cache to tell.
    """

    def label_written(self, issue_number: int, label: str, *, present: bool) -> None:
        return None


IGNORE_LABEL_WRITES = IgnoreLabelWrites()

__all__ = ["IGNORE_LABEL_WRITES", "IgnoreLabelWrites", "LabelWriteObserver"]
