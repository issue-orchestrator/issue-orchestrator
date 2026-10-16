"""The engine's Tech lead page as its tick last captured it (#8988).

What the page is built from lives in engine state the tick mutates under the
state lock, and a planning cycle holds that lock for the whole tick. A page read
that took the lock waited out the cycle: on an engine planning for ~10 s a tick,
the Control Center's 3 s read budget lapsed on most loads and the board said
"Waiting on you (0)".

So the read never takes the lock. Whoever holds it at a consistent point (the
tick at the end of each cycle, and the one command that changes what the page
shows) publishes here; the page route reads the latest publication. Its
``now`` is when it was captured, which the section reports as ``generated_at``:
the page's "as of".
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..view_models.tech_lead_page import TechLeadPageInputs


class TechLeadPageNotObserved(RuntimeError):
    """The engine has no page to serve: what waits is unknown, not empty."""


class TechLeadPageSnapshot:
    """The latest published page inputs, or why there are none."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inputs: TechLeadPageInputs | None = None
        self._withheld = "the engine has not finished a tick yet"

    def publish(self, inputs: TechLeadPageInputs) -> None:
        with self._lock:
            self._inputs, self._withheld = inputs, ""

    def withhold(self, reason: str) -> None:
        """No current page until the next publish: readers are told *reason*.

        The previous publication goes too: a page the engine can no longer
        stand behind is not served as if current.
        """
        with self._lock:
            self._inputs, self._withheld = None, reason

    def latest(self) -> TechLeadPageInputs:
        with self._lock:
            if self._inputs is None:
                raise TechLeadPageNotObserved(self._withheld)
            return self._inputs


__all__ = ["TechLeadPageNotObserved", "TechLeadPageSnapshot"]
