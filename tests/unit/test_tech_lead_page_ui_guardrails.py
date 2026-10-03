"""UI guardrails for the Tech lead page and its badge (#7763).

The badge is the operator's "something waits on you" signal on every page, so
these pin what makes it usable: a text label (never colour or a bare number),
a real link/button that is keyboard reachable with a visible focus ring, an
explicit empty state, labelled lanes, and ONE approve/decline surface.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"
CC_HTML = (ROOT / "templates" / "control_center.html").read_text(encoding="utf-8")
DASHBOARD_HTML = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
CC_JS = (ROOT / "static" / "js" / "control_center.js").read_text(encoding="utf-8")
CC_CSS = (ROOT / "static" / "css" / "control_center_tech_lead.css").read_text(encoding="utf-8")
DASHBOARD_CSS = (ROOT / "static" / "css" / "dashboard" / "base.css").read_text(encoding="utf-8")


def test_header_badge_is_a_labelled_link_to_the_page() -> None:
    badge = re.search(r'<a class="waiting-badge" id="techLeadHeaderBadge" href="\?view=techLead"[^>]*>([^<]+)</a>', CC_HTML)
    assert badge, "the header badge must be a native link to the Tech lead view"
    assert badge.group(1) == "Nothing waiting on you"  # quiet empty state, as text
    # It lives in the global header, outside any view container.
    header = CC_HTML[CC_HTML.index('<header class="header">'):CC_HTML.index("</header>", CC_HTML.index('<header class="header">'))]
    assert 'id="techLeadHeaderBadge"' in header


def test_nav_item_carries_the_count_as_text_inside_its_accessible_name() -> None:
    nav = re.search(r'<button class="nav-item" data-view="techLead" id="techLeadNavItem">(.*?)</button>', CC_HTML, re.S)
    assert nav, "the Tech lead nav entry must be a native button"
    assert "Tech lead" in nav.group(1)
    assert '<span class="waiting-count" id="techLeadNavCount">Nothing waiting on you</span>' in nav.group(1)
    # First nav section: pending items are front and centre.
    assert CC_HTML.index('data-view="techLead"') < CC_HTML.index('data-view="repositories"')


def test_badges_keep_a_visible_focus_ring() -> None:
    assert re.search(r"\.waiting-badge:focus-visible\s*{[^}]*outline: 2px solid", CC_CSS)
    assert re.search(r"\.tl-card:focus-visible\s*{[^}]*outline: 2px solid", CC_CSS)
    assert re.search(r"\.tech-lead-waiting-badge:focus-visible,\s*\.custody-open-tech-lead:focus-visible\s*{[^}]*outline: 2px solid", DASHBOARD_CSS)


def test_page_has_three_labelled_lanes_and_live_status() -> None:
    view = CC_HTML[CC_HTML.index('id="techLeadView"'):]
    for heading, label in (("techLeadWaitingHeading", "Waiting on you"), ("techLeadDoingHeading", "Doing on its own"),
                           ("techLeadWatchingHeading", "Watching")):
        assert f'aria-labelledby="{heading}"' in view
        assert re.search(rf'<h2 id="{heading}"[^>]*>{label}</h2>', view)
    assert 'id="techLeadRunStrip" role="status" aria-live="polite"' in view
    assert 'id="techLeadCommandStatus" role="status" aria-live="polite"' in view
    assert view.index("techLeadWaitingHeading") < view.index("techLeadDoingHeading") < view.index("techLeadWatchingHeading")


def test_page_reads_json_through_the_generated_contract() -> None:
    scripts = re.findall(r'<script src="/static/js/([^"?]+)', CC_HTML)
    order = [scripts.index(name) for name in ("ui-contracts.validators.js", "ui_contract_json.js",
                                              "control_center_tech_lead.js", "control_center.js")]
    assert order == sorted(order)
    assert "control_center_tech_lead.css" in CC_HTML


def test_landing_switches_to_the_page_when_anything_waits() -> None:
    assert "payload?.waiting_count > 0" in CC_JS and "switchView('techLead')" in CC_JS
    assert "setInterval(refreshTechLead" in CC_JS
    assert "type: 'cc-tech-lead-waiting'" in CC_JS  # embedded dashboards get the count


def test_dashboard_badge_is_a_native_button_with_text() -> None:
    assert '<button type="button" class="tech-lead-waiting-badge" id="dashboardTechLeadBadge" hidden>Nothing waiting on you</button>' in DASHBOARD_HTML


def test_one_approval_surface_the_rework_panel_is_gone() -> None:
    assert "reworkProposalsPanel" not in DASHBOARD_HTML
    for path in (ROOT / "static").rglob("*"):
        if path.is_file() and path.suffix in {".js", ".css", ".html"} and "ui-contracts" not in path.name:
            text = path.read_text(encoding="utf-8")
            assert "data-rework-command" not in text, path
            assert "/api/tech-lead/rework-proposals" not in text, path
    # The custody drawer links into the page and never approves on its own.
    custody = (ROOT / "templates" / "_blocked_custody.html").read_text(encoding="utf-8")
    assert "Open in the Tech lead page" in custody and "Approve" not in custody
