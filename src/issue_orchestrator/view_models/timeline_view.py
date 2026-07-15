"""Single owner of the timeline-view enum's *runtime* policy.

The wire enum lives in ``docs/api/ui-openapi.json`` as the reusable
``TimelineView`` schema and is generated into
``contracts.ui_openapi_models.TimelineView``.  That generated ``Literal``
is the single source of truth for the supported view values; deriving the
valid set from it here means a new view added to the schema flows through
to every route without a second edit.

Both the issue-detail drawer routes and the E2E-run timeline route accept
an untrusted ``view`` query value and coerce an unrecognised one back to
the default story view.  Centralising that here keeps the two endpoints
from drifting (previously each carried its own inline ``{"user", "ops",
"debug", "raw"}`` literal set, and one canonical model was already missing
``"raw"``).
"""

from __future__ import annotations

from typing import cast, get_args

from ..contracts.ui_openapi_models import TimelineView

#: The default view used when no value (or an unrecognised value) is supplied.
DEFAULT_TIMELINE_VIEW: TimelineView = "user"

#: Every supported timeline view, derived from the generated wire enum.
TIMELINE_VIEWS: frozenset[str] = frozenset(get_args(TimelineView))


def normalize_timeline_view(view: str) -> TimelineView:
    """Coerce an untrusted ``view`` query value to a supported timeline view.

    Unknown values fall back to :data:`DEFAULT_TIMELINE_VIEW` rather than
    raising, matching the endpoints' existing forgiving behaviour for
    stale bookmarks and hand-typed URLs.
    """
    if view in TIMELINE_VIEWS:
        return cast("TimelineView", view)
    return DEFAULT_TIMELINE_VIEW
