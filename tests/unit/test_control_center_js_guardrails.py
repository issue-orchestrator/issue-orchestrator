"""Source-level guardrails for ``static/js/control_center.js``.

Why grep the JS source instead of running it: the cc dashboard is
plain ES modules served straight to the browser — there is no JS
test harness in this repo. Without a check at the source level,
something as simple as omitting ``reason:`` from a ``fetch`` body
would only surface as a runtime 400 the first time an operator
clicked Stop after deploy. The drift the reviewer flagged on
PR #6263 was exactly this shape, so we encode the contract here.
"""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
CONTROL_CENTER_JS = (
    ROOT / "src" / "issue_orchestrator" / "static" / "js" / "control_center.js"
)
CONTROLS_REFRESH_JS = (
    ROOT
    / "src"
    / "issue_orchestrator"
    / "static"
    / "js"
    / "dashboard"
    / "controls_refresh.js"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


_SHUTDOWN_REASON_ROUTES = (
    "/control/orchestrator/stop",
    "/api/shutdown",
)


def _fetch_body_for_route(source: str, route: str) -> str:
    """Return the body string of the first ``fetch('<route>', { … body: ... })``.

    Bails out with an assertion error if the call site is missing or the
    body block can't be located — both are real regressions because the
    routes are listed in ``_SHUTDOWN_REASON_ROUTES``.
    """
    pattern = re.compile(rf"fetch\(\s*['\"]{re.escape(route)}['\"]")
    match = pattern.search(source)
    assert match, f"No fetch() call found for route {route!r}"
    body_marker = source.find("body:", match.end())
    assert body_marker != -1, f"No body: field after fetch({route!r}, ...)"
    # Capture from "body:" to the end of the JSON.stringify(...) call so
    # we can do plain substring checks for the required fields.
    end = source.find("})", body_marker)
    assert end != -1, f"Could not find body terminator for fetch({route!r}, ...)"
    return source[body_marker:end]


def test_control_center_stop_engine_payload_includes_reason_and_actor() -> None:
    """Stop engine button must send a non-empty reason + actor.

    The /control/orchestrator/stop route now requires ``reason``; if the
    JS forgets it, every Stop-engine click 400s with no UI feedback
    (the original PR shipped with this drift).
    """
    body = _fetch_body_for_route(_read(CONTROL_CENTER_JS), "/control/orchestrator/stop")
    assert "reason:" in body, (
        "control_center.js Stop-engine payload must carry 'reason' "
        "or the new shutdown contract will reject the request"
    )
    assert "actor:" in body, (
        "control_center.js Stop-engine payload must carry 'actor' "
        "for shutdown log attribution"
    )


def test_dashboard_shutdown_payload_includes_reason_and_actor() -> None:
    """The dashboard ``/api/shutdown`` call must send reason + actor."""
    body = _fetch_body_for_route(_read(CONTROLS_REFRESH_JS), "/api/shutdown")
    assert "reason:" in body, (
        "dashboard /api/shutdown payload must carry 'reason' "
        "or the new shutdown contract will reject the request"
    )
    assert "actor:" in body, (
        "dashboard /api/shutdown payload must carry 'actor' "
        "for shutdown log attribution"
    )


def test_no_shutdown_route_is_invoked_without_reason() -> None:
    """No JS file may POST to a reason-required route without a body containing 'reason'."""
    for route in _SHUTDOWN_REASON_ROUTES:
        for js_file in (CONTROL_CENTER_JS, CONTROLS_REFRESH_JS):
            source = _read(js_file)
            for pattern in (
                rf"fetch\(\s*['\"]{re.escape(route)}['\"]",
            ):
                for match in re.finditer(pattern, source):
                    body_marker = source.find("body:", match.end())
                    end = source.find("})", body_marker)
                    if body_marker == -1 or end == -1:
                        # Allow GETs / abort-style POSTs with no body for
                        # routes that don't require a reason; this loop
                        # only inspects bodies when present.
                        continue
                    snippet = source[body_marker:end]
                    assert "reason:" in snippet, (
                        f"{js_file.name} call to {route} must include 'reason' in the body"
                    )
